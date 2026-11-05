"""Public, keyless research sources with a reproducible on-disk cache.

Every fetch goes through `Research._cached()`:
  * mode "auto"  (default): try the live source; on any failure fall back to the
                             cached copy (and say so in the run log).
  * mode "live":            live only — a failure is a failure.
  * mode "cache":           never touch the network; use what was cached earlier
                             (used by the tests and for a reproducible demo).
Each successful live fetch overwrites the cache entry, so the cache is always the
last thing the process actually saw. Results are stored *normalised* (headlines,
job titles, page title/description) rather than as raw pages.

Sources (all public, no API key, no login):
  - Google News RSS search          -> recent news headlines
  - Wikipedia REST summary + search  -> company identity / disambiguation
  - Wikidata                        -> official website of a Wikipedia entity
  - Company website                 -> homepage <title> + meta description
  - Greenhouse / Lever / Ashby       -> public job-board APIs (hiring signals)
LinkedIn is deliberately NOT used: scraping it violates its terms.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).resolve().parent / "data" / "cache"
UA = "Mozilla/5.0 (compatible; OutreachResearchDemo/1.0; +case-study prototype)"
TIMEOUT = 8


class FetchError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key_path(key: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{h}.json"


_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="fetch")


def _get(url: str, *, params: dict | None = None, accept_404: bool = True) -> requests.Response:
    # `timeout=` only bounds each socket operation; a stalled TLS handshake or a slow
    # trickle can still hang. Enforce a hard wall-clock deadline as well.
    fut = _POOL.submit(requests.get, url, params=params, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}, timeout=TIMEOUT)
    try:
        r = fut.result(timeout=TIMEOUT + 3)
    except FuturesTimeout:
        raise FetchError(f"no response within {TIMEOUT + 3}s")
    if r.status_code == 404 and accept_404:
        return r
    if r.status_code == 429:
        raise FetchError("rate-limited (HTTP 429)")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    return r


class Research:
    def __init__(self, mode: str | None = None):
        self.mode = (mode or os.environ.get("RESEARCH_MODE") or "auto").lower()
        if self.mode not in ("auto", "live", "cache"):
            self.mode = "auto"
        self.stats = {"live": 0, "cache": 0, "fallback": 0, "miss": 0}
        self.trace: list[dict] = []  # every fetch, for the UI
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ cache core
    def _cached(self, key: str, url: str, fn) -> tuple[object | None, dict]:
        """Return (data, meta). meta.origin in live | cache | fallback | miss."""
        path = _key_path(key)
        cached = None
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
        meta = {"key": key, "url": url, "origin": None, "fetched_at": None, "error": None}

        if self.mode != "cache":
            try:
                data = fn()
                meta.update(origin="live", fetched_at=_now_iso())
                path.write_text(json.dumps({"key": key, "url": url, "fetched_at": meta["fetched_at"], "data": data}, ensure_ascii=False, indent=1), encoding="utf-8")
                self.stats["live"] += 1
                self.trace.append(meta)
                return data, meta
            except Exception as e:  # network down, timeout, rate limit, parse error...
                meta["error"] = f"{type(e).__name__}: {e}"[:160]
                if self.mode == "live":
                    meta["origin"] = "miss"
                    self.stats["miss"] += 1
                    self.trace.append(meta)
                    return None, meta

        if cached is not None:
            meta.update(origin="fallback" if self.mode != "cache" else "cache", fetched_at=cached.get("fetched_at"))
            self.stats["fallback" if self.mode != "cache" else "cache"] += 1
            self.trace.append(meta)
            return cached.get("data"), meta
        meta["origin"] = "miss"
        meta["error"] = meta["error"] or "not in cache"
        self.stats["miss"] += 1
        self.trace.append(meta)
        return None, meta

    # ------------------------------------------------------------------ Google News
    def news(self, query: str, window: str = "180d") -> tuple[list[dict] | None, dict]:
        q = f"{query} when:{window}" if window else query
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})

        def fetch():
            r = _get(url, accept_404=False)
            root = ET.fromstring(r.content)
            items = []
            for it in root.iter("item"):
                raw_title = html.unescape(it.findtext("title") or "").strip()
                src_el = it.find("source")
                source = (src_el.text or "").strip() if src_el is not None else ""
                source_url = src_el.get("url", "") if src_el is not None else ""
                title = raw_title
                if source and title.endswith(" - " + source):
                    title = title[: -len(" - " + source)].strip()
                pub = it.findtext("pubDate")
                try:
                    published = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat(timespec="seconds") if pub else None
                except Exception:
                    published = None
                items.append({"title": title, "source": source, "source_url": source_url, "url": it.findtext("link") or "", "published": published})
            return items[:60]

        return self._cached(f"news|{q}", url, fetch)

    # ------------------------------------------------------------------ Wikipedia / Wikidata
    def wiki_summary(self, title: str) -> tuple[dict | None, dict]:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))

        def fetch():
            r = _get(url)
            if r.status_code == 404:
                return {"missing": True}
            j = r.json()
            return {
                "type": j.get("type"),
                "title": j.get("title"),
                "description": j.get("description") or "",
                "extract": (j.get("extract") or "")[:900],
                "wikibase_item": j.get("wikibase_item"),
                "url": ((j.get("content_urls") or {}).get("desktop") or {}).get("page"),
            }

        return self._cached(f"wiki|{title.lower()}", url, fetch)

    def wiki_search(self, query: str, limit: int = 10) -> tuple[list[dict] | None, dict]:
        params = {"action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": limit, "prop": "description|pageprops", "ppprop": "wikibase_item", "format": "json", "formatversion": 2}
        url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)

        def fetch():
            j = _get(url, accept_404=False).json()
            pages = sorted((j.get("query") or {}).get("pages", []), key=lambda p: p.get("index", 99))
            return [{"title": p["title"], "description": p.get("description", ""), "wikibase_item": (p.get("pageprops") or {}).get("wikibase_item")} for p in pages]

        return self._cached(f"wikisearch|{query.lower()}", url, fetch)

    def wikidata_website(self, qid: str) -> tuple[str | None, dict]:
        url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({"action": "wbgetclaims", "entity": qid, "property": "P856", "format": "json"})

        def fetch():
            j = _get(url, accept_404=False).json()
            for c in (j.get("claims") or {}).get("P856", []):
                v = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
                if isinstance(v, str):
                    return v
            return ""

        return self._cached(f"wikidata-site|{qid}", url, fetch)

    # ------------------------------------------------------------------ company website
    def website(self, domain: str) -> tuple[dict | None, dict]:
        url = f"https://{domain}" if not domain.startswith("http") else domain

        def fetch():
            r = _get(url, accept_404=False)
            soup = BeautifulSoup(r.text[:600_000], "html.parser")
            title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]

            def meta(**attrs):
                el = soup.find("meta", attrs=attrs)
                return (el.get("content") or "").strip() if el else ""

            desc = meta(name="description") or meta(property="og:description") or meta(name="twitter:description")
            return {"url": url, "final_url": r.url, "title": title, "description": desc[:400]}

        return self._cached(f"site|{url.lower()}", url, fetch)

    # ------------------------------------------------------------------ job boards
    def greenhouse(self, slug: str):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

        def fetch():
            r = _get(url)
            if r.status_code == 404:
                return {"exists": False}
            jobs = r.json().get("jobs", [])
            board = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            name = board.json().get("name", "") if board.status_code == 200 else ""
            return {"exists": True, "board_name": name, "jobs": [
                {"title": (j.get("title") or "").strip(), "location": (j.get("location") or {}).get("name", ""), "updated": j.get("updated_at"), "url": j.get("absolute_url")}
                for j in jobs]}

        return self._cached(f"greenhouse|{slug}", url, fetch)

    def lever(self, slug: str):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

        def fetch():
            r = _get(url)
            if r.status_code == 404:
                return {"exists": False}
            out = []
            for j in r.json():
                ts = j.get("createdAt")
                out.append({"title": (j.get("text") or "").strip(), "location": (j.get("categories") or {}).get("location", ""),
                            "updated": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(timespec="seconds") if ts else None, "url": j.get("hostedUrl")})
            return {"exists": True, "board_name": "", "jobs": out}

        return self._cached(f"lever|{slug}", url, fetch)

    def ashby(self, slug: str):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

        def fetch():
            r = _get(url)
            if r.status_code == 404:
                return {"exists": False}
            jobs = r.json().get("jobs", [])
            if not jobs:
                return {"exists": False}
            return {"exists": True, "board_name": "", "jobs": [
                {"title": (j.get("title") or "").strip(), "location": j.get("location", ""), "updated": j.get("publishedAt"), "url": j.get("jobUrl")}
                for j in jobs]}

        return self._cached(f"ashby|{slug}", url, fetch)

    def job_boards(self, slugs: list[str]) -> list[tuple[str, str, dict | None, dict]]:
        """Probe every (provider, slug) pair in parallel. Returns [(provider, slug, data, meta)]."""
        tasks = [(p, s) for s in slugs for p in ("greenhouse", "lever", "ashby")]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [(p, s, ex.submit(getattr(self, p), s)) for p, s in tasks]
            out = []
            for p, s, f in futs:
                data, meta = f.result()
                out.append((p, s, data, meta))
        return out


def domain_of(url_or_domain: str) -> str:
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    host = urllib.parse.urlparse(s).hostname or ""
    return re.sub(r"^www\.", "", host)


def age_days(iso: str | None, as_of: datetime) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (as_of - d).total_seconds() / 86400)
    except Exception:
        return None


def parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

