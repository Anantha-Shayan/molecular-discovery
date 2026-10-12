/* Campaign monitor. Every number shown here is read from the server's saved state. */
const jobId = params.get("job");
const S = { job: null, stage: null, tab: "ranked", selected: new Set(), timer: null, cands: null, mols: null };

const STATUS_STYLE = {
  pending: "bg-surface-container text-on-surface-variant", running: "bg-primary/15 text-primary",
  succeeded: "bg-secondary-container/60 text-on-secondary-container", failed: "bg-error-container text-on-error-container",
  retrying: "bg-warning-container text-warning", skipped: "bg-surface-container-high text-outline",
  cancelled: "bg-warning-container text-warning",
  QUEUED: "bg-surface-container text-on-surface-variant", RUNNING: "bg-primary/15 text-primary",
  COMPLETE: "bg-secondary-container/60 text-on-secondary-container", FAILED: "bg-error-container text-on-error-container",
  CANCELLED: "bg-warning-container text-warning",
};
const NICE = { QUEUED: "Waiting to start", RUNNING: "Running", COMPLETE: "Complete", FAILED: "Stopped — needs attention", CANCELLED: "Cancelled",
  pending: "Pending", running: "Running", succeeded: "Done", failed: "Failed", retrying: "Retrying", skipped: "Skipped", cancelled: "Cancelled" };
const chip = (st, text) => `<span class="inline-flex items-center gap-space-xs px-space-sm py-space-2xs rounded font-mono-sm text-mono-sm font-medium ${STATUS_STYLE[st] || ""}">${["running", "RUNNING", "retrying"].includes(st) ? '<span class="w-1.5 h-1.5 rounded-full bg-current animate-pulse"></span>' : ""}${esc(text || NICE[st] || st)}</span>`;
const real = (mocked) => mocked
  ? '<span class="px-space-xs rounded bg-surface-container-high text-on-surface-variant font-mono-sm text-mono-sm" title="Placeholder values, not a prediction">placeholder</span>'
  : '<span class="px-space-xs rounded bg-secondary-container/60 text-on-secondary-container font-mono-sm text-mono-sm font-semibold" title="Computed by a real tool or model">real</span>';
const UNIT = { kcal: false };
try { UNIT.kcal = localStorage.getItem("mdp-energy-unit") === "kcal"; } catch (_) {}
const energy = (kj, d = 1) => (kj === null || kj === undefined ? "—" : (UNIT.kcal ? kj / 4.184 : kj).toFixed(d) + (UNIT.kcal ? " kcal/mol" : " kJ/mol"));
const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const sci = (v) => (v === null || v === undefined ? "—" : v >= 1e6 ? (v / 1e6).toPrecision(3) + " mM" : v >= 1e3 ? (v / 1e3).toPrecision(3) + " µM" : v.toPrecision(3) + " nM");

// ---------------------------------------------------------------- render ---
function renderTitle(j) {
  document.getElementById("t-label").textContent = j.label || "Campaign";
  document.getElementById("t-status").innerHTML = chip(j.status) + (j.cancel_requested ? " " + chip("cancelled", "Cancelling…") : "");
  const c = j.config;
  document.getElementById("t-meta").innerHTML = [
    `Protein ${esc(j.target?.pdb_id || j.target?.name || "—")} chain ${esc(c.chain || "—")}`,
    `Docking ${c.engine === "vina" ? "AutoDock Vina" : "BioSolveIT FlexX"}`,
    `Library ${esc(c.library_source)} × ${c.library_count}`,
    `Started ${fmt.when(j.started_at || j.created_at)}`, `Elapsed ${fmt.elapsed(j.started_at || j.created_at, j.finished_at)}`,
  ].map((x) => `<span>${x}</span>`).join("");
  const o = j.molecule_outcomes || {};
  const total = (o.active || 0) + (o.filtered || 0) + (o.failed || 0);
  document.getElementById("t-summary").textContent = total
    ? `${total} molecules entered. ${o.active || 0} still in contention, ${o.filtered || 0} removed by a stated rule, ${o.failed || 0} could not be processed.`
    : "Preparing the protein and fetching molecules…";
  const a = [];
  if (j.can_cancel) a.push(`<button data-act="cancel" class="h-9 px-space-md bg-surface-container-low hover:bg-surface-container rounded font-body-sm">Cancel</button>`);
  if (j.can_resume) a.push(`<button data-act="resume" class="h-9 px-space-md bg-primary text-on-primary rounded font-body-sm">Resume from saved progress</button>`);
  document.getElementById("t-actions").innerHTML = a.join("");
  const alert = document.getElementById("t-alert");
  if (j.status === "FAILED") {
    alert.hidden = false;
    alert.innerHTML = `<div class="p-space-md rounded bg-error-container text-on-error-container border-l-2 border-error"><div class="font-headline-sm text-headline-sm">The campaign stopped</div>
      <p class="font-body-default">${esc(j.failure_reason || "A step failed.")}</p>
      <p class="font-body-sm mt-space-xs">Finished work is saved. Open the failed step below to read the reason and the tool's log, then retry the failed batches or resume.</p></div>`;
  } else if (j.status === "CANCELLED") {
    alert.hidden = false;
    alert.innerHTML = `<div class="p-space-md rounded bg-warning-container text-warning">Cancelled. Finished work is kept; resume to continue.</div>`;
  } else alert.hidden = true;
  document.getElementById("rank-policy").textContent = j.rank_policy;
}

function segBar(b) {
  if (!b || !b.total) return "";
  const parts = [["succeeded", "bg-secondary"], ["running", "bg-primary animate-pulse"], ["retrying", "bg-warning"], ["failed", "bg-error"], ["cancelled", "bg-warning"], ["pending", "bg-surface-container-high"]]
    .flatMap(([k, cls]) => Array(b[k] || 0).fill(`<span class="h-2 flex-1 rounded-sm ${cls}"></span>`));
  return `<div class="flex gap-[2px] mt-space-xs">${parts.join("")}</div>
    <div class="mt-space-2xs font-mono-sm text-mono-sm text-outline">${b.succeeded}/${b.total} batches done${b.failed ? ` · <span class="text-error font-semibold">${b.failed} failed</span>` : ""}${b.running ? ` · ${b.running} running` : ""}</div>`;
}

function renderStages(j) {
  document.getElementById("stages").innerHTML = j.stages.map((s, i) => {
    const sel = S.stage === s.key;
    const counts = s.status === "skipped" ? "" : `<div class="mt-space-xs font-mono-data text-mono-data"><span class="text-outline">in</span> ${fmt.int(s.count_in)} <span class="text-outline">→ out</span> <strong>${fmt.int(s.count_out)}</strong>${s.count_failed ? ` <span class="text-error">· ${s.count_failed} failed</span>` : ""}</div>`;
    return `<button data-stage="${s.key}" class="text-left p-space-sm rounded border ${sel ? "border-primary bg-primary/5" : "border-surface-container-high bg-surface-container-lowest"} hover:border-primary transition-colors ${s.status === "skipped" ? "opacity-60" : ""}">
      <div class="flex items-center justify-between"><span class="font-mono-sm text-mono-sm text-outline">${String(i + 1).padStart(2, "0")}</span>${chip(s.status)}</div>
      <div class="mt-space-xs font-headline-sm text-headline-sm">${esc(s.label)} ${real(s.mocked)}</div>
      <div class="font-body-sm text-body-sm text-on-surface-variant mt-space-2xs">${esc(s.blurb)}</div>
      ${s.tool ? `<div class="mt-space-xs font-mono-sm text-mono-sm text-outline truncate" title="${esc(s.tool)}">${esc(s.tool)}</div>` : ""}
      ${counts}${segBar(s.batches)}
      ${s.detail && ["succeeded", "failed"].includes(s.status) ? `<div class="mt-space-xs font-body-sm text-body-sm ${s.status === "failed" ? "text-error" : "text-on-surface-variant"}">${esc(s.detail)}</div>` : ""}
    </button>`;
  }).join("");
}

async function renderBatches() {
  const sec = document.getElementById("batch-section");
  const st = S.job.stages.find((x) => x.key === S.stage);
  if (!st || !st.batched) { sec.hidden = true; return; }
  sec.hidden = false;
  document.getElementById("b-title").textContent = `Batches — ${st.label}`;
  const { batches } = await API.batches(jobId, S.stage);
  const canRetry = !["RUNNING", "QUEUED"].includes(S.job.status);
  document.getElementById("b-rows").innerHTML = batches.length ? batches.map((b) => {
    const retriable = canRetry && ["failed", "cancelled"].includes(b.status);
    return `<tr class="align-top">
      <td class="p-space-sm">${retriable ? `<input type="checkbox" data-batch="${b.batch_id}" ${S.selected.has(b.batch_id) ? "checked" : ""}>` : ""}</td>
      <td class="p-space-sm font-semibold">#${b.index}</td><td class="p-space-sm">${chip(b.status)}</td>
      <td class="p-space-sm">${b.attempt}</td><td class="p-space-sm text-right">${b.size}</td>
      <td class="p-space-sm text-right">${b.ok}</td><td class="p-space-sm text-right ${b.failed ? "text-error font-semibold" : ""}">${b.failed}</td>
      <td class="p-space-sm text-right">${b.seconds ? b.seconds.toFixed(1) + " s" : b.started_at && b.status === "running" ? fmt.elapsed(b.started_at) : "—"}</td>
      <td class="p-space-sm text-error max-w-md break-words">${esc(b.error || "")}</td>
      <td class="p-space-sm">${b.has_log ? `<button data-log="${b.batch_id}" data-idx="${b.index}" class="underline text-primary">log</button>` : ""}</td></tr>`;
  }).join("") : `<tr><td colspan="10" class="p-space-md text-outline">No batches yet for this step.</td></tr>`;
  const failedMols = (S.job.stages.find((x) => x.key === S.stage)?.count_failed) || 0;
  const btn = document.getElementById("b-retry");
  btn.disabled = !S.selected.size && !(canRetry && failedMols);
  btn.textContent = S.selected.size ? `Retry ${S.selected.size} selected batch${S.selected.size > 1 ? "es" : ""}` : failedMols && canRetry ? `Retry ${failedMols} failed molecule${failedMols > 1 ? "s" : ""}` : "Retry selected batches";
}

function candRow(c) {
  return `<tr class="hover:bg-surface-container-low/70 cursor-pointer align-middle" data-mol="${c.molecule_id}">
    <td class="p-space-sm font-semibold">${c.rank}</td>
    <td class="p-space-sm"><div class="structure-cell w-36 h-20 bg-white rounded">${c.structure_svg || ""}</div></td>
    <td class="p-space-sm"><div class="font-semibold">${esc(c.display_id)}</div><div class="text-outline">${esc(c.source_id || c.source || "")}</div></td>
    <td class="p-space-sm text-right">${num(c.docking_score, 1)} <span class="text-outline">${esc(c.docking_unit || "")}</span></td>
    <td class="p-space-sm text-right">${c.hyde_cumulative_kj_mol === null ? "—" : energy(c.hyde_cumulative_kj_mol)}
      <div class="text-outline">${c.hyde_affinity_lower_nm === null ? "" : sci(c.hyde_affinity_lower_nm) + " – " + sci(c.hyde_affinity_upper_nm)}</div></td>
    <td class="p-space-sm text-right">${num(c.sa_score, 1)}</td>
    <td class="p-space-sm">${c.admet_profile ? chip(c.admet_profile === "Favorable" ? "succeeded" : c.admet_profile === "Moderate" ? "retrying" : "failed", c.admet_profile) : "—"}
      ${c.admet_tox_flags ? `<div class="text-error">${esc(c.admet_tox_flags)}</div>` : ""}</td>
    <td class="p-space-sm text-outline text-right" title="Placeholder values — Koffee not connected">${c.affinity_kd_nm_MOCKED === null ? "—" : num(c.affinity_kd_nm_MOCKED, 1) + " nM*"}<div>${c.residence_time_min_MOCKED === null ? "" : num(c.residence_time_min_MOCKED, 0) + " min*"}</div></td></tr>`;
}

async function renderResults() {
  const host = document.getElementById("res-body");
  const j = S.job;
  const tabs = [["ranked", "Ranked candidates"], ["filtered", "Removed by a rule"], ["failed", "Could not be processed"]];
  if (j.config.enable_grow) tabs.push(["analogues", "Generated analogues"]);
  document.getElementById("tabs").innerHTML = tabs.map(([k, l]) => `<button data-tab="${k}" class="px-space-md py-space-xs rounded ${S.tab === k ? "bg-surface-container-lowest shadow-sm font-semibold" : "text-on-surface-variant"}">${l}</button>`).join("");
  const sdf = document.getElementById("dl-sdf"), csv = document.getElementById("dl-csv");
  sdf.href = `/api/jobs/${jobId}/candidates.sdf`; csv.href = `/api/jobs/${jobId}/candidates.csv`;
  if (S.tab === "analogues") {
    document.getElementById("dl-sdf").href = `/api/jobs/${jobId}/analogues.sdf`;
    const d = await API.analogues(jobId);
    host.innerHTML = d.analogues.length ? `<div class="mx-space-md mb-space-sm p-space-sm rounded bg-warning-container text-warning font-body-sm">
      These molecules were <strong>generated by FastGrow</strong> from the top candidates. They are not catalogue compounds, have not been synthesised or tested,
      and their novelty has not been checked. The FastGrow score is a fit score for the grown pose, not a binding affinity, and cannot be compared with docking or HYDE values.</div>
      <table class="w-full text-left font-mono-data text-mono-data"><thead><tr class="font-label-caps text-label-caps text-outline uppercase bg-surface-container-low/60">
      <th class="p-space-sm">Structure</th><th class="p-space-sm">Molecule</th><th class="p-space-sm">Grown from</th><th class="p-space-sm text-right">FastGrow score</th><th class="p-space-sm text-right">SA score</th><th class="p-space-sm text-right">Heavy atoms</th></tr></thead>
      <tbody class="divide-y divide-surface-container-high/60">${d.analogues.map((a) => `<tr><td class="p-space-sm"><div class="structure-cell w-36 h-20 bg-white rounded">${a.structure_svg}</div></td>
      <td class="p-space-sm font-semibold">${esc(a.display_id)}<div class="text-outline font-normal break-all max-w-xs">${esc(a.smiles)}</div></td><td class="p-space-sm">${esc(a.parent)}</td>
      <td class="p-space-sm text-right">${num(a.fastgrow_score, 1)}</td><td class="p-space-sm text-right">${num(a.sa_score, 1)}</td><td class="p-space-sm text-right">${a.heavy_atoms}</td></tr>`).join("")}</tbody></table>`
      : `<div class="p-space-xl text-center text-outline">${["QUEUED", "RUNNING"].includes(j.status) ? "Analogues appear when the ranking is done." : "FastGrow did not find fitting fragments for the top candidates."}</div>`;
    return;
  }
  if (S.tab === "ranked") {
    const d = await API.candidates(jobId);
    const rows = d.candidates;
    if (!rows.length) {
      host.innerHTML = `<div class="p-space-xl text-center text-outline">${["QUEUED", "RUNNING"].includes(j.status) ? "Candidates appear here as soon as the scoring steps have finished." : "No molecule passed every filter. Open 'Removed by a rule' to see why, or loosen the filters in a new campaign."}</div>`;
      return;
    }
    host.innerHTML = `${d.provisional ? '<div class="px-space-md pb-space-sm font-body-sm text-warning">Provisional: the campaign is still running, so this list and its order may change.</div>' : ""}
      <table class="w-full text-left font-mono-data text-mono-data"><thead><tr class="font-label-caps text-label-caps text-outline uppercase bg-surface-container-low/60">
      <th class="p-space-sm">#</th><th class="p-space-sm">Structure</th><th class="p-space-sm">Molecule</th>
      <th class="p-space-sm text-right" title="Predicted fit in the pocket by the docking program. Not comparable between programs.">Docking score</th>
      <th class="p-space-sm text-right" title="HYDE total binding-energy estimate, and its estimated affinity range">HYDE</th>
      <th class="p-space-sm text-right" title="1 easy to make … 10 very hard">SA score</th><th class="p-space-sm">ADMET</th>
      <th class="p-space-sm text-right" title="Placeholders">Koffee*</th></tr></thead>
      <tbody class="divide-y divide-surface-container-high/60">${rows.map(candRow).join("")}</tbody></table>
      <p class="p-space-md font-mono-sm text-mono-sm text-outline">* Placeholder values; the Koffee tools are not connected. They do not affect the ranking. Click a row for details.</p>`;
  } else {
    const d = await API.molecules(jobId, S.tab);
    host.innerHTML = d.molecules.length ? `<table class="w-full text-left font-mono-data text-mono-data"><thead><tr class="font-label-caps text-label-caps text-outline uppercase bg-surface-container-low/60">
      <th class="p-space-sm">Molecule</th><th class="p-space-sm">Left at</th><th class="p-space-sm">Reason</th><th class="p-space-sm">SMILES</th></tr></thead><tbody class="divide-y divide-surface-container-high/60">
      ${d.molecules.map((m) => `<tr class="hover:bg-surface-container-low/70 cursor-pointer" data-mol="${m.molecule_id}"><td class="p-space-sm font-semibold">${esc(m.display_id)}</td><td class="p-space-sm">${esc(m.exit_stage || "")}</td>
      <td class="p-space-sm ${S.tab === "failed" ? "text-error" : ""}">${esc(m.exit_reason || "")}</td><td class="p-space-sm text-outline break-all max-w-xs">${esc(m.smiles)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="p-space-xl text-center text-outline">${S.tab === "failed" ? "No molecule failed inside a tool." : "No molecule has been removed yet."}</div>`;
  }
}

function renderSide(j) {
  const c = j.config;
  const rows = [["Protein", `${j.target?.pdb_id || j.target?.name} chain ${c.chain}`], ["Pocket", c.site.mode === "ligand" ? `around ${c.site.reference}` : "manual box"],
    ["Box centre (Å)", c.site.center.join(", ")], ["Box size (Å)", c.site.size.join(" × ")], ["Docking effort", c.engine === "vina" ? `exhaustiveness ${c.exhaustiveness}` : "FlexX defaults"],
    ["Docking cut", `best ${Math.round(c.dock_keep_fraction * 100)}%`], ["SA limit", `≤ ${c.sa_threshold}`], ["ADMET gate", c.enable_admet ? (c.strict_admet_gate ? "no alerts" : "≤ 1 alert") : "off"],
    ["Seed", c.library_seed], ["Tools", j.stages.filter((s) => s.tool).map((s) => s.tool).join(" · ")]];
  document.getElementById("used").innerHTML = rows.map(([a, b]) => `<div class="flex justify-between gap-space-md bg-surface-container-low p-space-xs rounded"><span class="text-outline shrink-0">${a}</span><span class="text-right break-words">${esc(b)}</span></div>`).join("");
  document.getElementById("events").innerHTML = j.events.map((e) => `<div class="flex gap-space-sm"><span class="text-outline shrink-0">${fmt.when(e.at)}</span><span class="shrink-0 font-semibold">${esc(e.stage)}</span><span class="${/FAILED/.test(e.status) ? "text-error" : ""}">${esc(e.status)}${e.detail ? " — " + esc(e.detail) : ""}</span></div>`).join("");
}

async function renderFiles() {
  const { artifacts } = await API.artifacts(jobId);
  document.getElementById("files").innerHTML = artifacts.map((a) => `<a class="px-space-sm py-space-2xs rounded bg-surface-container-low hover:bg-surface-container font-mono-sm text-mono-sm" href="/api/artifacts/${a.artifact_id}" title="${esc(a.stage)} · ${fmt.bytes(a.size_bytes)}">${esc(a.name)}</a>`).join("") || '<span class="text-outline font-mono-sm">No files yet.</span>';
}

// ---------------------------------------------------------------- drawer ---
const GROUP_ORDER = ["Absorption", "Distribution", "Metabolism", "Excretion", "Toxicity", "Drug-likeness"];
async function openCandidate(id) {
  const drawer = document.getElementById("drawer"), body = document.getElementById("drawer-body");
  drawer.hidden = false; body.innerHTML = '<div class="text-outline">Loading…</div>';
  const m = await API.candidate(id);
  const sp = m.stage_payloads, d = sp.docking || {}, h = sp.hyde || {}, sa = sp.sa || {}, ad = sp.admet || {};
  const kv = (a, b, tip) => `<div class="flex justify-between gap-space-md bg-surface-container-low p-space-xs rounded" ${tip ? `title="${esc(tip)}"` : ""}><span class="text-outline">${a}</span><span class="text-right font-semibold">${b}</span></div>`;
  let admetHTML = "";
  if (ad.values) {
    admetHTML = GROUP_ORDER.map((g) => {
      const keys = Object.keys(m.admet_endpoints).filter((k) => m.admet_endpoints[k].group === g && ad.values[k] !== undefined);
      if (!keys.length) return "";
      return `<div class="mt-space-sm"><div class="font-label-caps text-label-caps text-outline uppercase">${g}</div>` + keys.map((k) => {
        const e = m.admet_endpoints[k], flag = (ad.tox_flags || []).includes(k);
        const pct = ad.percentiles?.[k];
        return kv(esc(e.label), `${ad.values[k]}${pct !== undefined ? ` <span class="text-outline font-normal">· riskier/higher than ${Math.round(pct)}% of approved drugs</span>` : ""}${flag ? ' <span class="text-error">⚑ alert</span>' : ""}`, e.unit);
      }).join("") + "</div>";
    }).join("");
  }
  body.innerHTML = `
    <div class="flex justify-between items-start"><div><div class="font-label-caps text-label-caps text-outline uppercase">Molecule</div>
      <h2 class="font-headline-lg text-headline-lg">${esc(m.display_id)}</h2>
      <div class="font-mono-sm text-mono-sm text-outline">${esc(m.source === "zinc20" ? "ZINC20 catalogue compound" : m.source)} ${esc(m.source_id || "")} — an existing catalogue entry, not a newly designed molecule</div></div>
      <button id="drawer-close" class="material-symbols-outlined">close</button></div>
    <div class="flex flex-wrap items-center gap-space-sm">${m.rank ? `<span class="px-space-sm py-space-2xs rounded bg-primary text-on-primary font-mono-data">Rank ${m.rank}</span>` : ""}
      ${m.outcome === "active" ? chip("succeeded", "Passed every filter") : chip(m.outcome === "failed" ? "failed" : "retrying", m.outcome === "failed" ? "Could not be processed" : "Removed by a rule")}
      <a class="ml-auto underline text-primary font-body-sm" href="/api/candidates/${m.molecule_id}/sdf">Download SDF</a></div>
    ${m.outcome !== "active" ? `<div class="p-space-sm rounded bg-warning-container text-warning font-body-sm">Left at <strong>${esc(m.exit_stage)}</strong>: ${esc(m.exit_reason)}</div>` : ""}
    <div class="grid grid-cols-1 md:grid-cols-2 gap-space-md">
      <div class="bg-white rounded p-space-sm structure-cell h-56">${m.svg_large}</div>
      <div id="viewer-host" class="relative bg-surface-container-low rounded h-56 flex items-center justify-center text-outline font-body-sm">${m.has_pose ? '<button id="load3d" class="px-space-md h-8 bg-primary text-on-primary rounded">View 3D pose in the pocket</button>' : "No 3D pose"}</div></div>
    <div class="font-mono-sm text-mono-sm break-all text-outline">${esc(m.smiles)}</div>

    <section><h3 class="font-headline-sm text-headline-sm">Docking ${real(false)}</h3>
      <p class="font-body-sm text-body-sm text-on-surface-variant">How well the molecule is predicted to fit the pocket. More negative is a better predicted fit. This is a computed estimate, not a measurement.</p>
      <div class="mt-space-xs space-y-space-2xs font-mono-data text-mono-data">
        ${d.score !== undefined ? kv(esc(d.engine === "vina" ? "AutoDock Vina score" : "FlexX score"), `${d.score} ${esc(d.score_unit)}`, "Different programs use different scales") + kv("Poses found", d.n_poses) : '<div class="text-outline">Not available.</div>'}</div></section>
    ${h.cumulative_kj_mol !== undefined ? `<section><h3 class="font-headline-sm text-headline-sm">HYDE re-score ${real(false)}</h3>
      <p class="font-body-sm text-body-sm text-on-surface-variant">A physics-based estimate that includes the cost of stripping water from the molecule and the pocket. The affinity range is HYDE's own uncertainty band: the real value is predicted to lie between the two ends.</p>
      <div class="mt-space-xs space-y-space-2xs font-mono-data text-mono-data">
        ${kv("Total HYDE score", energy(h.cumulative_kj_mol), "Sum of per-atom contributions; more negative is better")}
        ${h.components_kj_mol ? kv("↳ Protein desolvation", energy(h.components_kj_mol.rec_desolv), "Energy cost of removing water from the pocket (usually unfavourable, positive)") +
          kv("↳ Protein interactions", energy(h.components_kj_mol.rec_ia), "Hydrogen bonds and contacts made by the protein side") +
          kv("↳ Ligand desolvation", energy(h.components_kj_mol.lig_desolv), "Energy cost of removing water from the molecule") +
          kv("↳ Ligand interactions", energy(h.components_kj_mol.lig_ia), "Hydrogen bonds and contacts made by the molecule side") : ""}
        ${kv("Estimated affinity range", `${sci(h.affinity_lower_nm)} – ${sci(h.affinity_upper_nm)}`)}
        ${kv("Ligand efficiency", h.ligand_efficiency ?? "—", "Above 0.513 very high; below 0.205 poor (vendor guidance)")}
        ${kv("Clashes with protein", h.inter_clash || "—")}${kv("Clashes within molecule", h.intra_clash || "—")}
        ${kv("Hydrogen bonds", h.h_bonds ?? "—")}${kv("Unfavourable torsions", h.unfavorable_torsions ?? "—", "Strained rotatable bonds in the pose")}</div></section>` : ""}
    <section><h3 class="font-headline-sm text-headline-sm">Synthetic accessibility ${real(false)}</h3>
      <p class="font-body-sm text-body-sm text-on-surface-variant">1 = easy to make, 10 = very hard (Ertl &amp; Schuffenhauer score from RDKit).</p>
      <div class="mt-space-xs font-mono-data text-mono-data">${kv("SA score", sa.sa_score ?? "—")}${kv("Molecular weight", sa.molecular_weight ? sa.molecular_weight + " g/mol" : "—")}</div></section>
    ${ad.values ? `<section><h3 class="font-headline-sm text-headline-sm">ADMET ${real(false)}</h3>
      <p class="font-body-sm text-body-sm text-on-surface-variant">Predictions from the ${esc(ad.model)}. Values marked as probabilities are model confidence that the property is present, trained on public datasets. They carry real uncertainty and are not lab results.</p>
      <div class="mt-space-xs font-mono-data text-mono-data">${admetHTML}</div></section>` : ""}
    <section><h3 class="font-headline-sm text-headline-sm">Placeholders</h3>
      <p class="font-body-sm text-body-sm text-on-surface-variant">Binding affinity (Kd) and unbinding kinetics (koff, residence time) come from the Koffee tools, which are not connected yet. The numbers below are placeholders.</p>
      <div class="mt-space-xs font-mono-data text-mono-data opacity-70">${kv("Kd", sp.affinity ? sp.affinity.kd_nm + " nM*" : "—")}${kv("Residence time", sp.kinetics ? sp.kinetics.residence_time_min + " min*" : "—")}</div></section>
    <section><h3 class="font-headline-sm text-headline-sm">How this molecule got here</h3>
      <ol class="mt-space-xs space-y-space-2xs font-body-sm">${m.provenance.map((p) => `<li class="flex items-center gap-space-xs">${p.error ? '<span class="material-symbols-outlined text-error text-[16px]">cancel</span>' : p.has_result ? '<span class="material-symbols-outlined text-secondary text-[16px]">check_circle</span>' : '<span class="material-symbols-outlined text-outline text-[16px]">remove_circle_outline</span>'}
        <span>${esc(p.label)}</span> ${real(p.mocked)} <span class="text-outline font-mono-sm">${esc(p.tool || "")}</span>${p.error ? `<span class="text-error"> — ${esc(p.error)}</span>` : ""}</li>`).join("")}</ol></section>`;
  document.getElementById("drawer-close").onclick = closeDrawer;
  const b = document.getElementById("load3d");
  if (b) b.onclick = () => load3d(id);
}
function closeDrawer() { document.getElementById("drawer").hidden = true; }
async function load3d(id) {
  const host = document.getElementById("viewer-host");
  host.innerHTML = "Loading 3D…";
  try {
    if (typeof $3Dmol === "undefined") throw new Error("The 3D viewer library could not be loaded (the viewer ships with the app).");
    const c = await API.complex(id);
    host.innerHTML = ""; host.classList.remove("items-center", "justify-center");
    const v = $3Dmol.createViewer(host, { backgroundColor: "white" });
    v.addModel(c.protein_pdb, "pdb"); v.setStyle({}, { cartoon: { color: "#9aa7c7", opacity: 0.75 } });
    v.addModel(c.pose_sdf, "sdf"); v.setStyle({ model: -1 }, { stick: { colorscheme: "greenCarbon", radius: 0.2 } });
    v.zoomTo({ model: -1 }); v.render();
    host.insertAdjacentHTML("beforeend", `<div class="absolute left-1 bottom-1 px-space-xs rounded bg-white/80 font-mono-sm text-mono-sm text-outline">${esc(c.pose_source)} pose</div>`);
  } catch (e) { host.textContent = e.message; }
}

// ---------------------------------------------------------------- events ---
async function openLog(batchId, idx) {
  document.getElementById("logbox").hidden = false;
  document.getElementById("log-title").textContent = `Batch #${idx} — tool log (last 32 KB)`;
  document.getElementById("log-text").textContent = "Loading…";
  document.getElementById("log-text").textContent = await API.batchLog(jobId, batchId);
}

document.addEventListener("change", (ev) => {
  if (ev.target.id === "unit-toggle") {
    UNIT.kcal = ev.target.checked;
    try { localStorage.setItem("mdp-energy-unit", UNIT.kcal ? "kcal" : "kj"); } catch (_) {}
    refresh();
  }
});

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-stage],[data-act],[data-tab],[data-mol],[data-log],[data-batch]");
  if (ev.target.id === "drawer-bg") return closeDrawer();
  if (ev.target.id === "log-bg" || ev.target.id === "log-close") return (document.getElementById("logbox").hidden = true);
  if (ev.target.id === "b-retry") {
    try {
      const body = S.selected.size ? { batch_ids: [...S.selected], failed_molecules: false } : { failed_molecules: true };
      const r = await API.retry(jobId, body); S.selected.clear();
    } catch (e) { alert(e.message); }
    return refresh();
  }
  if (!t) return;
  if (t.dataset.stage) { S.stage = S.stage === t.dataset.stage ? null : t.dataset.stage; await refresh(); }
  else if (t.dataset.act) { try { await API[t.dataset.act](jobId); } catch (e) { alert(e.message); } await refresh(); }
  else if (t.dataset.tab) { S.tab = t.dataset.tab; await refresh(); }
  else if (t.dataset.mol) openCandidate(t.dataset.mol);
  else if (t.dataset.log) openLog(t.dataset.log, t.dataset.idx);
  else if (t.dataset.batch) { t.checked ? S.selected.add(t.dataset.batch) : S.selected.delete(t.dataset.batch); renderBatches(); }
});

async function refresh() {
  try {
    const j = await API.job(jobId);
    S.job = j;
    if (!S.stage) { const f = j.stages.find((s) => s.status === "failed") || j.stages.find((s) => s.status === "running"); if (f && f.batched && !S.autoPicked) { S.stage = f.key; S.autoPicked = true; } }
    document.getElementById("content").hidden = false;
    document.getElementById("unit-toggle").checked = UNIT.kcal;
    renderTitle(j); renderStages(j); renderSide(j);
    await Promise.all([renderBatches(), renderResults(), renderFiles()]);
    Shell.render({ active: "run", breadcrumb: [{ label: "Discoveries", href: "discoveries.html" }, { label: j.label || "Campaign" }], context: [{ label: "STATUS", value: NICE[j.status] }] });
    Shell.loadEngines();
  } catch (e) {
    showError("load-error", e.message); document.getElementById("load-error").hidden = false;
  }
  clearTimeout(S.timer);
  const live = S.job && ["QUEUED", "RUNNING"].includes(S.job.status);
  S.timer = setTimeout(refresh, live ? 2000 : 10000);
}

if (!jobId) showError("load-error", "No campaign selected.");
else refresh();
