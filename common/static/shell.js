/* Shared UI shell: top bar, "New run" (scenarios + custom input + live run view),
 * "Dashboard" (KPIs + run history), and a run detail route.
 *
 * Apps call Shell.init({...}) with:
 *   decisionTone:   { DECISION: 'good'|'warn'|'bad'|'info' }
 *   renderCustomInput(el, ctx)  -> optional; build a custom-input form. ctx.startRun(fetchPromise)
 *   renderResult(el, run)       -> render run.result (called when the run completes)
 *   kpis(runs)                  -> optional extra KPI tiles [{label, val, sub}]
 */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (sel, el = document) => el.querySelector(sel);
  const icon = { pending: "", running: "", done: "✓", warn: "!", fail: "✕", skipped: "–" };

  let CFG = null, META = null;
  let pollTimer = null, currentRunId = null, openStages = new Set(), lastRenderedStatus = null;
  let dashFilter = "ALL";

  function tone(decision) {
    if (!decision) return "";
    if (decision === "ERROR") return "bad";
    return (CFG.decisionTone || {})[decision] || "info";
  }
  function pill(decision, big) {
    if (!decision) return `<span class="pill">—</span>`;
    const label = (CFG.decisionLabel || {})[decision] || decision.replace(/_/g, " ");
    return `<span class="pill ${tone(decision)} ${big ? "big" : ""}">${esc(label)}</span>`;
  }
  function fmtMs(ms) { if (ms == null) return ""; return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`; }
  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function fmtVal(v) {
    if (v === null || v === undefined) return `<span class="muted">—</span>`;
    if (typeof v === "object") {
      let s = JSON.stringify(v, null, 2);
      if (s.length > 2500) s = s.slice(0, 2500) + "\n…";
      return `<pre>${esc(s)}</pre>`;
    }
    return esc(v);
  }

  // ------------------------------------------------------------------ layout
  function layout() {
    document.body.innerHTML = `
      <header class="topbar">
        <div class="brand">
          <div class="brand-mark">${esc(CFG.mark || "AI")}</div>
          <div style="min-width:0">
            <div class="brand-title">${esc(META.title)}</div>
            <div class="brand-sub">${esc(META.subtitle)}</div>
          </div>
        </div>
        <nav class="tabs">
          <button class="tab" data-route="new">New run</button>
          <button class="tab" data-route="runs">Dashboard</button>
        </nav>
        <div class="engine" title="Which engine makes the judgement calls in this process"><span class="dot ${META.llm ? "on" : ""}"></span>${esc(META.engine)}</div>
      </header>
      <main id="main"></main>`;
    document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => (location.hash = "#/" + b.dataset.route)));
  }

  function route() {
    stopPolling();
    const h = location.hash.replace(/^#\/?/, "") || "new";
    const [page, arg] = h.split("/");
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.route === page || (page === "run" && t.dataset.route === "runs")));
    if (page === "runs") return renderDashboard();
    if (page === "run" && arg) return renderRunPage(arg);
    return renderNew();
  }

  // ------------------------------------------------------------------ new run
  function renderNew() {
    const main = $("#main");
    main.innerHTML = `
      <div class="grid-new">
        <div>
          <div class="card">
            <div class="card-h"><h2>Prepared scenarios</h2><span class="muted small" style="margin-left:auto">${META.scenarios.length} inputs</span></div>
            <div class="card-b" id="scenarios"></div>
          </div>
          <div class="card" style="margin-top:16px" id="custom-card">
            <div class="card-h"><h2>${esc(CFG.customTitle || "Custom input")}</h2></div>
            <div class="card-b" id="custom"></div>
          </div>
        </div>
        <div class="card" id="live-card">
          <div class="card-b" id="live">
            <div class="empty"><div class="big">No run selected</div>Pick a prepared scenario or submit your own input — each stage will appear here as it executes.</div>
          </div>
        </div>
      </div>`;
    const sc = $("#scenarios");
    sc.innerHTML = META.scenarios.map((s) => `
      <div class="scenario">
        <div class="s-body">
          <div class="s-title">${esc(s.title)} <span class="tag ${s.kind === "happy" ? "happy" : "edge"}">${s.kind === "happy" ? "Happy path" : "Edge case"}</span></div>
          <div class="s-desc">${esc(s.description)}</div>
        </div>
        <button class="btn primary sm" data-sid="${esc(s.id)}">Run</button>
      </div>`).join("");
    sc.querySelectorAll("button[data-sid]").forEach((b) => (b.onclick = () => startRun(fetch(`/api/scenarios/${b.dataset.sid}/run`, { method: "POST" }))));
    if (CFG.renderCustomInput) CFG.renderCustomInput($("#custom"), { startRun, meta: META });
    else $("#custom-card").classList.add("hidden");
    if (currentRunId) showRun(currentRunId, $("#live"));
  }

  async function startRun(fetchPromise) {
    try {
      const res = await fetchPromise;
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || res.statusText);
      currentRunId = body.run_id;
      openStages = new Set();
      if (!$("#live")) { location.hash = "#/new"; return; }
      showRun(body.run_id, $("#live"));
      $("#live-card").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      alert("Could not start run: " + e.message);
    }
  }

  // ------------------------------------------------------------------ live run view
  function showRun(runId, el) {
    stopPolling();
    lastRenderedStatus = null;
    el.innerHTML = `
      <div class="run-head" id="rh"></div>
      <div class="split" style="margin-top:16px">
        <div><p class="eyebrow">Process stages</p><ol class="timeline" id="tl"></ol></div>
        <div><p class="eyebrow">Outcome</p><div id="result"><div class="muted small">Waiting for the process to finish…</div></div></div>
      </div>`;
    const tick = async () => {
      let run;
      try { run = await (await fetch(`/api/runs/${runId}`)).json(); } catch { return; }
      renderRunHead(run, $("#rh", el));
      renderTimeline(run, $("#tl", el));
      if (run.status !== "running" && lastRenderedStatus !== run.status) {
        lastRenderedStatus = run.status;
        const r = $("#result", el);
        if (run.status === "error") {
          r.innerHTML = `<div class="result-banner bad"><div><div class="rb-title">Process error</div><div class="rb-sub">${esc(run.summary)}</div></div></div>`;
        } else {
          try { CFG.renderResult(r, run); } catch (e) { r.innerHTML = `<pre>${esc(e.stack)}</pre>`; }
        }
      }
      if (run.status !== "running") stopPolling();
    };
    tick();
    pollTimer = setInterval(tick, 350);
  }

  function renderRunHead(run, el) {
    const status = run.status === "running" ? `<span class="pill info">● Running</span>` : run.status === "error" ? `<span class="pill bad">Error</span>` : `<span class="pill">Completed in ${fmtMs(run.duration_ms)}</span>`;
    el.innerHTML = `
      <div style="min-width:0">
        <div class="label">${esc(run.label)}</div>
        <div class="muted small">Run <code>${esc(run.id)}</code> · ${esc(fmtTime(run.created_at))} · scenario: ${esc(run.scenario)}</div>
      </div>
      <div style="margin-left:auto; display:flex; gap:8px; align-items:center">${status}${run.decision ? pill(run.decision, true) : ""}</div>`;
  }

  function renderTimeline(run, el) {
    el.innerHTML = run.stages.map((s, i) => {
      const autoOpen = s.status === "running";
      const open = openStages.has(s.key) ? true : openStages.has("!" + s.key) ? false : autoOpen;
      const logs = (s.logs || []).map((l) => `<div class="${esc(l.level)}"><span class="t">${l.t.toFixed(2)}s</span>${esc(l.msg)}</div>`).join("");
      const data = Object.entries(s.data || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${fmtVal(v)}</td></tr>`).join("");
      const hasDetail = logs || data;
      return `
        <li class="stage ${esc(s.status)}" data-key="${esc(s.key)}">
          <div class="ic">${s.status === "pending" ? i + 1 : icon[s.status] || ""}</div>
          <div class="s-row"><span class="s-name">${esc(s.title)}</span><span class="s-ms">${s.status === "running" ? "running…" : fmtMs(s.ms)}</span>${hasDetail ? `<span class="muted small">${open ? "▾" : "▸"}</span>` : ""}</div>
          ${s.note ? `<div class="s-note">${esc(s.note)}</div>` : ""}
          ${hasDetail ? `<div class="s-detail ${open ? "" : "hidden"}">${logs ? `<div class="log">${logs}</div>` : ""}${data ? `<table class="kv">${data}</table>` : ""}</div>` : ""}
        </li>`;
    }).join("");
    el.querySelectorAll(".s-row").forEach((row) => (row.onclick = () => {
      const li = row.parentElement, key = li.dataset.key, det = $(".s-detail", li);
      if (!det) return;
      const nowOpen = det.classList.contains("hidden");
      openStages.delete(key); openStages.delete("!" + key);
      openStages.add(nowOpen ? key : "!" + key);
      det.classList.toggle("hidden", !nowOpen);
    }));
  }

  function renderRunPage(runId) {
    const main = $("#main");
    main.innerHTML = `<div style="margin-bottom:12px"><a href="#/runs">← Back to dashboard</a></div><div class="card"><div class="card-b" id="live"></div></div>`;
    openStages = new Set();
    showRun(runId, $("#live"));
  }

  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

  // ------------------------------------------------------------------ dashboard
  async function renderDashboard() {
    const main = $("#main");
    const runs = await (await fetch("/api/runs")).json();
    const done = runs.filter((r) => r.status !== "running");
    const counts = {};
    done.forEach((r) => (counts[r.decision] = (counts[r.decision] || 0) + 1));
    const avg = done.length ? done.reduce((a, r) => a + (r.duration_ms || 0), 0) / done.length : 0;
    const order = Object.keys(CFG.decisionTone || {});
    const decisions = [...order.filter((d) => counts[d]), ...Object.keys(counts).filter((d) => !order.includes(d))];
    const colorOf = (d) => ({ good: "var(--good)", warn: "#f59e0b", bad: "var(--bad)", info: "var(--info)" }[tone(d)] || "var(--faint)");
    const extra = CFG.kpis ? CFG.kpis(done) : [];

    main.innerHTML = `
      <div class="kpis">
        <div class="kpi"><div class="k-label">Total runs</div><div class="k-val">${runs.length}</div><div class="k-sub">${runs.length - done.length} running now</div></div>
        <div class="kpi"><div class="k-label">Outcome mix</div>
          <div class="bar">${decisions.map((d) => `<span title="${esc(d)}: ${counts[d]}" style="width:${(100 * counts[d]) / done.length}%;background:${colorOf(d)}"></span>`).join("")}</div>
          <div class="k-sub" style="margin-top:6px">${decisions.map((d) => `${esc((CFG.decisionLabel || {})[d] || d)} ${counts[d]}`).join(" · ") || "No runs yet"}</div></div>
        <div class="kpi"><div class="k-label">Avg. run time</div><div class="k-val">${fmtMs(Math.round(avg)) || "—"}</div><div class="k-sub">input → decision</div></div>
        ${extra.map((k) => `<div class="kpi"><div class="k-label">${esc(k.label)}</div><div class="k-val">${esc(k.val)}</div><div class="k-sub">${esc(k.sub || "")}</div></div>`).join("")}
      </div>
      <div class="card">
        <div class="card-h">
          <h2>Run history</h2>
          <div class="filters" style="margin-left:12px">
            ${["ALL", ...decisions].map((d) => `<button class="chip ${dashFilter === d ? "active" : ""}" data-f="${esc(d)}">${d === "ALL" ? "All" : esc((CFG.decisionLabel || {})[d] || d)}</button>`).join("")}
          </div>
          <div style="margin-left:auto; display:flex; gap:8px">
            <button class="btn sm" id="refresh">Refresh</button>
            <button class="btn sm" id="clear">Clear history</button>
          </div>
        </div>
        <div style="overflow-x:auto">
        <table class="t">
          <thead><tr><th>When</th><th>Input</th><th>Scenario</th><th>Outcome</th><th>Why</th><th>Stages</th><th class="num">Time</th></tr></thead>
          <tbody>${runs.filter((r) => dashFilter === "ALL" || r.decision === dashFilter).map((r) => `
            <tr class="click" data-id="${esc(r.id)}">
              <td class="small muted" style="white-space:nowrap">${esc(fmtTime(r.created_at))}</td>
              <td><b>${esc(r.label)}</b></td>
              <td class="small muted">${esc(r.scenario)}</td>
              <td>${r.status === "running" ? `<span class="pill info">Running</span>` : pill(r.decision)}</td>
              <td class="small"><div class="ellipsis" title="${esc(r.summary)}">${esc(r.summary || "")}</div></td>
              <td><span class="mini">${r.stages.map((s) => `<i class="${esc(s.status)}" title="${esc(s.title)}: ${esc(s.status)}"></i>`).join("")}</span></td>
              <td class="num small">${fmtMs(r.duration_ms)}</td>
            </tr>`).join("") || `<tr><td colspan="7" class="empty">No runs yet — start one from “New run”.</td></tr>`}
          </tbody>
        </table></div>
      </div>`;
    main.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = "#/run/" + tr.dataset.id)));
    main.querySelectorAll(".chip").forEach((c) => (c.onclick = () => { dashFilter = c.dataset.f; renderDashboard(); }));
    $("#refresh").onclick = renderDashboard;
    $("#clear").onclick = async () => { if (confirm("Delete all run history?")) { await fetch("/api/runs", { method: "DELETE" }); renderDashboard(); } };
    if (runs.some((r) => r.status === "running")) pollTimer = setTimeout(() => location.hash.startsWith("#/runs") && renderDashboard(), 1500);
  }

  // ------------------------------------------------------------------ helpers exported to apps
  function checks(list) {
    const sym = { pass: "✓", fail: "✕", warn: "!", na: "–", info: "i" };
    return list.map((c) => `
      <div class="check"><div class="ci ${esc(c.status)}">${sym[c.status] || "•"}</div>
      <div><div class="cn">${esc(c.name)}</div><div class="cd">${esc(c.detail || "")}</div></div></div>`).join("");
  }
  function money(v, cur) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (cur ? cur + " " : "") + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  window.Shell = {
    esc, pill, checks, money, fmtMs, tone,
    async init(cfg) {
      CFG = cfg;
      META = await (await fetch("/api/meta")).json();
      document.title = META.title;
      layout();
      window.addEventListener("hashchange", route);
      route();
    },
  };
})();
