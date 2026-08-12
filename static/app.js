"use strict";
/* TLF Review Platform — single-page frontend. */

const pdfjsLib = window.pdfjsLib;
pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.mjs";

// Human-settable review statuses. "Auto-approved" is set by the system (a clean AI run / import) and is never offered here — a reviewer taking ownership picks "Manually approved".
const STATUSES = ["Not Reviewed", "In Progress", "Manually approved", "Needs Revision"];

const state = {
  project: null,      // full project object (with outputs)
  tab: "toc",
  output: null,       // current output in TLF viewer
  pdf: null,          // loaded pdfjs document for current output
  pageNum: 1,
  zoom: 1,            // user zoom multiplier on top of fit-to-width
  filter: "",
  tool: "pan",        // pan | highlight | rect | freehand | eraser
  annoColor: localStorage.getItem("tlf_anno_color") || "#ffd54a",
  annos: [],          // annotations for the current output
  bmGroup: localStorage.getItem("tlf_bm_group") || "file",  // left-panel grouping: "file" | "table"
  bmOpen: new Set(),  // expanded group keys in the bookmark tree (per session)
  aiFindingCounts: {},// output_id → count of PENDING (unactioned) findings
};

const $ = (sel, root = document) => root.querySelector(sel);
const app = () => $("#app");

// Comments are no longer attributed to a named reviewer; findings-actions still send an author, so this stays as a fixed label rather than a per-user name.
function reviewer() { return "Reviewer"; }

// Light / dark theme toggle. The saved (or OS-preferred) theme is already applied to <html> in the page <head> before first paint; here we only sync the toggle button glyph and flip + persist the choice on click.
(function initTheme() {
  const paint = btn => { btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙"; };
  const wire = () => {
    const btn = $("#themeToggle"); if (!btn) return;
    paint(btn);
    btn.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("tlf_theme", next); } catch (e) {}
      paint(btn);
    });
  };
  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);
})();

// ---------------------------------------------------------------- helpers
// Global "working…" indicator: a thin indeterminate bar pinned to the top of the viewport, shown whenever one or more tracked requests are in flight. Ref-counted so overlapping calls (e.g. loadFindings + loadLastRun) don't flicker it off early. The element is created lazily, so no markup change is needed. Pass {quiet:true} to opt a request OUT — the run-progress poller does, so it doesn't strobe the bar every 1.2s.
let _busyN = 0;
function setBusy(on) {
  _busyN = Math.max(0, _busyN + (on ? 1 : -1));
  const active = _busyN > 0;
  const bar = _busyBar();
  if (bar) bar.classList.toggle("on", active);
  document.documentElement.classList.toggle("busy", active);
}
function _busyBar() {
  let b = document.getElementById("busybar");
  if (!b && document.body) { b = document.createElement("div"); b.id = "busybar"; document.body.appendChild(b); }
  return b;
}
async function getJSON(url, opts) {
  const quiet = !!(opts && opts.quiet);
  if (!quiet) setBusy(true);
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    return await r.json();
  } finally { if (!quiet) setBusy(false); }
}
async function postForm(url, data) {
  const fd = data instanceof FormData ? data : Object.entries(data).reduce((f, [k, v]) => (f.append(k, v), f), new FormData());
  setBusy(true);
  try {
    const r = await fetch(url, { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    return await r.json();
  } finally { setBusy(false); }
}
async function del(url) {
  setBusy(true);
  try {
    const r = await fetch(url, { method: "DELETE" });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  } finally { setBusy(false); }
}
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
function esc(s) { return (s ?? "").toString().replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function safeInt(value, fallback = 0) {
  const n = Number(value);
  return Number.isSafeInteger(n) && n >= 0 ? n : fallback;
}
function displayInt(value, fallback = "?") {
  const n = safeInt(value, -1);
  return n >= 0 ? String(n) : fallback;
}
function toast(msg, ms = 2200) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
const STATUS_CLASSES = Object.freeze({
  "Not Reviewed": "st-NotReviewed",
  "In Progress": "st-InProgress",
  "Manually approved": "st-Manuallyapproved",
  "Auto-approved": "st-Auto-approved",
  "Needs Revision": "st-NeedsRevision",
  "Approved": "st-Manuallyapproved", // legacy database value
});
const FINDING_STATE_CLASSES = Object.freeze({
  pending: "state-pending",
  posted: "state-posted",
  rejected: "state-rejected",
  resolved: "state-resolved",
});
const stClass = s => STATUS_CLASSES[s] || "st-NotReviewed";
const findingStateClass = s => FINDING_STATE_CLASSES[s] || "state-pending";

// Split a table title into its descriptive name and the trailing analysis-population parenthetical, e.g. "… Studies (Safety Analysis Set)" → {name, population}. Only the LAST parenthetical is treated as a population, and only if it reads like one (so "(Narrow)" mid-title stays part of the name).
function splitTitle(title) {
  const t = (title || "").trim();
  const m = t.match(/^(.*?)\s*\(([^()]*)\)\s*$/);
  if (m && /set|population|analys|subjects|itt|per[- ]protocol|safety|efficacy/i.test(m[2])) {
    return { name: m[1].trim(), population: m[2].trim() };
  }
  return { name: t, population: "" };
}

// ---------------------------------------------------------------- routing
$("#homeBtn").addEventListener("click", goHome);
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  setTab(b.dataset.tab);
});
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  renderTab();
}
function goHome() {
  state.project = null;
  $("#tabs").classList.add("hidden");
  $("#homeBtn").classList.add("hidden");
  $("#projMeta").textContent = "";
  renderHome();
}

// ---------------------------------------------------------------- home
async function renderHome() {
  const projects = await getJSON("/api/projects").catch(() => []);
  const compounds = [...new Set(projects.map(p => p.compound).filter(Boolean))];
  const studiesByCompound = {};
  projects.forEach(p => { if (p.study) (studiesByCompound[p.compound] ||= new Set()).add(p.study); });

  app().innerHTML = `<div class="home-wrap">
    <div class="card create-card">
      <h2>Create Project</h2>
      <form id="newProj">
        <div class="field"><label>Compound <span class="req">*</span></label>
          <input name="compound" list="compoundList" placeholder="Select or type a compound…" autocomplete="off" required>
          <datalist id="compoundList">${compounds.map(c => `<option value="${esc(c)}">`).join("")}</datalist>
        </div>
        <div class="field"><label>Study <span class="opt-tag">optional</span></label>
          <input name="study" id="studyInput" list="studyList" placeholder="Optional — select or type a study…" autocomplete="off">
          <datalist id="studyList"></datalist>
        </div>
        <div class="field"><label>Project name <span class="req">*</span></label>
          <input name="name" placeholder="e.g. Topline" required></div>

        <div class="field"><label>TLF outputs (.pdf) <span class="req">*</span></label>
          <div id="dropzone" class="dropzone">
            <input type="file" id="deliveryInput" accept="application/pdf" multiple hidden>
            <div class="dz-inner">
              <span class="dz-icon">📄</span>
              <span class="dz-text">Drop PDF files here, or <button type="button" class="link-btn" id="browseBtn">browse</button></span>
            </div>
          </div>
          <ul class="staged-list" id="stagedList"></ul></div>

        <details class="opt-docs">
          <summary>Supplementary documents <span class="opt-tag">optional</span></summary>
          <p class="sub">If you upload them, the AI uses these as ground truth for SAP-aligned checks
            (analysis dataset, CI level/method, stratification, MMRM, etc.) and for visit-schedule context.
            The protocol is much longer than the SAP — uploading both is fine; Anthropic prompt caching keeps
            the per-run cost reasonable.</p>
          <div class="field"><label>TOC workbook (.xlsx)</label>
            <input type="file" name="toc" accept=".xlsx">
            <span class="hint">Not required — outputs are indexed from the PDF bookmarks. Attach a TOC only if you have one.</span></div>
          <div class="field"><label>Statistical Analysis Plan / SAP (.pdf or .docx)</label>
            <input type="file" name="sap" accept=".pdf,.docx"></div>
          <div class="field"><label>Protocol (.pdf or .docx)</label>
            <input type="file" name="protocol" accept=".pdf,.docx"></div>
          <div class="field"><label>Statistical Programming Plan / SPP (.pdf or .docx)</label>
            <input type="file" name="spp" accept=".pdf,.docx"></div>
        </details>

        <button class="btn create-btn" type="submit" id="createBtn">Create</button>
      </form>
    </div>

    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:.6rem">
        <h2>View Projects</h2>
        <button type="button" class="btn ghost sm" id="importBtn" title="Import projects from .zip files">Import</button>
      </div>
      <input type="file" id="importInput" accept=".zip" multiple hidden>
      <ul class="proj-list" id="projList"></ul>
    </div>
  </div>`;

  // Study is optional; its datalist suggestions still track the chosen compound.
  const compoundInp = $('[name="compound"]'), studyInp = $("#studyInput"), studyList = $("#studyList");
  const syncStudy = () => {
    const c = compoundInp.value.trim();
    const studies = c && studiesByCompound[c] ? [...studiesByCompound[c]] : [];
    studyList.innerHTML = studies.map(s => `<option value="${esc(s)}">`).join("");
  };
  compoundInp.addEventListener("input", syncStudy);
  syncStudy();

  // --- Staged delivery uploads: add one at a time, replace, delete, drag-drop --- Files are held in memory until Create; the form is then submitted as one multipart POST with each staged file appended under the "delivery" field the backend already expects (repeated part). No new endpoint needed.
  const staged = [];
  const dz = $("#dropzone"), fileInput = $("#deliveryInput"), stagedList = $("#stagedList");
  const replaceInput = document.createElement("input");
  replaceInput.type = "file"; replaceInput.accept = "application/pdf";
  let replaceIdx = -1;
  let mainFile = null;   // the File the user marked as the main (reviewed) edition; rest are comparison
  const isPdf = f => f.type === "application/pdf" || /\.pdf$/i.test(f.name);
  const keyOf = f => `${f.name}:${f.size}`;
  const fmtSize = b => b < 1024 ? `${b} B` : b < 1048576 ? `${Math.round(b / 1024)} KB` : `${(b / 1048576).toFixed(1)} MB`;

  function renderStaged() {
    stagedList.innerHTML = "";
    const multi = staged.length > 1;   // "main document" only matters when there's something to compare
    staged.forEach((f, i) => {
      const isMain = f === mainFile;
      // The main-document control sits between Replace and ✕. It shows only with 2+ files (a lone upload is implicitly the one under review). One main at a time: the chosen row is highlighted with a MAIN badge and its button flips to "De-select".
      const mainTip = isMain
        ? "Clear the main-document choice"
        : "Mark this as the main document to review; the others become comparison editions only";
      const mainBtn = multi
        ? `<button type="button" class="btn ${isMain ? "" : "ghost "}sm" data-act="main" title="${mainTip}">${isMain ? "De-select" : "Set as main"}</button>`
        : "";
      const li = el(`<li class="staged-item${isMain ? " is-main" : ""}">
        <span class="si-ic">📄</span>
        <span class="si-name" title="${esc(f.name)}">${esc(f.name)}</span>
        ${isMain ? '<span class="main-badge">MAIN</span>' : ""}
        <span class="si-size">${fmtSize(f.size)}</span>
        <span class="si-actions">
          <button type="button" class="btn ghost sm" data-act="replace">Replace</button>
          ${mainBtn}
          <button type="button" class="btn ghost sm" data-act="delete" aria-label="Remove file">✕</button>
        </span></li>`);
      li.querySelector('[data-act="replace"]').addEventListener("click", () => { replaceIdx = i; replaceInput.click(); });
      const mb = li.querySelector('[data-act="main"]');
      if (mb) mb.addEventListener("click", () => { mainFile = isMain ? null : f; renderStaged(); });
      li.querySelector('[data-act="delete"]').addEventListener("click", () => {
        if (staged[i] === mainFile) mainFile = null;   // dropped the main → clear the choice
        staged.splice(i, 1); renderStaged();
      });
      stagedList.appendChild(li);
    });
    dz.classList.toggle("has-files", staged.length > 0);
  }
  function addFiles(list) {
    const have = new Set(staged.map(keyOf));
    let added = 0, skipped = 0;
    for (const f of list) {
      if (!isPdf(f)) { skipped++; continue; }
      if (have.has(keyOf(f))) continue;   // ignore an exact duplicate (same name + size)
      have.add(keyOf(f)); staged.push(f); added++;
    }
    if (skipped) toast(`Skipped ${skipped} non-PDF file${skipped === 1 ? "" : "s"} — TLF outputs must be PDFs`, 3500);
    if (added) renderStaged();
  }

  $("#browseBtn").addEventListener("click", e => { e.stopPropagation(); fileInput.click(); });
  dz.addEventListener("click", e => { if (!e.target.closest("button")) fileInput.click(); });
  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  replaceInput.addEventListener("change", () => {
    const f = replaceInput.files[0];
    if (f && replaceIdx >= 0) {
      if (!isPdf(f)) toast("Replacement must be a PDF", 3000);
      else {
        if (staged[replaceIdx] === mainFile) mainFile = f;   // keep the main designation on the replacement
        staged[replaceIdx] = f; renderStaged();
      }
    }
    replaceInput.value = ""; replaceIdx = -1;
  });
  let dragDepth = 0;   // counter so dragenter/leave over child nodes doesn't flicker the highlight
  dz.addEventListener("dragenter", e => { e.preventDefault(); dragDepth++; dz.classList.add("dragover"); });
  dz.addEventListener("dragover", e => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = "copy"; });
  dz.addEventListener("dragleave", e => { e.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; dz.classList.remove("dragover"); } });
  dz.addEventListener("drop", e => {
    e.preventDefault(); dragDepth = 0; dz.classList.remove("dragover");
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });

  $("#newProj").addEventListener("submit", async e => {
    e.preventDefault();
    if (!staged.length) { toast("Add at least one TLF PDF before creating", 3000); return; }
    if (staged.length > 1 && !mainFile) {
      toast("Choose which document is the main one to check — use “Set as main”", 3800); return;
    }
    const mainIdx = staged.length > 1 ? staged.indexOf(mainFile) : 0;
    const form = e.target, val = nm => (form.elements[nm] ? form.elements[nm].value : "");
    const btn = $("#createBtn"); btn.disabled = true; btn.innerHTML = 'Indexing… <span class="spin"></span>';
    try {
      const fd = new FormData();
      fd.append("compound", val("compound"));
      fd.append("study", val("study"));
      fd.append("name", val("name"));
      for (const nm of ["toc", "sap", "protocol", "spp"]) {   // optional single reference docs
        const inp = form.elements[nm];
        if (inp && inp.files && inp.files[0]) fd.append(nm, inp.files[0]);
      }
      staged.forEach(f => fd.append("delivery", f));          // field name the backend expects
      fd.append("main_index", mainIdx);                       // which delivery doc is the reviewed edition
      const res = await postForm("/api/projects", fd);
      toast(`Project created · ${res.n_outputs} outputs indexed`);
      await openProject(res.id);
    } catch (err) { toast("Create failed: " + err.message, 4000); btn.disabled = false; btn.textContent = "Create"; }
  });

  const ul = $("#projList");
  ul.innerHTML = projects.length ? "" : '<li class="muted">No projects yet — create one first.</li>';
  projects.forEach(p => {
    const projectId = safeInt(p.id);
    const nOutputs = safeInt(p.n_outputs);
    const meta = [p.compound, p.study, p.edition_label].filter(Boolean).map(esc).join(" · ");
    const li = el(`<li><div><div><b>${esc(p.name)}</b></div>
      <div class="meta">${meta}${meta ? " · " : ""}${nOutputs} outputs</div></div>
      <span style="display:inline-flex;gap:.3rem;flex:0 0 auto">
        <button class="btn ghost sm" data-exp="${projectId}" title="Export this project to a shareable file">Export</button>
        <button class="btn ghost sm" data-del="${projectId}">Delete</button>
      </span></li>`);
    li.addEventListener("click", ev => { if (ev.target.dataset.del || ev.target.dataset.exp) return; openProject(projectId); });
    li.querySelector("[data-exp]").addEventListener("click", ev => {
      ev.stopPropagation();
      location.href = "/api/projects/" + projectId + "/export/project.zip";
    });
    li.querySelector("[data-del]").addEventListener("click", async ev => {
      ev.stopPropagation();
      if (!confirm(`Delete project "${p.name}"?`)) return;
      await del("/api/projects/" + projectId); renderHome();
    });
    ul.appendChild(li);
  });

  // --- Import one or more project bundles shared by teammates (*.zip) ---
  const importBtn = $("#importBtn"), importInput = $("#importInput");
  importBtn.addEventListener("click", () => importInput.click());
  importInput.addEventListener("change", async () => {
    const files = [...importInput.files];
    importInput.value = "";            // reset so the same file(s) can be re-picked
    if (!files.length) return;
    let ok = 0, warnings = 0, lastId = null;
    const failed = [];
    for (const f of files) {           // sequential: conflict prompts are modal
      try {
        const res = await importProject(f, "ask");
        ok++; lastId = res.id; warnings += (res.warnings || []).length;
      } catch (err) {
        failed.push(`${f.name}: ${err.message}`);
      }
    }
    if (failed.length) toast(`Import failed — ${failed.join("; ")}`, 5000);
    if (ok === 1 && files.length === 1) {
      toast(`Project imported${warnings ? ` (${warnings} warning${warnings === 1 ? "" : "s"})` : ""}`);
      await openProject(lastId);       // single file → jump straight into it
    } else if (ok) {
      toast(`Imported ${ok} project${ok === 1 ? "" : "s"}`
            + `${warnings ? ` (${warnings} warning${warnings === 1 ? "" : "s"})` : ""}`, 3500);
      renderHome();                    // multiple → stay on the list and refresh
    }
  });
}

// Import one shared project bundle. On a name collision the server returns a {conflict} flag (nothing written); ask whether to replace the existing project or add a separate copy, then re-POST with the chosen mode. Returns the import result; the caller handles toasts/navigation so it can batch multiple files.
async function importProject(file, mode) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("mode", mode);
  const res = await postForm("/api/projects/import", fd);
  if (res.conflict) {
    const replace = confirm(`A project "${res.label}" already exists.\n\n` +
      `OK = replace it (its current review will be deleted).\n` +
      `Cancel = add this as a separate copy.`);
    return importProject(file, replace ? "replace" : "new");
  }
  return res;
}

// ---------------------------------------------------------------- project
// The "main" (reviewed) edition — mirrors backend runner._pick_current_prior: the highest-edition delivery document, which is the user-marked main when one was chosen at creation (the others become role='prior'). Used to default the TLF view to the reviewed edition instead of whichever file happens to sort first by document id / filename.
function mainDocId(project) {
  const mains = (project.documents || []).filter(d => d.role === "delivery");
  if (!mains.length) return null;
  mains.sort((a, b) => String(b.edition || "").localeCompare(String(a.edition || "")));
  return mains[0].id;
}

// The reviewable outputs are the tables in the MAIN document only. Comparison (prior) editions are indexed so cross-edition checks can run, but they are not themselves review targets, so they are excluded from the output count, the TOC, the dashboard totals and the TLF bookmark tree / Prev-Next. Every edition stays in state.project.outputs so a finding on any edition can still be resolved by output_id; only the *listed/counted* set is narrowed.
function reviewableOutputs(project) {
  const outs = project.outputs || [];
  const mid = mainDocId(project);
  if (mid == null) return outs;
  const main = outs.filter(o => o.document_id === mid);
  return main.length ? main : outs;   // fallback: never blank the worklist
}

async function openProject(pid) {
  state.project = await getJSON("/api/projects/" + pid);
  const outs = state.project.outputs || [];
  const mid = mainDocId(state.project);
  // Outputs are ordered by (document_id, seq), so the first match is the main doc's first table.
  state.output = (mid != null && outs.find(o => o.document_id === mid)) || outs[0] || null;
  $("#tabs").classList.remove("hidden");
  $("#homeBtn").classList.remove("hidden");
  const p = state.project;
  $("#projMeta").textContent = [p.compound, p.study, p.name].filter(Boolean).join(" · ")
    + (p.edition_label ? ` · ${p.edition_label}` : "");
  setTab("dashboard");
}

function renderTab() {
  if (!state.project) return renderHome();
  ({ dashboard: renderDashboard, toc: renderTOC, tlf: renderTLF, ai: renderAI, comments: renderComments }[state.tab] || renderTOC)();
}

// ---------------------------------------------------------------- Overview tab
// A colourful, low-detail summary of where the review stands. Everything is aggregated client-side from endpoints that already exist; numbers wear ink, only glyph chips / donut ring / bar segments / dots carry colour. Mirrors renderAI's shell-first pattern so a fetch that lands after the user switches tabs can't clobber the new view.
const pct = (n, d) => (d ? Math.round((100 * n) / d) : 0);

// Build a conic-gradient ring from [{val,color}]; forces the final stop to 360deg so rounding can't leave a seam, and paints a neutral ring when empty.
function donutStyle(segs) {
  const total = segs.reduce((s, x) => s + x.val, 0);
  if (!total) return "background: var(--line);";
  const live = segs.filter(s => s.val > 0);
  let acc = 0;
  const stops = live.map((s, i) => {
    const start = (acc / total) * 360;
    acc += s.val;
    const end = i === live.length - 1 ? 360 : (acc / total) * 360;
    return `${s.color} ${start.toFixed(2)}deg ${end.toFixed(2)}deg`;
  });
  return `background: conic-gradient(${stops.join(", ")});`;
}

async function renderDashboard() {
  const p = state.project, pid = p.id, outs = reviewableOutputs(p);

  app().innerHTML = `<div class="dash">
    <div class="dash-hero">
      <div>
        <h2 class="dash-hero-title">${esc(p.name || "Overview")}</h2>
        <p class="dash-hero-sub">${esc([p.compound, p.study].filter(Boolean).join(" · ")) || "Review overview"}</p>
      </div>
      <span class="dash-hero-chip" id="dashRun">…</span>
    </div>

    <div class="dash-kpis">
      <div class="card dash-kpi is-green" data-goto="toc">
        <div class="dash-kpi-ico">📋</div>
        <div class="dash-kpi-body">
          <div class="dash-kpi-num" id="kReview">…</div>
          <div class="dash-kpi-lbl">Review progress</div>
          <div class="dash-kpi-sub" id="kReviewSub"></div>
        </div>
      </div>
      <div class="card dash-kpi is-indigo" data-goto="ai">
        <div class="dash-kpi-ico">🔍</div>
        <div class="dash-kpi-body">
          <div class="dash-kpi-num" id="kFind">…</div>
          <div class="dash-kpi-lbl">AI findings</div>
          <div class="dash-kpi-sub" id="kFindSub"></div>
        </div>
      </div>
      <div class="card dash-kpi is-slate" data-goto="comments">
        <div class="dash-kpi-ico">💬</div>
        <div class="dash-kpi-body">
          <div class="dash-kpi-num" id="kCmt">…</div>
          <div class="dash-kpi-lbl">Comments</div>
          <div class="dash-kpi-sub" id="kCmtSub"></div>
        </div>
      </div>
    </div>

    <div class="dash-panels">
      <div class="card dash-panel"><h3>Review status</h3><div id="dashReview"></div></div>
      <div class="card dash-panel"><h3>AI review</h3><div id="dashFindings"></div></div>
      <div class="card dash-panel"><h3>Comments</h3><div id="dashComments"></div></div>
    </div>
  </div>`;

  app().querySelectorAll(".dash-kpi[data-goto]").forEach(c => {
    c.style.cursor = "pointer";
    c.onclick = () => setTab(c.dataset.goto);
  });

  const [findings, comments, lastRun] = await Promise.all([
    getJSON(`/api/projects/${pid}/findings`).catch(() => []),
    getJSON(`/api/projects/${pid}/comments`).catch(() => []),
    getJSON(`/api/projects/${pid}/ai-last-run`).catch(() => ({ none: true })),
  ]);
  if (state.tab !== "dashboard") return;   // user switched away mid-fetch

  // ---- aggregate -----------------------------------------------------------
  const STAT = ["Manually approved", "Auto-approved", "In Progress", "Needs Revision", "Not Reviewed"];
  const by = { "Manually approved": 0, "Auto-approved": 0, "In Progress": 0, "Needs Revision": 0, "Not Reviewed": 0 };
  outs.forEach(o => {
    const s = o.status === "Approved" ? "Manually approved" : o.status;   // fold any legacy status
    by[STAT.includes(s) ? s : "Not Reviewed"]++;
  });
  // Review progress counts BOTH approval kinds (human + AI) as approved.
  const total = outs.length, approved = by["Manually approved"] + by["Auto-approved"];

  const fTotal = findings.length;
  const open = findings.filter(f => f.state === "pending").length;

  const threads = comments.filter(c => c.parent_id == null);   // top-level threads only
  const cResolved = threads.filter(c => c.resolved).length;
  const cTotal = threads.length, cOpen = cTotal - cResolved;

  // ---- hero chip -----------------------------------------------------------
  if (lastRun.none) {
    $("#dashRun").innerHTML = `<span class="muted">AI review not run yet</span>`;
  } else {
    const s = lastRun.summary || {};
    const ok = s.status
      ? s.status === "succeeded" && s.review_complete === true
      : !s.error && (!s.errors || s.errors.length === 0);
    const runLabel = ok ? "OK" : (s.status || "errors");
    $("#dashRun").innerHTML = `Last AI review ${esc(timeAgo(lastRun.started_at))} `
      + `<span class="run-pill ${ok ? "ok" : "err"}">${esc(runLabel)}</span>`;
  }

  // ---- KPIs ----------------------------------------------------------------
  $("#kReview").textContent = `${pct(approved, total)}%`;
  $("#kReviewSub").textContent = `${approved} of ${total} approved`;
  $("#kFind").textContent = fTotal;
  $("#kFindSub").textContent = fTotal ? `${open} open` : (lastRun.none ? "not run yet" : "no issues found");
  $("#kCmt").textContent = cTotal;
  $("#kCmtSub").textContent = `${cOpen} open`;

  // ---- Review status panel: output-status donut + legend -------------------
  const revSegs = [
    { key: "Manually approved", color: "var(--minor)" },
    { key: "Auto-approved", color: "var(--auto)" },
    { key: "In Progress", color: "var(--accent)" },
    { key: "Needs Revision", color: "var(--crit)" },
    { key: "Not Reviewed", color: "var(--muted)" },
  ];
  $("#dashReview").innerHTML = `<div class="dash-donut-wrap">
    <div class="dash-donut" style="${donutStyle(revSegs.map(s => ({ val: by[s.key], color: s.color })))}">
      <div class="dash-donut-center">
        <div class="dash-donut-pct">${total ? pct(approved, total) + "%" : "—"}</div>
        <div class="dash-donut-cap">approved</div>
      </div>
    </div>
    <ul class="dash-legend">
      ${revSegs.map(s => `<li class="dash-legend-item">
        <span class="dash-dot" style="background:${s.color}"></span>
        <span class="dash-legend-lbl">${esc(s.key)}</span>
        <span class="dash-legend-ct">${by[s.key]}</span></li>`).join("")}
    </ul></div>`;

  // ---- AI review panel: finding-disposition pie + legend -------------------
  // Every finding falls in exactly one disposition, so the segments sum to fTotal. "Active" = still awaiting triage (split High/Low); the rest have been acted on.
  if (!fTotal) {
    $("#dashFindings").innerHTML = `<div class="dash-empty">${lastRun.none
      ? "No AI review run yet.<br>Run one from the <b>AI Review</b> tab."
      : "AI review found no issues. 🎉"}</div>`;
  } else {
    const aiSegs = [
      { key: "Active · High", color: "var(--crit)",
        val: findings.filter(f => f.state === "pending" && tier(f) === "high").length },
      { key: "Active · Low", color: "var(--minor)",
        val: findings.filter(f => f.state === "pending" && tier(f) === "low").length },
      { key: "Sent to human review", color: "var(--accent)",
        val: findings.filter(f => f.state === "posted").length },
      { key: "Rejected", color: "var(--muted)",
        val: findings.filter(f => f.state === "rejected").length },
      { key: "Resolved", color: "var(--major)",
        val: findings.filter(f => f.state === "resolved").length },
    ].filter(s => s.val > 0);
    $("#dashFindings").innerHTML = `<div class="dash-donut-wrap">
      <div class="dash-donut" style="${donutStyle(aiSegs)}">
        <div class="dash-donut-center">
          <div class="dash-donut-pct">${fTotal}</div>
          <div class="dash-donut-cap">findings</div>
        </div>
      </div>
      <ul class="dash-legend">
        ${aiSegs.map(s => `<li class="dash-legend-item">
          <span class="dash-dot" style="background:${s.color}"></span>
          <span class="dash-legend-lbl">${esc(s.key)}</span>
          <span class="dash-legend-ct">${s.val}</span></li>`).join("")}
      </ul></div>`;
  }

  // ---- Comments panel: open / resolved + resolved meter --------------------
  if (!cTotal) {
    $("#dashComments").innerHTML = `<div class="dash-empty">No comments yet.</div>`;
  } else {
    $("#dashComments").innerHTML = `
      <div class="dash-donut-wrap" style="justify-content:space-around;text-align:center">
        <div><div class="dash-kpi-num">${cOpen}</div><div class="dash-kpi-sub">open</div></div>
        <div><div class="dash-kpi-num">${cResolved}</div><div class="dash-kpi-sub">resolved</div></div>
      </div>
      <div class="dash-meter-row" style="margin-top:.9rem"><span>Resolved</span><b>${cResolved} / ${cTotal}</b></div>
      <div class="progress dash-meter-green"><div style="width:${pct(cResolved, cTotal)}%"></div></div>`;
  }
}

// ---------------------------------------------------------------- TOC tab
const TOC_COLS = [
  { key: "status", label: "Status", val: o => o.page_start ? o.status : "No TLF output",
    cell: o => o.page_start
      ? `<span class="badge ${stClass(o.status)}">${esc(o.status)}</span>`
      : `<span class="badge st-noout">No TLF output</span>` },
  { key: "label", label: "Output", val: o => o.label, cell: o => `<a class="link" data-open>${esc(o.label)}</a>` },
  { key: "n_comments", label: "Cmts", val: o => String(safeInt(o.n_comments) || ""),
    cell: o => safeInt(o.n_comments) ? `💬 ${safeInt(o.n_comments)}` : "" },
  { key: "file", label: "File", val: o => o.doc_filename || "", cell: o => esc(o.doc_filename || "") },
  { key: "output_type", label: "Output Type", val: o => o.output_type, cell: o => esc(o.output_type) },
  { key: "number", label: "Number", val: o => o.number, cell: o => esc(o.number) },
  { key: "title", label: "Title", val: o => o.title, cell: o => esc(o.title) },
  { key: "pages", label: "Pages", val: o => safeInt(o.page_start) ? `${safeInt(o.page_start)}-${safeInt(o.page_end)}` : "",
    cell: o => safeInt(o.page_start) ? `${safeInt(o.page_start)}–${safeInt(o.page_end)}` : "" },
  // How much of this output the AI actually read. A long table can be only partly extracted, and then "no findings" is not evidence that it is clean — so the TOC, where a reviewer decides whether to trust a row, has to show it.
  { key: "coverage", label: "AI read", val: o => tocCoverage(o).text,
    cell: o => { const c = tocCoverage(o);
      return c.text ? `<span class="badge ${c.cls}" title="${esc(c.title)}">${esc(c.text)}</span>` : ""; } },
];

// "AI read" cell content for one output: full / partial / not yet analysed.
function tocCoverage(o) {
  if (o.cov_total == null || o.cov_read == null) return { text: "", cls: "", title: "" };
  const total = safeInt(o.cov_total), read = safeInt(o.cov_read);
  if (read >= total) return { text: "full", cls: "cov-full",
                              title: `All ${total} page(s) were read by the AI` };
  const pct = total ? Math.round(100 * read / total) : 0;
  return { text: `${read}/${total} (${pct}%)`, cls: "cov-part",
           title: `Only ${read} of ${total} pages reached the AI — rows on the other `
                + `pages were never analysed, so "no findings" here does not mean clean` };
}

function tocHidden() { try { return JSON.parse(localStorage.getItem("tlf_toc_hidden") || "[]"); } catch { return []; } }
function tocSetHidden(arr) { localStorage.setItem("tlf_toc_hidden", JSON.stringify(arr)); }

function renderTOC() {
  const outs = reviewableOutputs(state.project);
  const filters = {};        // per-column contains filters
  let hidden = new Set(tocHidden());

  app().innerHTML = `
    <div id="tocStructural"></div>
    <div class="toolbar">
      <input type="text" id="tocSearch" placeholder="Search title, number, status…" value="${esc(state.filter)}">
      <div class="dropdown"><button class="btn ghost" id="colBtn">Show/Hide Columns ▾</button>
        <div class="dd-menu hidden" id="colMenu"></div></div>
      <span class="muted" id="tocCount" style="margin-left:auto"></span>
    </div>
    <div class="table-wrap"><table class="grid">
      <thead><tr id="tocHead"></tr><tr id="tocFilters" class="filter-row"></tr></thead>
      <tbody id="tocBody"></tbody></table></div>`;

  const visibleCols = () => TOC_COLS.filter(c => !hidden.has(c.key));

  const drawHead = () => {
    $("#tocHead").innerHTML = visibleCols().map(c => `<th>${esc(c.label)}</th>`).join("");
    $("#tocFilters").innerHTML = visibleCols().map(c =>
      `<th><input class="col-filter" data-k="${c.key}" placeholder="contains…" value="${esc(filters[c.key] || "")}"></th>`).join("");
    $("#tocFilters").querySelectorAll("input").forEach(inp =>
      inp.addEventListener("input", e => { filters[e.target.dataset.k] = e.target.value; drawBody(); }));
  };

  const drawBody = () => {
    const g = state.filter.toLowerCase();
    const cols = visibleCols();
    const rows = outs.filter(o => {
      if (g && !cols.some(c => (c.val(o) || "").toLowerCase().includes(g))) return false;
      return cols.every(c => {
        const f = (filters[c.key] || "").toLowerCase();
        return !f || (c.val(o) || "").toLowerCase().includes(f);
      });
    });
    const body = $("#tocBody"); body.innerHTML = "";
    rows.forEach(o => {
      const tr = el(`<tr>${cols.map(c => `<td>${c.cell(o)}</td>`).join("")}</tr>`);
      const open = tr.querySelector("[data-open]");
      if (open) open.onclick = () => { state.output = o; setTab("tlf"); };
      body.appendChild(tr);
    });
    $("#tocCount").textContent = `${rows.length} of ${outs.length} rows`;
  };

  // Show/Hide columns menu
  const menu = $("#colMenu");
  menu.innerHTML = TOC_COLS.map(c =>
    `<label><input type="checkbox" data-k="${c.key}" ${hidden.has(c.key) ? "" : "checked"}> ${esc(c.label)}</label>`).join("");
  $("#colBtn").onclick = () => menu.classList.toggle("hidden");
  menu.querySelectorAll("input").forEach(inp => inp.addEventListener("change", e => {
    const k = e.target.dataset.k;
    if (e.target.checked) hidden.delete(k); else hidden.add(k);
    tocSetHidden([...hidden]); drawHead(); drawBody();
  }));
  document.addEventListener("click", e => {
    if (!e.target.closest(".dropdown")) menu.classList.add("hidden");
  });

  $("#tocSearch").addEventListener("input", e => { state.filter = e.target.value; drawBody(); });
  drawHead(); drawBody();
  loadStructuralBanner();
}

// Deterministic (rule-based, non-AI) findings surfaced on the TOC page. These are written at project creation, so they are visible BEFORE any AI review is triggered. Renders a foldable summary card; a finding tied to an output jumps to that table in the TLF viewer.
async function loadStructuralBanner() {
  const box = $("#tocStructural"); if (!box) return;
  const rows = (await getJSON(`/api/projects/${state.project.id}/findings`, { quiet: true })
    .catch(() => [])).filter(f => f.phase === "structural");
  if (!rows.length) { box.innerHTML = ""; return; }
  const fam = { blank: 0, gap: 0, missing: 0, other: 0 };
  rows.forEach(f => {
    const id = f.check_id || "";
    if (id.startsWith("FMT-010")) fam.blank++;
    else if (id.startsWith("XOUT-001")) fam.gap++;
    else if (id.startsWith("XOUT-020")) fam.missing++;
    else fam.other++;
  });
  const parts = [];
  if (fam.blank) parts.push(`${fam.blank} blank page${fam.blank === 1 ? "" : "s"}`);
  if (fam.gap) parts.push(`${fam.gap} numbering gap${fam.gap === 1 ? "" : "s"}`);
  if (fam.missing) parts.push(`${fam.missing} missing vs prior`);
  if (fam.other) parts.push(`${fam.other} other`);
  const items = rows.map(f => {
    const loc = f.output_label
      ? `<a class="link" data-open="${esc(f.output_label)}">${esc(f.output_label)}</a> · ` : "";
    return `<li><span class="chk">${esc(f.check_id)}</span> ${loc}${esc(f.message)}</li>`;
  }).join("");
  box.innerHTML = `<details class="struct-banner" open>
    <summary>⚠ Deterministic checks: ${esc(parts.join(" · "))}
      <span class="muted">— rule-based, no AI; run at project creation</span></summary>
    <ul class="struct-list">${items}</ul></details>`;
  box.querySelectorAll("[data-open]").forEach(a => a.addEventListener("click", () => {
    const o = state.project.outputs.find(x => x.label === a.dataset.open);
    if (o) { state.output = o; setTab("tlf"); }
    else { state.filter = a.dataset.open; setTab("toc"); }
  }));
}

// ---------------------------------------------------------------- TLF tab
function renderTLF() {
  const p = state.project;
  app().innerHTML = `<div class="tlf" id="tlfGrid">
    <div class="col">
      <div class="bm-toolbar" id="bmToolbar">
        <span class="bm-tb-lbl">Group by</span>
        <div class="bm-seg">
          <button class="bm-seg-btn" data-group="file" title="File → Table → subtable">File</button>
          <button class="bm-seg-btn" data-group="table" title="Table → File → subtable">Table</button>
        </div>
      </div>
      <ul class="bm-list" id="bmList"></ul>
    </div>
    <div class="gutter" data-side="left"></div>
    <div class="col">
      <div class="viewer-head">
        <div class="vh-row vh-row1">
          <button class="btn ghost sm" id="prevOut">‹ Prev</button>
          <button class="btn ghost sm" id="nextOut">Next ›</button>
          <span class="title" id="vTitle"></span>
        </div>
        <div class="vh-row vh-row2">
          <span class="pg-ctl">
            <button class="btn ghost sm" id="pgPrev" title="Previous page">◀</button>
            <span id="pgInfo" class="muted"></span>
            <button class="btn ghost sm" id="pgNext" title="Next page">▶</button>
          </span>
          <span class="zoom-ctl">
            <button class="btn ghost sm" id="zOut" title="Zoom out">−</button>
            <button class="btn ghost sm" id="zIn" title="Zoom in">+</button>
            <button class="btn ghost sm" id="zReset" title="Fit to width">Fit</button>
          </span>
          <span class="anno-tools">
            <button class="tool-btn" data-tool="pan" title="Move / pan the page (drag to scroll)">
              <svg viewBox="0 0 20 20"><path d="M10 3v14M10 3L8 5m2-2l2 2M10 17l-2-2m2 2l2-2M3 10h14M3 10l2-2m-2 2l2 2M17 10l-2-2m2 2l-2 2" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
            <button class="tool-btn" data-tool="highlight" title="Highlighter">
              <svg viewBox="0 0 20 20"><path d="M4 16l1.3-3.7L13 5l3 3-7.3 7.3L4 16z"/><rect x="3.4" y="16.4" width="6" height="1.6" rx=".6"/></svg></button>
            <button class="tool-btn" data-tool="rect" title="Rectangle">
              <svg viewBox="0 0 20 20"><rect x="3.5" y="4.5" width="13" height="11" rx="1" fill="none" stroke-width="2"/></svg></button>
            <button class="tool-btn" data-tool="freehand" title="Freehand pen">
              <svg viewBox="0 0 20 20"><path d="M3 17l1-3.5L14 4l2.5 2.5L6.5 16 3 17z"/><path d="M12.5 5.5l2 2" stroke-width="1.4"/></svg></button>
            <button class="tool-btn" data-tool="eraser" title="Eraser — drag over a mark to remove it">
              <svg viewBox="0 0 20 20"><path d="M3 13.4l6.4-6.4 5 5-4 4H6l-3-2.6z" fill="none" stroke-width="1.6" stroke-linejoin="round"/><path d="M8 16h9" stroke-width="1.6" stroke-linecap="round"/></svg></button>
            <input type="color" id="annoColor" class="color-swatch" title="Annotation color">
          </span>
          <button class="btn ghost sm" id="checkToc" title="Show in TOC">Check TOC</button>
          <span class="vfile" id="vFile"></span>
        </div>
      </div>
      <div class="pagewrap"><div class="canvas-stack" id="stack">
        <canvas id="pdfCanvas"></canvas>
        <canvas id="annoCanvas"></canvas>
      </div></div>
    </div>
    <div class="gutter" data-side="right"></div>
    <div class="col sidebar" id="sidebar"></div>
  </div>`;

  setupPanelResize();

  // Collapsible bookmark tree (File → Table → subtable, or Table → File → subtable).
  const multiFile = new Set(reviewableOutputs(p).map(o => o.document_id)).size > 1;
  const tb = $("#bmToolbar");
  if (!multiFile) tb.classList.add("hidden");   // grouping mode only matters across files
  tb.querySelectorAll(".bm-seg-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.group === state.bmGroup);
    b.addEventListener("click", () => {
      if (state.bmGroup === b.dataset.group) return;
      state.bmGroup = b.dataset.group;
      localStorage.setItem("tlf_bm_group", state.bmGroup);
      tb.querySelectorAll(".bm-seg-btn").forEach(x => x.classList.toggle("active", x.dataset.group === state.bmGroup));
      state.bmOpen = new Set();                 // reset expansion for the new layout
      if (state.output) expandToLeaf(state.output);
      renderBmTree();
    });
  });
  renderBmTree();
  // Non-blocking: once findings + last-run land, the panel switches from table counts to remaining-finding counts (if an AI review has completed).
  loadBookmarkMeta().then(renderBmTree);

  $("#prevOut").addEventListener("click", () => moveOutput(-1));
  $("#nextOut").addEventListener("click", () => moveOutput(1));
  $("#pgPrev").addEventListener("click", () => gotoPage(state.pageNum - 1));
  $("#pgNext").addEventListener("click", () => gotoPage(state.pageNum + 1));
  $("#zIn").addEventListener("click", () => setZoom(state.zoom + 0.25));
  $("#zOut").addEventListener("click", () => setZoom(state.zoom - 0.25));
  $("#zReset").addEventListener("click", () => setZoom(1));
  $("#checkToc").addEventListener("click", () => { state.filter = state.output.label; setTab("toc"); });

  document.querySelectorAll(".tool-btn").forEach(b =>
    b.addEventListener("click", () => setTool(b.dataset.tool)));
  const cp = $("#annoColor");
  cp.value = state.annoColor;
  cp.addEventListener("input", e => {
    state.annoColor = e.target.value;
    localStorage.setItem("tlf_anno_color", state.annoColor);
  });
  setupAnnoCanvas();
  setTool(state.tool);   // sync toolbar + cursor to the actual tool (survives tab switches)

  if (state.output) { selectOutput(state.output, state.jumpPage || 1); state.jumpPage = null; }
}

// --- resizable TLF panels ------------------------------------------------- #
function panelWidths() {
  const d = { left: 260, right: 340 };
  try { return { ...d, ...JSON.parse(localStorage.getItem("tlf_panels") || "{}") }; } catch { return d; }
}
function applyPanelWidths(w) {
  const grid = $("#tlfGrid"); if (!grid) return;
  grid.style.gridTemplateColumns = `${w.left}px 6px 1fr 6px ${w.right}px`;
}
function setupPanelResize() {
  const w = panelWidths();
  applyPanelWidths(w);
  document.querySelectorAll("#tlfGrid .gutter").forEach(g => {
    g.addEventListener("mousedown", e => {
      e.preventDefault();
      const side = g.dataset.side;
      const startX = e.clientX, startW = w[side];
      g.classList.add("dragging");
      document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
      const move = ev => {
        const dx = ev.clientX - startX;
        // left gutter grows the left panel with the cursor; right gutter grows the right panel as the cursor moves left.
        const raw = side === "left" ? startW + dx : startW - dx;
        w[side] = Math.max(140, Math.min(680, raw));
        applyPanelWidths(w);
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        g.classList.remove("dragging");
        document.body.style.cursor = ""; document.body.style.userSelect = "";
        localStorage.setItem("tlf_panels", JSON.stringify(w));
        if (state.pdf) gotoPage(state.pageNum);   // refit the PDF to the new width
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  });
}

function moveOutput(d) {
  const outs = reviewableOutputs(state.project);
  const i = outs.findIndex(o => o.id === state.output.id);
  const n = outs[i + d]; if (n) selectOutput(n);
}

// --- collapsible bookmark tree ------------------------------------------- #
// Two layouts are available through the "Group by" control: file (File → Table N → subtable leaves, the default) and table (Table N → File → subtable leaves). "Table N" is the leading integer of the dotted output number (2.2.1 → "Table 2"). A standalone output whose number has no dotted part is shown as a plain leaf instead of a redundant one-item group.
function groupBy(items, keyFn) {
  const m = new Map();
  items.forEach(it => { const k = keyFn(it); (m.get(k) || m.set(k, []).get(k)).push(it); });
  return m;   // insertion-ordered
}
function leadingNum(o) { const n = (o.number || "").split(".")[0]; return /^\d+$/.test(n) ? n : ""; }
function bigtableKey(o) {
  const t = o.output_type || "Output", n = leadingNum(o);
  return n ? `${t} ${n}` : (o.label || t);
}
function hasSubtable(o) { return (o.number || "").includes("."); }
function fileHeaderHtml(o) {
  return `<span class="bm-file-ic">📄</span> ${esc(o.doc_filename || "file")}`
    + (o.edition ? ` <span class="ed">(${esc(o.edition)})</span>` : "");
}

// Bookmark badge count: always the number of OPEN (pending, unactioned) AI findings for these outputs — never a table count. Deterministic structural findings written at project creation count too, so before any AI review is run/imported the badges are all zero except on tables the deterministic checks flagged; a completed/imported review fills in the rest.
function bmCount(outs) {
  return outs.reduce((s, o) => s + (state.aiFindingCounts[o.id] || 0), 0);
}
// Refresh the finding-count metadata that drives the bookmark panel, then rebuild the tree.
async function loadBookmarkMeta() {
  const pid = state.project && state.project.id; if (!pid) return;
  const findings = await getJSON(`/api/projects/${pid}/findings`, { quiet: true }).catch(() => []);
  const m = {};
  findings.forEach(f => {
    if (f.state === "pending" && f.output_id) m[f.output_id] = (m[f.output_id] || 0) + 1;
  });
  state.aiFindingCounts = m;
}
function refreshBookmarks() {
  loadBookmarkMeta().then(() => { if (state.tab === "tlf") renderBmTree(); });
}

function leafNode(o) {
  const s = splitTitle(o.title);
  const n = state.aiFindingCounts[o.id] || 0;
  const badge = `<span class="bm-leaf-ct ${n ? "on" : ""}">${n}</span>`;
  const li = el(`<li class="bm-leaf" data-id="${safeInt(o.id)}">
    <span class="bm-num">${esc(o.label)}</span>
    <span class="bm-name">${esc(s.name)}</span>
    ${s.population ? `<span class="bm-pop">${esc(s.population)}</span>` : ""}
    ${badge}</li>`);
  li.addEventListener("click", () => selectOutput(o));
  return li;
}
function groupNode(key, level, headerHtml, count) {
  const open = state.bmOpen.has(key);
  const ctCls = "bm-grp-ct find" + (count ? " on" : "");
  const li = el(`<li class="bm-grp bm-lvl-${level} ${open ? "open" : ""}">
    <div class="bm-grp-hd">
      <span class="tw">${open ? "▾" : "▸"}</span>
      <span class="bm-grp-lbl">${headerHtml}</span>
      <span class="${ctCls}">${count}</span>
    </div>
    <ul class="bm-children"${open ? "" : ' style="display:none"'}></ul></li>`);
  li.querySelector(".bm-grp-hd").addEventListener("click", () => {
    if (state.bmOpen.has(key)) state.bmOpen.delete(key); else state.bmOpen.add(key);
    renderBmTree();
  });
  return li;
}

// Append the Table-N level (and its leaves) under a container. `parentKey` scopes the node keys so the same "Table 2" under two files stays independently openable.
function appendBigtables(container, outs, parentKey) {
  groupBy(outs, bigtableKey).forEach((bigOuts, blabel) => {
    if (bigOuts.length === 1 && !hasSubtable(bigOuts[0])) { container.appendChild(leafNode(bigOuts[0])); return; }
    const key = (parentKey || "T1") + "|T:" + blabel;
    const node = groupNode(key, "table", esc(blabel), bmCount(bigOuts));
    const kids = node.querySelector(".bm-children");
    bigOuts.forEach(o => kids.appendChild(leafNode(o)));
    container.appendChild(node);
  });
}

function renderBmTree() {
  const list = $("#bmList"); if (!list) return;
  const col = list.parentElement, scroll = col ? col.scrollTop : 0;
  list.innerHTML = "";
  const outs = reviewableOutputs(state.project);
  const multiFile = new Set(outs.map(o => o.document_id)).size > 1;

  if (multiFile && state.bmGroup === "table") {
    // Table N → File → leaves
    groupBy(outs, bigtableKey).forEach((bigOuts, blabel) => {
      if (bigOuts.length === 1 && !hasSubtable(bigOuts[0])) { list.appendChild(leafNode(bigOuts[0])); return; }
      const tkey = "T:" + blabel;
      const tnode = groupNode(tkey, "table", esc(blabel), bmCount(bigOuts));
      const tkids = tnode.querySelector(".bm-children");
      groupBy(bigOuts, o => o.document_id).forEach(fileOuts => {
        const f0 = fileOuts[0], fkey = tkey + "|F:" + f0.document_id;
        const fnode = groupNode(fkey, "file", fileHeaderHtml(f0), bmCount(fileOuts));
        const fkids = fnode.querySelector(".bm-children");
        fileOuts.forEach(o => fkids.appendChild(leafNode(o)));
        tkids.appendChild(fnode);
      });
      list.appendChild(tnode);
    });
  } else if (multiFile) {
    // File → Table N → leaves
    groupBy(outs, o => o.document_id).forEach(fileOuts => {
      const f0 = fileOuts[0], fkey = "F:" + f0.document_id;
      const fnode = groupNode(fkey, "file", fileHeaderHtml(f0), bmCount(fileOuts));
      appendBigtables(fnode.querySelector(".bm-children"), fileOuts, fkey);
      list.appendChild(fnode);
    });
  } else {
    // Single file: Table N → leaves
    appendBigtables(list, outs, "T1");
  }
  highlightActiveLeaf();
  if (col) col.scrollTop = scroll;
}

// Open every group that could contain this output (extra keys are harmless), so a leaf reached from a finding / Prev-Next is revealed even if its branch was closed.
function expandToLeaf(o) {
  if (!o) return;
  const doc = o.document_id, b = bigtableKey(o);
  ["F:" + doc, "F:" + doc + "|T:" + b, "T:" + b, "T:" + b + "|F:" + doc, "T1|T:" + b]
    .forEach(k => state.bmOpen.add(k));
}
function highlightActiveLeaf() {
  const id = state.output && state.output.id;
  document.querySelectorAll("#bmList li.bm-leaf").forEach(li => {
    const on = +li.dataset.id === id;
    li.classList.toggle("active", on);
    if (on) li.scrollIntoView({ block: "nearest" });
  });
}

async function selectOutput(o, page = 1) {
  state.output = o; state.pageNum = page;
  expandToLeaf(o); renderBmTree();   // reveal + highlight this leaf in the tree
  $("#vTitle").textContent = `${o.label} — ${o.title}`;
  const vf = $("#vFile");
  if (vf) vf.innerHTML = o.doc_filename ? `📄 ${esc(o.doc_filename)}${o.edition ? ` <span class="ed">(${esc(o.edition)})</span>` : ""}` : "";
  renderSidebar();
  try {
    state.annos = await getJSON(`/api/outputs/${o.id}/annotations`);
  } catch { state.annos = []; }
  try {
    const buf = await (await fetch("/api/tlf-clip?output_id=" + o.id)).arrayBuffer();
    // Defense in depth for untrusted uploaded PDFs. Current PDF.js is patched for CVE-2024-4367; disabling dynamic evaluation also protects this viewer if a future dependency downgrade is introduced accidentally.
    state.pdf = await pdfjsLib.getDocument({ data: buf, isEvalSupported: false }).promise;
    gotoPage(page);
  } catch (err) { toast("PDF load failed: " + err.message, 4000); }
}

function setZoom(z) {
  state.zoom = Math.max(0.5, Math.min(4, Math.round(z * 100) / 100));
  // The "Fit" button doubles as the zoom indicator: shows % when zoomed, "Fit" at 1×.
  const rb = $("#zReset"); if (rb) rb.textContent = state.zoom === 1 ? "Fit" : Math.round(state.zoom * 100) + "%";
  gotoPage(state.pageNum);
}

async function gotoPage(n) {
  if (!state.pdf) return;
  n = Math.max(1, Math.min(state.pdf.numPages, n));
  state.pageNum = n;
  const page = await state.pdf.getPage(n);
  const canvas = $("#pdfCanvas"); if (!canvas) return;
  // Fit to the available width, then apply the user's zoom. Render at the device pixel ratio so text stays crisp on HiDPI screens. Measure the scroll container (.pagewrap), NOT the canvas' immediate parent — the .canvas-stack wrapper sizes to the canvas itself, which would feed back and make "Fit" unstable.
  const wrap = canvas.closest(".pagewrap");
  let wrapW = 800;
  if (wrap) {
    const cs = getComputedStyle(wrap);
    const padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    // clientWidth excludes the scrollbar; with scrollbar-gutter:stable it's constant whether or not a vertical scrollbar is showing, so Fit never overflows.
    wrapW = wrap.clientWidth - padX - 2;
  }
  wrapW = Math.max(320, wrapW);
  const base = page.getViewport({ scale: 1 });
  const fit = wrapW / base.width;
  const cssScale = fit * state.zoom;
  const dpr = window.devicePixelRatio || 1;
  const vp = page.getViewport({ scale: cssScale });
  const ctx = canvas.getContext("2d");
  canvas.width = Math.floor(vp.width * dpr);
  canvas.height = Math.floor(vp.height * dpr);
  canvas.style.width = Math.floor(vp.width) + "px";
  canvas.style.height = Math.floor(vp.height) + "px";
  await page.render({
    canvasContext: ctx, viewport: vp,
    transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
  }).promise;
  // Match the annotation overlay to the PDF canvas' CSS size and redraw marks.
  const anno = $("#annoCanvas");
  if (anno) {
    anno.width = canvas.width; anno.height = canvas.height;
    anno.style.width = canvas.style.width; anno.style.height = canvas.style.height;
    drawAnnotations();
  }
  let info = `${state.pageNum}/${state.pdf.numPages} · p.${state.output.page_start + state.pageNum - 1}`;
  // After jumping to a finding, confirm its subtable/section on the matching page.
  const jl = state.jumpLoc;
  if (jl && jl.page === state.pageNum && (jl.printed_page || jl.section)) {
    const bits = [];
    if (jl.printed_page && jl.pages_total) bits.push(`subtable ${jl.printed_page}/${jl.pages_total}`);
    if (jl.section) bits.push(jl.section);
    info += " · " + bits.join(" · ");
  }
  $("#pgInfo").textContent = info;
}

// ---------------------------------------------------------------- annotations
function setTool(tool) {
  state.tool = tool;
  document.querySelectorAll(".tool-btn").forEach(b => b.classList.toggle("sel", b.dataset.tool === tool));
  const anno = $("#annoCanvas");
  if (anno) {
    anno.classList.remove("cur-pan", "cur-highlight", "cur-rect", "cur-freehand", "cur-eraser", "panning");
    anno.classList.add("cur-" + tool);
  }
}

function hexToRgba(hex, alpha) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return `rgba(255,213,74,${alpha})`;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

function drawAnnotations() {
  const anno = $("#annoCanvas"); if (!anno) return;
  const ctx = anno.getContext("2d");
  ctx.clearRect(0, 0, anno.width, anno.height);
  const W = anno.width, H = anno.height;
  state.annos.filter(a => a.page === state.pageNum).forEach(a => {
    const g = JSON.parse(a.geom_json || "{}");
    const color = g.color || "#ffd54a";
    ctx.lineWidth = a.kind === "freehand" ? 2.5 : 2;
    if (a.kind === "highlight") {
      ctx.fillStyle = hexToRgba(color, 0.35);
      ctx.fillRect(g.x * W, g.y * H, g.w * W, g.h * H);
    } else if (a.kind === "rect") {
      ctx.strokeStyle = color;
      ctx.strokeRect(g.x * W, g.y * H, g.w * W, g.h * H);
    } else if (a.kind === "freehand" && g.pts) {
      ctx.strokeStyle = color; ctx.lineJoin = "round"; ctx.beginPath();
      g.pts.forEach((p, i) => { const x = p[0] * W, y = p[1] * H; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    }
  });
}

function setupAnnoCanvas() {
  const anno = $("#annoCanvas"); if (!anno) return;
  const wrap = anno.closest(".pagewrap");
  let drawing = false, start = null, pts = [];
  let erasing = false;
  const pos = e => {
    const r = anno.getBoundingClientRect();
    return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height];
  };
  anno.addEventListener("mousedown", e => {
    if (state.tool === "pan") {                 // Task 6: drag the page to scroll it
      if (!wrap) return;
      e.preventDefault();
      anno.classList.add("panning");
      const sx = e.clientX, sy = e.clientY, sl = wrap.scrollLeft, st = wrap.scrollTop;
      const move = ev => { wrap.scrollLeft = sl - (ev.clientX - sx); wrap.scrollTop = st - (ev.clientY - sy); };
      const up = () => {
        anno.classList.remove("panning");
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);   // window-bound so a fast drag past the edge keeps panning
      window.addEventListener("mouseup", up);
      return;
    }
    if (state.tool === "eraser") { erasing = true; eraseAt(pos(e)); return; }   // drag over marks to remove them
    drawing = true; start = pos(e); pts = [start];
  });
  anno.addEventListener("mousemove", e => {
    if (erasing) { eraseAt(pos(e)); return; }
    if (!drawing) return;
    const p = pos(e); const W = anno.width, H = anno.height;
    drawAnnotations();
    const ctx = anno.getContext("2d");
    const color = state.annoColor;
    if (state.tool === "freehand") {
      pts.push(p); ctx.lineWidth = 2.5; ctx.lineJoin = "round"; ctx.strokeStyle = color; ctx.beginPath();
      pts.forEach((q, i) => { const x = q[0] * W, y = q[1] * H; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.stroke();
    } else {
      ctx.lineWidth = 2;
      const x = Math.min(start[0], p[0]) * W, y = Math.min(start[1], p[1]) * H;
      const w = Math.abs(p[0] - start[0]) * W, h = Math.abs(p[1] - start[1]) * H;
      if (state.tool === "highlight") { ctx.fillStyle = hexToRgba(color, 0.35); ctx.fillRect(x, y, w, h); }
      else { ctx.strokeStyle = color; ctx.strokeRect(x, y, w, h); }
    }
  });
  const finish = async e => {
    if (erasing) { erasing = false; return; }
    if (!drawing) return; drawing = false;
    const p = pos(e); let geom;
    if (state.tool === "freehand") {
      if (pts.length < 2) return drawAnnotations();
      geom = { pts, color: state.annoColor };
    } else {
      const g = { x: Math.min(start[0], p[0]), y: Math.min(start[1], p[1]),
                  w: Math.abs(p[0] - start[0]), h: Math.abs(p[1] - start[1]), color: state.annoColor };
      if (g.w < 0.005 || g.h < 0.005) return drawAnnotations();
      geom = g;
    }
    await saveAnnotation(state.tool, geom);
  };
  anno.addEventListener("mouseup", finish);
  anno.addEventListener("mouseleave", e => { if (drawing || erasing) finish(e); });
}

async function saveAnnotation(kind, geom) {
  const res = await postForm(`/api/outputs/${state.output.id}/annotations`,
    { kind, page: state.pageNum, geom_json: JSON.stringify(geom) });
  state.annos.push({ id: res.id, output_id: state.output.id, kind, page: state.pageNum, geom_json: JSON.stringify(geom) });
  drawAnnotations();
  toast("Annotation saved");
}

// Hit-test: does point (nx,ny) fall on annotation `a` (on the current page)?
function annoHit(a, nx, ny) {
  if (a.page !== state.pageNum) return false;
  const g = JSON.parse(a.geom_json || "{}");
  if (a.kind === "freehand") return (g.pts || []).some(p => Math.abs(p[0] - nx) < 0.02 && Math.abs(p[1] - ny) < 0.02);
  return nx >= g.x && nx <= g.x + g.w && ny >= g.y && ny <= g.y + g.h;
}

// Eraser: remove the topmost mark under the cursor. Optimistic — drop it from state.annos and redraw right away (so a continued drag won't re-hit it), and fire the delete at the server without a confirm dialog (it's an eraser).
function eraseAt([nx, ny]) {
  const hit = [...state.annos].reverse().find(a => annoHit(a, nx, ny));
  if (!hit) return;
  state.annos = state.annos.filter(a => a.id !== hit.id);
  drawAnnotations();
  del("/api/annotations/" + hit.id).catch(() => {});
}

// ---------------------------------------------------------------- sidebar (status + comments)
async function renderSidebar() {
  const o = state.output;
  const sb = $("#sidebar");
  const storedStatus = o.status === "Approved" ? "Manually approved" : o.status;
  const selectedStatus = [...STATUSES, "Auto-approved"].includes(storedStatus)
    ? storedStatus : "Not Reviewed";
  const statusOptions = selectedStatus === "Auto-approved"
    ? ["Auto-approved", ...STATUSES] : STATUSES;
  sb.innerHTML = `
    <h3>REVIEW STATUS</h3>
    <select id="statusSel">${statusOptions.map(s =>
      `<option value="${esc(s)}" ${s === selectedStatus ? "selected" : ""} ${s === "Auto-approved" ? "disabled" : ""}>${esc(s)}</option>`).join("")}</select>
    <h3>Comments</h3>
    <div id="cmts" class="muted">Loading…</div>
    <h3>Add comment</h3>
    <textarea id="cBody" rows="3" placeholder="Comment…"></textarea>
    <div style="margin-top:.4rem"><button class="btn sm" id="addCmt">Post comment</button></div>
    <div class="sb-head"><h3>AI findings for this output</h3><button class="btn ghost sm" id="reRun">Re-run</button></div>
    <div id="oFindings" class="muted">—</div>
    <h3>Ask about this output</h3>
    <div class="chat-log" id="ochat" style="height:150px"></div>
    <div class="chat-input">
      <input type="text" id="oq" placeholder="Question…"><button class="btn sm" id="osend">Ask</button>
    </div>`;

  const sel = $("#statusSel");
  sel.addEventListener("change", async () => {
    const prev = STATUSES.includes(o.status) ? o.status : selectedStatus;
    const next = sel.value;
    if (next === prev) return;
    try {
      await postForm(`/api/outputs/${o.id}/status`, { status: next });
      o.status = next; toast("Status: " + o.status);
    } catch (err) {
      sel.value = prev; toast("Could not save status: " + err.message, 4000);
    }
  });
  $("#reRun").addEventListener("click", async () => {
    const btn = $("#reRun"); btn.disabled = true; btn.innerHTML = 'Running… <span class="spin"></span>';
    try {
      const r = await postForm(`/api/outputs/${o.id}/ai-run`, {});
      toast(r.error ? "Re-run: " + r.error : `Re-run done · ${r.findings} findings`);
      loadOutputFindings();
      refreshBookmarks();
    } catch (err) { toast("Re-run failed: " + err.message, 4000); }
    btn.disabled = false; btn.textContent = "Re-run";
  });
  $("#addCmt").addEventListener("click", async () => {
    const body = $("#cBody").value.trim(); if (!body) return;
    await postForm(`/api/outputs/${o.id}/comments`, { body });
    if (o.status === "Not Reviewed") { o.status = "In Progress"; if ($("#statusSel")) $("#statusSel").value = o.status; }
    loadOutputComments();
    $("#cBody").value = ""; toast("Comment posted");
  });
  $("#osend").onclick = sendOutputChat;
  $("#oq").addEventListener("keydown", e => { if (e.key === "Enter") sendOutputChat(); });
  loadOutputComments();
  loadOutputFindings();
}

async function loadOutputFindings() {
  const o = state.output;
  const box = $("#oFindings"); if (!box) return;
  const rows = (await getJSON(`/api/projects/${state.project.id}/findings`)).filter(f => f.output_id === o.id);
  box.classList.remove("muted");
  if (!rows.length) { box.innerHTML = '<span class="muted">No findings for this output.</span>'; return; }
  box.innerHTML = "";
  rows.forEach(r => box.appendChild(findingCard(r, () => { loadOutputFindings(); loadOutputComments(); refreshBookmarks(); })));
}

async function sendOutputChat() {
  const q = $("#oq").value.trim(); if (!q) return;
  const log = $("#ochat");
  log.appendChild(el(`<div class="chat-msg user"><span class="bubble">${esc(q)}</span></div>`));
  $("#oq").value = "";
  const wait = el(`<div class="chat-msg"><span class="bubble"><span class="spin"></span></span></div>`);
  log.appendChild(wait); log.scrollTop = log.scrollHeight;
  try {
    const r = await postForm("/api/chat", { scope: "output", output_id: state.output.id, question: q });
    wait.querySelector(".bubble").textContent = r.answer;
  } catch (err) { wait.querySelector(".bubble").textContent = "Error: " + err.message; }
  log.scrollTop = log.scrollHeight;
}

// Build a parent→replies tree from a flat comment list.
function threadify(comments) {
  const byId = {}; comments.forEach(c => (byId[c.id] = { ...c, replies: [] }));
  const tops = [];
  comments.forEach(c => {
    if (c.parent_id && byId[c.parent_id]) byId[c.parent_id].replies.push(byId[c.id]);
    else tops.push(byId[c.id]);
  });
  return tops;
}

async function loadOutputComments() {
  const o = state.output;
  const all = await getJSON(`/api/projects/${state.project.id}/comments`);
  // Order by the per-Table comment ID (num) so display order matches the export / (ID,Table) key.
  const mine = all.filter(c => c.output_id === o.id).sort((a, b) => (a.num || 0) - (b.num || 0));
  const tops = threadify(mine);
  const box = $("#cmts"); if (!box) return;
  box.classList.remove("muted");
  box.innerHTML = tops.length ? "" : '<span class="muted">No comments yet.</span>';
  tops.forEach(c => box.appendChild(commentCard(c, loadOutputComments)));
}

function commentCard(c, refresh) {
  const resolved = c.resolved ? " resolved" : "";
  const card = el(`<div class="comment${resolved}">
    <div class="c-body"><span class="cnum">#${displayInt(c.num)}</span> ${esc(c.body)}</div>
    <div class="c-meta">
      <span class="tagpill">${esc(c.source)}</span>
      <a class="link" data-reply>reply</a>
      <a class="link" data-res>${c.resolved ? "unresolve" : "resolve"}</a>
      <a class="link" data-del>delete</a>
    </div>
    <div class="replies"></div></div>`);
  const repBox = card.querySelector(".replies");
  (c.replies || []).slice().sort((a, b) => (a.num || 0) - (b.num || 0)).forEach(r => repBox.appendChild(replyCard(r)));
  card.querySelector("[data-del]").onclick = async () => { await del("/api/comments/" + c.id); refresh(); };
  card.querySelector("[data-res]").onclick = async () => {
    await postForm(`/api/comments/${c.id}/resolve`, { resolved: c.resolved ? 0 : 1 }); refresh();
  };
  card.querySelector("[data-reply]").onclick = async () => {
    const body = prompt("Reply:"); if (!body) return;
    await postForm(`/api/comments/${c.id}/reply`, { body }); refresh();
  };
  return card;
}

function replyCard(r) {
  return el(`<div class="reply"><span class="arrow">↳</span>
    <div><div class="c-body"><span class="cnum">#${displayInt(r.num)}</span> ${esc(r.body)}</div></div></div>`);
}

// ---------------------------------------------------------------- AI Review tab
// One two-tier scale, shown as a single High/Low badge. `tier()` collapses a finding to "high"|"low" from its risk, tolerating legacy rows (old severity critical/major/minor, or risk Medium) so a mixed database still renders. TIER_ORDER sorts High before Low.
const TIER_ORDER = { high: 0, low: 1 };
function tier(f) {
  const risk = (f.risk || "").toLowerCase();
  if (risk === "high") return "high";
  if (risk === "low" || risk === "medium") return "low";
  const sev = (f.severity || "").toLowerCase();   // legacy fallback
  return (sev === "high" || sev === "critical" || sev === "major") ? "high" : "low";
}
const tierLabel = f => tier(f) === "high" ? "High" : "Low";

// Subtable / section locator shown on a finding — tells the reviewer exactly which printed subtable page ("TABLE PAGE x of N") and indication section the row is in.
function locChip(f) {
  const bits = [];
  const printedPage = safeInt(f.printed_page);
  const pagesTotal = safeInt(f.pages_total);
  if (printedPage && pagesTotal) bits.push(`p${printedPage}/${pagesTotal}`);
  if (f.section) bits.push(esc(f.section));
  if (!bits.length) return "";
  return `<span class="loc-chip" title="printed subtable page · section">${bits.join(" · ")}</span>`;
}

function findingCard(f, refresh) {
  const scopeMap = {
    "within-file": { cls: "sc-within", txt: "within file" },
    "cross-file": { cls: "sc-cross", txt: "cross-file (vs prior edition)" },
    "cross-output": { cls: "sc-out", txt: "cross-output" },
  };
  const sc = scopeMap[f.scope] || scopeMap["within-file"];
  const fileTag = f.file ? `<span class="f-file" title="source file">📄 ${esc(f.file)}</span>` : "";
  const card = el(`<div class="finding sev-${tier(f)} ${findingStateClass(f.state)}">
    <div class="f-head">
      <span class="chk">${esc(f.check_id)}</span>
      <span class="risk risk-${tier(f)}" title="risk tier">${tierLabel(f)}</span>
      ${f.output_label ? `<a class="link" data-open>${esc(f.output_label)}</a>` : `<span class="muted">${(f.affected || []).map(esc).join(", ") || "cross-output"}</span>`}
      ${locChip(f)}
      ${f.row_kind === "aggregate" ? '<span class="agg-tag" title="subtotal / category row (not a per-subject row)">aggregate</span>' : ""}
      <span class="scope ${sc.cls}">${sc.txt}</span>
      ${fileTag}
      ${f.badge === "new" ? '<span class="flag-new">✦ New</span>' : ""}
      ${f.badge === "potentially_resolved" ? '<span class="flag-pr">🟡 Potentially resolved</span>' : ""}
      <span style="margin-left:auto" class="muted">${esc(f.state)}</span>
    </div>
    <div class="f-msg">${esc(f.message)}</div>
    <div class="f-actions"></div></div>`);
  const acts = card.querySelector(".f-actions");
  const mkBtn = (label, fn) => { const b = el(`<button class="btn ghost sm">${label}</button>`); b.onclick = fn; acts.appendChild(b); };
  if (f.state === "posted") mkBtn("Reopen", () => act(f.id, "reopen", refresh));
  else if (f.state === "rejected") mkBtn("Reopen", () => act(f.id, "reopen", refresh));
  else { mkBtn("Edit & post", () => postFinding(f, refresh)); mkBtn("Reject", () => act(f.id, "reject", refresh)); }
  const open = card.querySelector("[data-open]");
  if (open) open.onclick = () => {
    // Resolve by output_id first: in a multi-edition project two outputs can share the same label ("Table 1" in both the prior and current editions), and the finding's output_id is the authoritative link — matching on label alone lands on the wrong edition and the sidebar then shows "no findings" (findings filter by output_id).
    const o = state.project.outputs.find(x => x.id === f.output_id)
           || state.project.outputs.find(x => x.label === f.output_label);
    if (!o) return;
    // Jump the viewer straight to the finding's page (a table spans many pages); carry the locator so #pgInfo can confirm the subtable/section on arrival.
    state.output = o;
    state.jumpPage = f.page || 1;
    state.jumpLoc = { page: f.page || 1, printed_page: f.printed_page,
                      pages_total: f.pages_total, section: f.section };
    setTab("tlf");
  };
  return card;
}
async function act(fid, action, refresh) { await postForm(`/api/findings/${fid}/${action}`, { author: reviewer() }); refresh(); }
async function postFinding(f, refresh) {
  const text = prompt("Comment text (edit before posting):", f.message);
  if (text === null) return;
  await postForm(`/api/findings/${f.id}/post`, { text, author: reviewer() });
  toast("Posted as comment"); refresh();
}

async function renderAI() {
  app().innerHTML = `<div class="split">
    <div>
      <div class="toolbar">
        <button class="btn" id="runBtn" title="Review tables not yet reviewed, reusing cached extractions where possible">▶ Run AI Review</button>
        <button class="btn ghost" id="freshBtn" title="Re-read this edition from scratch and re-review every table, ignoring cached extractions">Fresh re-run</button>
        <button class="btn ghost" id="expBtn" title="Download all AI findings as an Excel (.xlsx) file">Export</button>
        <button class="btn ghost" id="impBtn" title="Load findings from an Excel (.xlsx) file, added alongside existing findings">Import</button>
        <input type="file" id="impFile" accept=".xlsx" hidden>
        <button class="btn ghost" id="clrBtn" title="Delete all AI findings for this project">Clear</button>
        <span id="findCount" class="muted" style="margin-left:auto"></span>
      </div>
      <div class="toolbar ai-cfg">
        <label class="cfg-lbl">Model <select id="aiModel" class="cfg-sel"></select></label>
        <label class="cfg-lbl">Effort <select id="aiEffort" class="cfg-sel"></select></label>
        <span id="aiEstimate" class="est muted"></span>
      </div>
      <div id="lastRun" class="lastrun muted"></div>
      <div id="runAlert" class="run-alert hidden"></div>
      <div class="progress hidden" id="prog"><div></div></div>
      <div id="findings"><p class="muted">No findings yet — run the AI review.</p></div>
    </div>
    <div>
      <h3 style="margin:.2rem 0">Ask about the delivery</h3>
      <div class="chatbox">
        <div class="chat-log" id="gchat"></div>
        <div class="chat-input">
          <input type="text" id="gq" placeholder="e.g. Which tables have N decreases?">
          <button class="btn" id="gsend">Send</button>
        </div>
      </div>
    </div></div>`;

  const avail = await getJSON("/api/ai/available");
  if (!avail.available) { $("#lastRun").textContent = "AI unavailable — set ANTHROPIC_API_KEY."; $("#runBtn").disabled = true; $("#freshBtn").disabled = true; }

  await setupAIConfig();

  $("#runBtn").onclick = () => startRun("incremental");
  $("#freshBtn").onclick = () => startRun("fresh");
  $("#expBtn").onclick = () => {
    // A download navigation gives no readable progress; flash the busy bar briefly so the click visibly registers while the file is generated server-side.
    setBusy(true); setTimeout(() => setBusy(false), 1500);
    location.href = `/api/projects/${state.project.id}/export/findings.xlsx`;
  };
  $("#impBtn").onclick = () => $("#impFile").click();
  $("#impFile").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    e.target.value = "";   // allow re-selecting the same file later
    // Importing reads the workbook and inserts every finding row, which can take a few seconds. Disable + relabel the button so it's obvious work is underway (the global busy bar also shows, via postForm), and restore it whatever the outcome.
    const btn = $("#impBtn"), label = btn && btn.textContent;
    if (btn) { btn.disabled = true; btn.textContent = "Importing…"; }
    try {
      const fd = new FormData(); fd.append("file", f); fd.append("actor", reviewer());
      const r = await postForm(`/api/projects/${state.project.id}/import-findings`, fd);
      const bits = [];
      if (r.unmatched_output) bits.push(`${r.unmatched_output} with an unknown output label`);
      if (r.auto_approved) bits.push(`${r.auto_approved} clean table${r.auto_approved === 1 ? "" : "s"} auto-approved`);
      const extra = bits.length ? ` (${bits.join(", ")})` : "";
      toast(`Imported ${r.imported} finding${r.imported === 1 ? "" : "s"} from "${f.name}"${extra}`, 3500);
      await Promise.all([loadFindings(), loadLastRun()]);
      // Import counts as a review completion and may have auto-approved clean tables. Re-fetch the project so those statuses show everywhere (TOC / TLF / Dashboard) without a manual reload, mirroring the refresh after an in-app AI run.
      try {
        const fresh = await getJSON(`/api/projects/${state.project.id}`, { quiet: true });
        state.project.outputs = fresh.outputs;
        if (state.output) {
          const u = state.project.outputs.find(o => o.id === state.output.id);
          if (u) { state.output = u; const sel = $("#statusSel"); if (sel) sel.value = u.status; }
        }
      } catch (e) {}
      await loadBookmarkMeta();
      if (state.tab === "tlf") renderBmTree();
    } catch (err) { toast("Import failed: " + err.message, 5000); }
    finally { if (btn) { btn.disabled = false; btn.textContent = label; } }
  };
  $("#clrBtn").onclick = async () => {
    if (!confirm("Clear all AI findings for this project?")) return;
    await del(`/api/projects/${state.project.id}/findings`); loadFindings(); loadLastRun();
  };
  $("#gsend").onclick = sendGlobalChat;
  $("#gq").addEventListener("keydown", e => { if (e.key === "Enter") sendGlobalChat(); });
  loadLastRun();
  loadFindings();
}

// Render a backend timestamp in the viewer's LOCAL time. The server stores UTC with a +00:00 offset (db.now_iso); the old code stripped the offset and printed the raw UTC clock, so at UTC+8 "Last run" read 8 hours behind. Parse WITH the offset (treating a naive legacy value as UTC, like timeAgo) and format local as "YYYY-MM-DD HH:MM".
function fmtLocal(iso) {
  if (!iso) return "";
  const d = new Date(/[Z+]/.test(iso) ? iso : iso + "Z");
  if (isNaN(d.getTime())) return String(iso).replace("T", " ");
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const h = Math.round(mins / 60);
  return h < 24 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

async function loadLastRun() {
  const box = $("#lastRun"); if (!box) return;
  const r = await getJSON(`/api/projects/${state.project.id}/ai-last-run`);
  if (r.none) { box.textContent = "No AI run yet."; renderRunAlert(null); return; }
  const s = r.summary || {};
  const ok = s.status
    ? s.status === "succeeded" && s.review_complete === true
    : !s.error && (!s.errors || s.errors.length === 0); // legacy summaries
  const runLabel = ok ? "OK" : (s.status || "errors");
  const when = fmtLocal(r.started_at);
  const cov = s.coverage || {};
  const skipped = safeInt(s.skipped);
  const pagesTotal = safeInt(cov.pages_total);
  const pagesRead = safeInt(cov.pages_read);
  box.innerHTML = `Last run: <b>${esc(when)}</b> (${esc(timeAgo(r.started_at))}) `
    + `<span class="run-pill ${ok ? "ok" : "err"}">${esc(runLabel)}</span>`
    + (skipped ? ` · <span class="muted">${skipped} non-table outputs skipped</span>` : "")
    + (pagesTotal ? ` · <span class="muted">read ${pagesRead}/${pagesTotal} pages</span>` : "");
  renderRunAlert(s);   // fail loudly if the run couldn't reach the API
}

// Fail loudly: a run whose extractions couldn't reach the API is NOT a clean review even though it "finishes". Show a prominent red banner stating how many outputs failed and the distinct connection error(s) behind it.
function renderRunAlert(s) {
  const box = $("#runAlert"); if (!box) return;
  const errs = Array.isArray(s && s.errors) ? s.errors.map(String) : [];
  const total = safeInt(s && s.targets);
  const isConn = e => /connection error|could ?n.?t reach|timed out|APIConnectionError/i.test(e);
  const conn = errs.filter(isConn);
  // Trip when: backend flags it unreachable (fix 1/2), OR a connection error hit at least half the analyzed outputs, OR the whole run threw a connection error.
  const unreachable = (s && s.ai_unreachable)
    || (conn.length && conn.length >= Math.max(1, Math.ceil(total / 2)))
    || (s && s.error && isConn(s.error));
  box.classList.remove("warn");
  if (!unreachable) {
    const incomplete = s && (s.review_complete === false
      || s.status === "partial" || s.status === "failed" || s.error
      || (s.n_failed || 0) > 0 || (s.n_judge_failed || 0) > 0);
    if (!incomplete) return renderCoverageAlert(box, s);
    const details = [...new Set([
      ...errs,
      ...(s.error ? [s.error] : []),
    ].filter(Boolean))].slice(0, 8);
    box.classList.remove("hidden");
    box.innerHTML =
        `<div class="run-alert-hd">⚠ AI review ${esc(s.status || "failed")} — results are incomplete.</div>`
      + `<div class="run-alert-sub">This run is <b>NOT a clean review</b> and did not auto-approve tables. `
      + `Resolve the failed extraction/judge/configuration step and re-run.</div>`
      + (details.length ? `<ul class="run-alert-errs">${details.map(m => `<li>${esc(m)}</li>`).join("")}</ul>` : "");
    return;
  }
  const failed = safeInt(s && s.n_failed) || conn.length || errs.length || total;
  const denom  = total || failed;
  const detail = conn.length ? conn : (errs.length ? errs : [String((s && s.error) || "")]);
  const distinct = [...new Set(detail.map(e => e.replace(/^.*?:\s*/, "")))].slice(0, 5);
  box.classList.remove("hidden");
  box.innerHTML =
      `<div class="run-alert-hd">⚠ AI couldn't reach the API — ${failed}/${denom} outputs failed (Connection error).</div>`
    + `<div class="run-alert-sub">This is <b>NOT a clean review.</b> Findings below are incomplete — restore connectivity and re-run.</div>`
    + (distinct.length ? `<ul class="run-alert-errs">${distinct.map(m => `<li>${esc(m)}</li>`).join("")}</ul>` : "");
}

// Second fail-loud case: the API WAS reachable, but a table longer than the slice cap was only partially extracted. The pages past the cut never reached the model, so no judge could raise a finding on them — "0 findings" there is not evidence of a clean table. Amber (less severe than unreachable) but still surfaced, because the alternative is silently presenting a partial review as complete.
function renderCoverageAlert(box, s) {
  const cov = (s && s.coverage) || {};
  const n = safeInt(cov.n_truncated);
  if (!n) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const pagesTotal = safeInt(cov.pages_total);
  const pagesRead = safeInt(cov.pages_read);
  const pct = pagesTotal ? Math.round(100 * pagesRead / pagesTotal) : 0;
  const truncated = Array.isArray(cov.truncated) ? cov.truncated : [];
  const items = truncated
    .map(c => `<li>${esc(c.label)} — read ${displayInt(c.pages_read)} / ${displayInt(c.pages_total)} pages`
            + (c.reason ? ` <span class="muted">(${esc(c.reason)})</span>` : "") + `</li>`)
    .join("");
  box.classList.remove("hidden");
  box.classList.add("warn");
  // Only advise raising the cap when the cap is actually why pages are missing; a failed page-read needs a re-run instead, and telling the reviewer to raise a limit that wasn't the cause would send them down the wrong path.
  const anyCapped = truncated.some(c => c.capped);
  const advice = anyCapped
    ? ` Raise <code>TLF_MAX_SLICES</code> (or set it to 0 for no limit) and re-run for full coverage.`
    : ` Those pages failed to read rather than being skipped by a limit — check the errors above and re-run.`;
  box.innerHTML =
      `<div class="run-alert-hd">⚠ ${n} table${n === 1 ? "" : "s"} only PARTIALLY read`
    + ` — ${pct}% of the analysed pages reached the AI (${pagesRead}/${pagesTotal}).</div>`
    + `<div class="run-alert-sub">Rows on the missing pages were <b>never sent to the model</b>, so no`
    + ` finding can exist for them — <b>“0 findings” on these tables does not mean clean.</b>`
    + advice + `</div>`
    + (items ? `<ul class="run-alert-errs">${items}</ul>` : "");
}

// Open (collapsed=false) or close (collapsed=true) every severity + per-output group in the findings tree at once, syncing each header's ▸/▾ twisty.
function setFindingsCollapsed(collapsed) {
  const box = $("#findings"); if (!box) return;
  box.querySelectorAll(".sev-sec > .sev-body, .out-grp > .out-body").forEach(body => {
    body.style.display = collapsed ? "none" : "";
    const hd = body.previousElementSibling;
    const tw = hd && hd.querySelector(".tw");
    if (tw) tw.textContent = collapsed ? "▸" : "▾";
  });
}

async function loadFindings() {
  const rows = await getJSON(`/api/projects/${state.project.id}/findings`);
  const box = $("#findings"); if (!box) return;
  const cnt = $("#findCount");
  if (!rows.length) { box.innerHTML = '<p class="muted">No findings yet — run the AI review.</p>'; if (cnt) cnt.textContent = ""; return; }
  const counts = { high: 0, low: 0 };
  rows.forEach(r => counts[tier(r)]++);
  if (cnt) cnt.innerHTML = `${rows.length} findings &nbsp;`
    + `<span class="chip c-high">${counts.high} high</span> `
    + `<span class="chip c-low">${counts.low} low</span>`;
  box.innerHTML = "";
  // Bulk expand/collapse for the whole findings tree (severity + per-output groups).
  const bulk = el(`<div class="find-bulk">
    <button class="btn ghost sm" id="expandAll" title="Open every severity and output group">Expand all</button>
    <button class="btn ghost sm" id="collapseAll" title="Close every severity and output group">Collapse all</button></div>`);
  bulk.querySelector("#expandAll").onclick = () => setFindingsCollapsed(false);
  bulk.querySelector("#collapseAll").onclick = () => setFindingsCollapsed(true);
  box.appendChild(bulk);
  // Group by tier (High first) → then cross-output group + per-output groups.
  ["high", "low"].forEach(t => {
    const sevRows = rows.filter(r => tier(r) === t);
    if (!sevRows.length) return;
    const unaddressed = sevRows.filter(r => r.state === "pending").length;
    const sec = el(`<div class="sev-sec"><div class="sev-hd" data-open="1">
        <span class="tw">▾</span> <b>${t.toUpperCase()}</b>
        <span class="muted">(${unaddressed}/${sevRows.length} unaddressed)</span></div>
      <div class="sev-body"></div></div>`);
    const body = sec.querySelector(".sev-body");
    sec.querySelector(".sev-hd").onclick = e => {
      const open = body.style.display !== "none";
      body.style.display = open ? "none" : "";
      e.currentTarget.querySelector(".tw").textContent = open ? "▸" : "▾";
    };
    // cross-output first
    const cross = sevRows.filter(r => !r.output_label);
    if (cross.length) body.appendChild(outputGroup("🌐 Global / cross-output", cross));
    // then per output, preserving order of appearance
    const byOut = {};
    sevRows.filter(r => r.output_label).forEach(r => (byOut[r.output_label] ||= []).push(r));
    Object.keys(byOut).forEach(lbl => body.appendChild(outputGroup(lbl, byOut[lbl])));
    box.appendChild(sec);
  });
}

function outputGroup(title, rows) {
  const g = el(`<div class="out-grp"><div class="out-hd" data-open="1">
      <span class="tw">▾</span> ${esc(title)} <span class="muted">(${rows.length})</span></div>
    <div class="out-body"></div></div>`);
  const body = g.querySelector(".out-body");
  g.querySelector(".out-hd").onclick = e => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "";
    e.currentTarget.querySelector(".tw").textContent = open ? "▸" : "▾";
  };
  // All AI findings for this output are pooled together — aggregate / subtotal rows are shown inline alongside per-subject rows, not split into a separate sub-group.
  rows.forEach(r => body.appendChild(findingCard(r, loadFindings)));
  return g;
}

// Populate the model + effort selectors from the configured API account, preselecting the saved / default choice, then wire the estimate to update whenever either changes.
async function setupAIConfig() {
  const mSel = $("#aiModel"), eSel = $("#aiEffort");
  if (!mSel || !eSel) return;
  let cfg;
  try { cfg = await getJSON("/api/ai/models"); }
  catch { $("#aiEstimate").textContent = ""; return; }
  const savedM = localStorage.getItem("tlf_ai_model");
  const savedE = localStorage.getItem("tlf_ai_effort");
  mSel.innerHTML = (cfg.models || []).map(m =>
    `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  eSel.innerHTML = (cfg.efforts || []).map(x =>
    `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  mSel.value = savedM && [...mSel.options].some(o => o.value === savedM) ? savedM : (cfg.default_model || "");
  eSel.value = savedE && [...eSel.options].some(o => o.value === savedE) ? savedE : (cfg.default_effort || "high");
  const onChange = () => {
    localStorage.setItem("tlf_ai_model", mSel.value);
    localStorage.setItem("tlf_ai_effort", eSel.value);
    updateEstimate();
  };
  mSel.onchange = onChange;
  eSel.onchange = onChange;
  updateEstimate();
}

async function updateEstimate() {
  const box = $("#aiEstimate"); if (!box) return;
  const model = $("#aiModel") ? $("#aiModel").value : "";
  const effort = $("#aiEffort") ? $("#aiEffort").value : "";
  box.textContent = "estimating…";
  try {
    const r = await getJSON(`/api/projects/${state.project.id}/ai-estimate`
      + `?model=${encodeURIComponent(model)}&effort=${encodeURIComponent(effort)}`);
    box.textContent = r.text || "";
  } catch { box.textContent = ""; }
}

async function startRun(kind) {
  const model = $("#aiModel") ? $("#aiModel").value : "";
  const effort = $("#aiEffort") ? $("#aiEffort").value : "";
  try {
    const res = await postForm(`/api/projects/${state.project.id}/ai-run?kind=${kind}`, { model, effort });
    if (res.error) { toast(res.error, 4000); return; }
    renderRunAlert(null);   // clear any stale banner from a prior failed run
    $("#prog").classList.remove("hidden");
    pollProgress();
  } catch (err) { toast("Run failed: " + err.message, 4000); }
}
async function pollProgress() {
  const p = await getJSON(`/api/projects/${state.project.id}/ai-progress`, { quiet: true });
  const done = safeInt(p.done), total = safeInt(p.total);
  const pct = total ? Math.min(100, Math.round(100 * done / total)) : 0;
  $("#prog").firstElementChild.style.width = pct + "%";
  const lr = $("#lastRun");
  if (lr && p.running) lr.innerHTML = `${esc(p.message)}… (${done}/${total}) <span class="spin"></span>`;
  if (p.running) { setTimeout(pollProgress, 1200); }
  else {
    $("#prog").classList.add("hidden");
    loadLastRun();
    loadFindings();
    // The run may have auto-approved clean tables — re-fetch the project so those statuses show everywhere (TOC / TLF / Dashboard), then refresh bookmark counts.
    try {
      const fresh = await getJSON(`/api/projects/${state.project.id}`, { quiet: true });
      state.project.outputs = fresh.outputs;
      if (state.output) {
        const u = state.project.outputs.find(o => o.id === state.output.id);
        if (u) { state.output = u; const sel = $("#statusSel"); if (sel) sel.value = u.status; }
      }
    } catch (e) {}
    await loadBookmarkMeta();
    if (state.tab === "tlf") renderBmTree();
  }
}

async function sendGlobalChat() {
  const q = $("#gq").value.trim(); if (!q) return;
  const log = $("#gchat");
  log.appendChild(el(`<div class="chat-msg user"><span class="bubble">${esc(q)}</span></div>`));
  $("#gq").value = "";
  const wait = el(`<div class="chat-msg"><span class="bubble"><span class="spin"></span></span></div>`);
  log.appendChild(wait); log.scrollTop = log.scrollHeight;
  try {
    const r = await postForm("/api/chat", { scope: "global", project_id: state.project.id, question: q });
    wait.querySelector(".bubble").textContent = r.answer;
  } catch (err) { wait.querySelector(".bubble").textContent = "Error: " + err.message; }
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- Comments tab
async function renderComments() {
  const pid = state.project.id;
  app().innerHTML = `<div class="toolbar">
      <button class="btn" id="exp">⬇ Export to Excel</button>
      <button class="btn" id="imp" title="Re-import an edited comments sheet (ID | Table | Comment | Reply to | Resolved). An (ID, Table) that already exists is replaced; a new (ID, Table) with an existing Table is created.">⬆ Import from Excel</button>
      <input type="file" id="impCmtFile" accept=".xlsx" hidden>
      <button class="btn ghost" id="expPdf">⬇ Export annotated PDF</button>
      <label class="muted"><input type="checkbox" id="grp" checked> Group by table</label>
      <span id="cCount" class="muted" style="margin-left:auto"></span>
    </div>
    <div class="table-wrap"><table class="grid"><thead><tr>
      <th>ID</th><th>Table</th><th>Comment</th><th>Reply to</th><th>Resolved</th><th>Actions</th>
    </tr></thead><tbody id="cBody"></tbody></table></div>`;
  $("#exp").onclick = () => { location.href = `/api/projects/${pid}/export/comments.xlsx`; };
  $("#expPdf").onclick = () => { location.href = `/api/projects/${pid}/export/annotated.pdf`; };
  $("#imp").onclick = () => $("#impCmtFile").click();
  $("#impCmtFile").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    e.target.value = "";   // allow re-selecting the same file later
    const btn = $("#imp"), label = btn.textContent;
    btn.disabled = true; btn.textContent = "Importing…";
    try {
      const fd = new FormData(); fd.append("file", f);
      const r = await postForm(`/api/projects/${pid}/import-comments`, fd);
      toast(`Updated ${r.updated} · created ${r.created} from "${f.name}"`, 3500);
      draw();
    } catch (err) {
      let msg = err.message;
      try { const j = JSON.parse(msg); if (j && j.detail) msg = j.detail; } catch {}
      toast("Import failed: " + msg, 9000);
    }
    finally { btn.disabled = false; btn.textContent = label; }
  };

  // A flat row per comment (ID | Table | Comment | Reply to | Resolved | Actions). "Reply to" shows the parent comment's per-Table ID; grouped rows leave Table blank (the group header carries it). byId maps global comment id → row so a reply can find its parent's num.
  const commentRow = (c, byId, grouped) => {
    const parent = c.parent_id != null ? byId[c.parent_id] : null;
    const tr = el(`<tr>
      <td>${esc(c.num ?? "")}</td>
      <td>${grouped ? "" : esc(c.output_label || "")}</td>
      <td><span class="c-body">${esc(c.body).replace(/\n/g, "<br>")}</span></td>
      <td>${parent ? esc(parent.num ?? "") : ""}</td>
      <td><input type="checkbox" ${c.resolved ? "checked" : ""} data-res></td>
      <td class="row-actions"><a class="link" data-reply>reply</a> · <a class="link" data-del>delete</a></td></tr>`);
    tr.querySelector("[data-del]").onclick = async () => { await del("/api/comments/" + c.id); draw(); };
    tr.querySelector("[data-reply]").onclick = async () => {
      const body = prompt("Reply:"); if (!body) return;
      await postForm(`/api/comments/${c.id}/reply`, { body }); draw();
    };
    tr.querySelector("[data-res]").onclick = async e => {
      await postForm(`/api/comments/${c.id}/resolve`, { resolved: e.target.checked ? 1 : 0 }); draw();
    };
    return tr;
  };

  const draw = async () => {
    const all = await getJSON(`/api/projects/${pid}/comments`);
    const byId = {}; all.forEach(c => (byId[c.id] = c));
    const body = $("#cBody"); body.innerHTML = "";
    $("#cCount").textContent = `${all.length} comment${all.length === 1 ? "" : "s"}`;
    if (!all.length) { body.innerHTML = '<tr><td colspan="6" class="muted">No comments.</td></tr>'; return; }

    const byNum = (a, b) => (a.num || 0) - (b.num || 0);
    if ($("#grp").checked) {
      const groups = {};
      all.forEach(c => (groups[c.output_label || "(no table)"] ||= []).push(c));
      Object.keys(groups).sort().forEach(lbl => {
        const t = groups[lbl][0];
        body.appendChild(el(`<tr class="grp-row"><td colspan="6"><b>${esc(lbl)}</b>
          ${t.output_title ? `— <span class="muted">${esc(t.output_title)}</span>` : ""}</td></tr>`));
        groups[lbl].slice().sort(byNum).forEach(c => body.appendChild(commentRow(c, byId, true)));
      });
    } else {
      all.slice().sort((a, b) =>
        (a.output_label || "").localeCompare(b.output_label || "") || byNum(a, b)
      ).forEach(c => body.appendChild(commentRow(c, byId, false)));
    }
  };
  $("#grp").onchange = draw;
  draw();
}

// ---------------------------------------------------------------- boot
window.addEventListener("resize", () => { if (state.tab === "tlf" && state.pdf) gotoPage(state.pageNum); });
async function boot() {
  try {
    const info = await getJSON("/api/runtime-info", { quiet: true });
    const banner = $("#demoBanner");
    if (info.demo_mode && banner) {
      banner.textContent = info.notice;
      banner.classList.remove("hidden");
      document.documentElement.classList.add("demo-mode");
    }
  } catch (_) { /* The main UI still loads if this informational endpoint is unavailable. */ }
  const direct = location.hash.match(
    /^#project\/(\d+)(?:\/(dashboard|toc|tlf|ai|comments)(?:\/(\d+))?)?$/
  );
  if (direct) {
    try {
      await openProject(Number(direct[1]));
      if (direct[3]) {
        const chosen = (state.project.outputs || []).find(o => o.id === Number(direct[3]));
        if (chosen) state.output = chosen;
      }
      if (direct[2]) setTab(direct[2]);
      return;
    } catch (_) {}
  }
  goHome();
}
boot();
