/*
 * Shared frontend runtime: API client, app shell, and small formatting
 * helpers used by every screen in the New Discovery flow.
 *
 * Deliberately framework-free, matching the existing dashboard.html. The
 * pages are served by the FastAPI app itself (mounted at /app), so API
 * calls are same-origin and relative.
 */

const API = {
  async request(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail;
      try {
        const body = await response.json();
        detail = body.detail;
      } catch (_) {
        detail = await response.text();
      }
      const error = new Error(
        typeof detail === "string"
          ? detail
          : (detail && detail.message) || `Request failed (${response.status})`
      );
      error.status = response.status;
      error.checks = (detail && detail.checks) || [];
      throw error;
    }
    const type = response.headers.get("content-type") || "";
    return type.includes("application/json") ? response.json() : response.text();
  },

  get(path) {
    return API.request(path);
  },

  postJSON(path, body) {
    return API.request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  postForm(path, formData) {
    return API.request(path, { method: "POST", body: formData });
  },

  // --- domain calls -----------------------------------------------------
  demoTarget: () => API.postJSON("/api/targets/demo", {}),
  targetByPdbId: (pdbId) => API.postJSON("/api/targets/pdb-id", { pdb_id: pdbId }),
  uploadTarget: (file) => {
    const data = new FormData();
    data.append("file", file);
    return API.postForm("/api/targets/upload", data);
  },
  target: (id) => API.get(`/api/targets/${id}`),
  structure: (id) => API.get(`/api/targets/${id}/structure`),
  tools: () => API.get("/api/tools"),
  sites: (id, padding = 6) => API.get(`/api/targets/${id}/sites?padding=${padding}`),
  validateBox: (id, body) => API.postJSON(`/api/targets/${id}/sites/validate-box`, body),
  jobs: () => API.get("/api/jobs"),
  job: (id) => API.get(`/api/jobs/${id}`),
  createJob: (payload) => API.postJSON("/api/jobs", payload),
  batches: (id, stage) => API.get(`/api/jobs/${id}/batches${stage ? `?stage=${stage}` : ""}`),
  batchLog: (id, batchId) => API.get(`/api/jobs/${id}/batches/${batchId}/log`),
  molecules: (id, outcome) => API.get(`/api/jobs/${id}/molecules${outcome ? `?outcome=${outcome}` : ""}`),
  candidates: (id) => API.get(`/api/jobs/${id}/candidates`),
  analogues: (id) => API.get(`/api/jobs/${id}/analogues`),
  candidate: (id) => API.get(`/api/candidates/${id}`),
  complex: (id) => API.get(`/api/candidates/${id}/complex`),
  artifacts: (id) => API.get(`/api/jobs/${id}/artifacts`),
  cancel: (id) => API.postJSON(`/api/jobs/${id}/cancel`, {}),
  resume: (id) => API.postJSON(`/api/jobs/${id}/resume`, {}),
  retry: (id, body) => API.postJSON(`/api/jobs/${id}/retry`, body),
};

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------
const fmt = {
  int: (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString()),
  dash: (value) => (value === null || value === undefined || value === "" ? "—" : value),
  bytes(value) {
    if (!value && value !== 0) return "—";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  },
  resolution: (value) => (value ? `${Number(value).toFixed(2)} Å` : null),
  when(iso) {
    if (!iso) return "—";
    const date = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
    if (Number.isNaN(date.getTime())) return "—";
    const diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diffSeconds < 60) return "just now";
    if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} min ago`;
    if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} h ago`;
    return date.toLocaleDateString();
  },
  elapsed(iso, endIso) {
    if (!iso) return "—";
    const start = new Date(iso.endsWith("Z") ? iso : `${iso}Z`).getTime();
    const end = endIso
      ? new Date(endIso.endsWith("Z") ? endIso : `${endIso}Z`).getTime()
      : Date.now();
    const seconds = Math.max(0, Math.floor((end - start) / 1000));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  },
  chainLabel(chain) {
    if (!chain) return "—";
    const range =
      chain.first_residue !== null && chain.last_residue !== null
        ? ` (${chain.first_residue}–${chain.last_residue})`
        : "";
    return `${chain.chain_id}${range}`;
  },
};

/** HTML-escape untrusted text before it goes into innerHTML. Anything that
 *  originates in an uploaded file (title, filename, chain IDs, error text
 *  quoting file content) must pass through this. */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

const params = new URLSearchParams(window.location.search);

// ---------------------------------------------------------------------------
// App shell — header, breadcrumb, wizard rail
// ---------------------------------------------------------------------------
const WIZARD_STEPS = [
  { key: "intake", no: "01", label: "Target Intake", icon: "biotech", href: "new-discovery.html" },
  { key: "validation", no: "02", label: "Target Validation", icon: "fact_check", href: null },
  { key: "configure", no: "03", label: "Configure & Review", icon: "tune", href: null },
  { key: "run", no: "04", label: "Discovery Run", icon: "analytics", href: null },
];

const Shell = {
  /**
   * @param {object} options
   *  - active: wizard step key, or null on the Discoveries index
   *  - breadcrumb: array of {label, href?}
   *  - context: array of {label, value} shown on the right of the sub-bar
   */
  render({ active = null, breadcrumb = [], context = [] } = {}) {
    const header = document.getElementById("app-header");
    const sidebar = document.getElementById("app-sidebar");
    if (header) header.innerHTML = Shell.headerHTML(breadcrumb, context);
    if (sidebar) sidebar.innerHTML = Shell.sidebarHTML(active);
  },

  headerHTML(breadcrumb, context) {
    const crumbs = breadcrumb
      .map((crumb, index) => {
        const last = index === breadcrumb.length - 1;
        const inner = crumb.href
          ? `<a class="text-outline hover:text-on-surface transition-colors" href="${crumb.href}">${esc(crumb.label)}</a>`
          : `<span class="${last ? "text-primary font-semibold" : "text-outline"}">${esc(crumb.label)}</span>`;
        const sep = index > 0
          ? '<span class="material-symbols-outlined text-[12px] text-outline-variant">chevron_right</span>'
          : "";
        return sep + inner;
      })
      .join("");

    const contextHTML = context
      .map(
        (item) => `
        <div class="hidden lg:flex items-center gap-space-xs">
          <span class="text-outline">${esc(item.label)}:</span>
          <span class="text-on-surface font-semibold">${esc(item.value)}</span>
        </div>`
      )
      .join("");

    return `
      <div class="h-14 w-full px-space-lg flex items-center justify-between gap-space-lg">
        <div class="flex items-center gap-space-lg">
          <a class="flex items-center gap-space-sm" href="discoveries.html">
            <span class="w-8 h-8 rounded bg-primary flex items-center justify-center">
              <span class="material-symbols-outlined text-on-primary text-[18px]">hub</span>
            </span>
            <span class="font-headline-sm text-headline-sm text-on-surface tracking-tight font-semibold">
              Molecular Discovery Platform
            </span>
          </a>
          <nav class="hidden xl:flex items-center gap-space-xs text-body-sm font-body-sm">
            <a class="px-space-md py-space-xs rounded bg-surface-container-high text-primary font-semibold" href="discoveries.html">Discoveries</a>
            <span class="px-space-md py-space-xs rounded text-outline-variant cursor-not-allowed" title="Not implemented in this MVP">Projects</span>
            <span class="px-space-md py-space-xs rounded text-outline-variant cursor-not-allowed" title="Not implemented in this MVP">Molecules</span>
            <span class="px-space-md py-space-xs rounded text-outline-variant cursor-not-allowed" title="Not implemented in this MVP">Jobs</span>
          </nav>
        </div>
        <div class="flex items-center gap-space-md">
          <span class="hidden sm:flex items-center gap-space-xs px-space-sm py-space-2xs bg-surface-container-low rounded border border-surface-container-high font-mono-sm text-mono-sm text-on-surface-variant">
            <span class="w-1.5 h-1.5 rounded-full bg-secondary"></span>
            Live
          </span>
          <a href="new-discovery.html"
             class="px-space-md py-space-xs bg-primary hover:bg-primary-container text-on-primary rounded font-headline-sm text-headline-sm transition-colors flex items-center gap-space-xs">
            <span class="material-symbols-outlined text-[16px]">add</span>
            New Discovery
          </a>
        </div>
      </div>
      <div class="h-9 w-full px-space-lg bg-surface border-t border-surface-container-high flex items-center justify-between">
        <nav class="flex items-center gap-space-xs font-mono-data text-mono-data">
          <span class="text-outline">WORKSPACE</span>
          <span class="material-symbols-outlined text-[12px] text-outline-variant">chevron_right</span>
          ${crumbs}
        </nav>
        <div class="flex items-center gap-space-lg font-mono-sm text-mono-sm">${contextHTML}</div>
      </div>`;
  },

  sidebarHTML(active) {
    const activeIndex = WIZARD_STEPS.findIndex((step) => step.key === active);
    const steps = WIZARD_STEPS.map((step, index) => {
      const isActive = step.key === active;
      const isDone = activeIndex > -1 && index < activeIndex;
      const classes = isActive
        ? "bg-surface-container-high text-primary font-semibold"
        : isDone
        ? "text-on-surface-variant"
        : "text-outline";
      const icon = isDone ? "check_circle" : step.icon;
      const body = `
        <span class="flex items-center gap-space-sm">
          <span class="material-symbols-outlined text-[16px]">${icon}</span>${step.label}
        </span>
        <span class="font-mono-sm text-mono-sm text-outline">${step.no}</span>`;
      return `<div class="flex items-center justify-between px-space-sm py-space-xs rounded text-body-sm font-body-sm ${classes}">${body}</div>`;
    }).join("");

    return `
      <div class="p-space-md">
        <div class="font-label-caps text-label-caps text-outline uppercase tracking-wider mb-space-sm px-space-xs">
          Discovery Flow
        </div>
        <nav class="space-y-space-2xs">${steps}</nav>

        <div class="mt-space-lg font-label-caps text-label-caps text-outline uppercase tracking-wider mb-space-sm px-space-xs">
          This server
        </div>
        <div id="sidebar-limits" class="px-space-xs font-mono-sm text-mono-sm text-on-surface-variant"></div>
      </div>
      <div class="p-space-md border-t border-surface-container-high bg-surface-container-low">
        <div class="font-label-caps text-label-caps text-outline uppercase tracking-wider mb-space-xs">
          Scientific Engines
        </div>
        <div id="sidebar-engines" class="space-y-space-2xs font-mono-sm text-mono-sm text-on-surface-variant">
          <div class="text-outline">Loading…</div>
        </div>
      </div>`;
  },

  /** Fills the sidebar from /api/tools: what can really run here. Never hardcoded. */
  async loadEngines() {
    const host = document.getElementById("sidebar-engines");
    if (!host) return;
    try {
      const info = await API.tools();
      const rows = Object.values(info.tools).map((t) => `
        <div class="flex items-center justify-between gap-space-xs" title="${esc(t.reason || t.license_detail || t.version || "")}">
          <span class="truncate">${esc(t.name.replace("BioSolveIT ", ""))}</span>
          <span class="shrink-0 px-space-xs py-space-2xs rounded text-mono-sm ${t.available ? "bg-secondary-container/60 text-on-secondary-container font-semibold" : "bg-error-container text-on-error-container"}">${t.available ? "ready" : "not installed"}</span>
        </div>`);
      rows.push(...info.models.map((m) => `
        <div class="flex items-center justify-between gap-space-xs"><span class="truncate">ADMET-AI</span>
        <span class="shrink-0 px-space-xs py-space-2xs rounded text-mono-sm bg-secondary-container/60 text-on-secondary-container font-semibold">ready</span></div>`));
      rows.push(...info.mocked.map((m) => `
        <div class="flex items-center justify-between gap-space-xs" title="${esc(m.reason)}"><span class="truncate">${esc(m.name)}</span>${badgeHTML(true)}</div>`));
      host.innerHTML = rows.join("");
      const lim = document.getElementById("sidebar-limits");
      if (lim) lim.innerHTML = `
        <div class="p-space-sm rounded bg-surface-container-low border border-surface-container-high space-y-space-2xs">
          <div class="flex justify-between"><span class="text-outline">CPU budget</span><span>${info.limits.cpu_budget} threads</span></div>
          <div class="flex justify-between"><span class="text-outline">Parallel batches</span><span>${info.limits.max_parallel_batches}</span></div>
          <div class="flex justify-between"><span class="text-outline">Batch size</span><span>${info.limits.batch_size}</span></div>
        </div>`;
    } catch (error) {
      host.innerHTML = `<div class="text-error">Backend unreachable</div>`;
    }
  },
};

function badgeHTML(isMocked) {
  return isMocked
    ? '<span class="shrink-0 px-space-xs py-space-2xs rounded bg-surface-container text-on-surface-variant text-mono-sm">mocked</span>'
    : '<span class="shrink-0 px-space-xs py-space-2xs rounded bg-secondary-container/60 text-on-secondary-container text-mono-sm font-semibold">real</span>';
}

// ---------------------------------------------------------------------------
// Inline messaging
// ---------------------------------------------------------------------------
function showError(hostId, message, checks = []) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const checkList = (checks || [])
    .map(
      (check) => `
      <li class="flex items-start gap-space-xs">
        <span class="material-symbols-outlined text-[14px] mt-0.5 ${check.passed ? "text-secondary" : "text-error"}">
          ${check.passed ? "check_circle" : "cancel"}
        </span>
        <span><strong>${esc(check.label)}</strong> — ${esc(check.detail)}</span>
      </li>`
    )
    .join("");
  host.innerHTML = `
    <div class="p-space-md rounded bg-error-container border-l-2 border-error space-y-space-xs">
      <div class="flex items-center gap-space-xs text-on-error-container font-headline-sm text-headline-sm">
        <span class="material-symbols-outlined text-[18px]">error</span>
        Could not use this structure
      </div>
      <p class="font-body-default text-body-default text-on-error-container">${esc(message)}</p>
      ${checkList ? `<ul class="font-body-sm text-body-sm text-on-error-container space-y-space-2xs mt-space-xs">${checkList}</ul>` : ""}
    </div>`;
  host.hidden = false;
}

function clearError(hostId) {
  const host = document.getElementById(hostId);
  if (host) {
    host.innerHTML = "";
    host.hidden = true;
  }
}
