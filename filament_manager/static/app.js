"use strict";
// Filament Manager page. Talks only to the local service (server.py).

const $ = (id) => document.getElementById(id);
const form = $("form");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hex = (c) => (c ? "#" + String(c).replace("#", "").slice(0, 6) : "transparent");

// form field -> Spoolman filament field
const FIL_MAP = {
  name: "name", type: "material", color_hex: "color_hex", multi_color_hexes: "multi_color_hexes",
  extruder_temp: "settings_extruder_temp", bed_temp: "settings_bed_temp", density: "density",
  diameter: "diameter", weight: "weight", spool_weight: "spool_weight", price: "price",
  upc: "article_number", subtype: "variant",
};
const SPOOL_FIELDS = ["remaining_weight", "location", "lot_nr", "comment"];
const NUMERIC = new Set(["extruder_temp", "bed_temp", "density", "diameter", "weight", "spool_weight", "price", "remaining_weight"]);
const IDENTITY = ["brand", "type", "name", "color_hex", "subtype"];

const state = {
  catalog: { vendors: [], filaments: [], spools: [], materials: [], locations: [] },
  tag: null,          // last tag read (server JSON incl. lookup)
  lastUid: null,
  mode: "new",        // "new" | "existing"
  spool: null,        // selected existing spool
  base: null,         // filament a new spool starts from
  original: {},       // form values when loaded (stale-prefill guard)
  polling: false,
  pollToken: 0,
  fake: false,
  writePref: true,    // the user's choice for "Write OpenSpool data", kept across UID-only tags
};

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) { const e = new Error(data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

function confirmDialog(title, body, yes = "OK") {
  return new Promise((resolve) => {
    const d = $("dlg-confirm");
    $("confirm-title").textContent = title; $("confirm-body").textContent = body; $("confirm-yes").textContent = yes;
    d.onclose = () => resolve(d.returnValue === "yes");
    d.showModal();
  });
}

function vendorName(f) { return (f && f.vendor && f.vendor.name) || ""; }
function decodeExtra(v) { if (v == null) return ""; try { const x = JSON.parse(v); return typeof x === "string" ? x : String(x); } catch { return String(v); } }
function filLabel(f) {
  // Skip brand/material/variant when the filament's name already says them ("Snapmaker PETG HF Dark Green").
  const name = String(f.name || ""), low = name.toLowerCase();
  const parts = [vendorName(f), f.material, decodeExtra((f.extra || {}).variant)].filter((x) => x && !low.includes(String(x).toLowerCase()));
  return [...parts, name].filter(Boolean).join(" ");
}
function spoolLabel(s) { return `#${s.id} ${filLabel(s.filament || {})}`; }

// ---------------------------------------------------------------- status & catalog

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.fake = s.fake; $("fake-panel").hidden = !s.fake;
    const fl = $("chip-flipper"), sm = $("chip-spoolman");
    fl.textContent = s.flipper.connected ? `Flipper · ${s.flipper.port}` : "Flipper offline";
    fl.className = "chip " + (s.flipper.connected ? "ok" : "bad"); fl.title = s.flipper.error || "";
    sm.textContent = s.spoolman.connected ? `Spoolman ${s.spoolman.version || ""}` : "Spoolman offline";
    sm.className = "chip " + (s.spoolman.connected ? "ok" : "bad"); sm.title = s.spoolman.error || s.settings.spoolman_url;
    $("set-url").value = s.settings.spoolman_url; $("set-store").value = s.settings.tag_store;
    return s;
  } catch (e) { toast("Local service not responding: " + e.message); return null; }
}

async function loadCatalog() {
  try {
    state.catalog = await api("/api/catalog");
  } catch (e) { toast(e.message); return; }
  const fill = (id, items) => { $(id).innerHTML = items.map((x) => `<option value="${esc(x)}">`).join(""); };
  fill("dl-vendors", state.catalog.vendors.map((v) => v.name));
  fill("dl-materials", [...new Set([...state.catalog.materials, "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PLA-CF", "PETG-CF"])]);
  fill("dl-locations", state.catalog.locations);
  renderSpoolList(); renderFilamentList();
}

// ---------------------------------------------------------------- scanning

function setScan(kind, title, sub = "") {
  $("scan-state").className = "scan-state " + kind;
  $("scan-title").textContent = title; $("scan-sub").textContent = sub;
}

async function pollLoop(skipUid, onTag = handleTag) {
  const token = ++state.pollToken;
  state.polling = true;
  setScan("waiting", skipUid ? "Waiting for a different tag…" : "Waiting for a tag…", "Hold it flat against the back of the Flipper.");
  while (state.polling && token === state.pollToken) {
    try {
      const r = await api("/api/tag/wait", { timeout: 15, skip_uid: skipUid || null });
      if (token !== state.pollToken) return;
      if (r.tag) { state.polling = false; await onTag(r.tag); return; }
    } catch (e) {
      if (token !== state.pollToken) return;
      setScan("error", "Can't reach the Flipper", e.message);
      await sleep(4000);
      if (state.polling) setScan("waiting", "Waiting for a tag…", "Retrying.");
    }
  }
}

async function stopPolling() {
  state.pollToken++;
  if (state.polling) { state.polling = false; try { await api("/api/tag/cancel", {}); } catch { /* ignore */ } }
}

async function readNow() {
  await stopPolling();
  setScan("waiting", "Reading…");
  try { await handleTag(await api("/api/tag/read", {})); }
  catch (e) { setScan("error", e.data && e.data.no_tag ? "No tag on the Flipper" : "Read failed", e.data && e.data.no_tag ? "" : e.message); }
}

function tagToFields(f) {
  if (!f) return {};
  const avg = (a, b) => (a && b ? Math.round((+a + +b) / 2) : +(a || b) || undefined);
  return {
    brand: f.brand, type: f.type, subtype: f.subtype, color_hex: String(f.color_hex || "").replace("#", "").toUpperCase() || undefined,
    extruder_temp: avg(f.min_temp, f.max_temp), bed_temp: avg(f.bed_min_temp, f.bed_max_temp),
    weight: f.weight, diameter: f.diameter,
    multi_color_hexes: (f.additional_color_hexes || []).length ? [f.color_hex, ...f.additional_color_hexes].map((c) => String(c).replace("#", "")).join(",") : undefined,
  };
}

async function handleTag(t) {
  state.tag = t; state.lastUid = t.uid;
  const fmt = { openspool: "OpenSpool", ace: "Anycubic ACE", snapmaker: "Snapmaker", ndef: "Other NDEF", blank: "Blank", unformatted: "Unformatted", uid_only: "UID only" }[t.format] || t.format;
  setScan("done", "Tag read", `${t.device || t.family} · ${fmt}`);
  $("tag-info").hidden = false;
  $("tag-uid").textContent = t.uid;
  $("tag-family").textContent = t.family === "ntag" ? (t.device || "NTAG") : t.family === "mifare_classic" ? "Mifare Classic" : t.family;
  $("tag-format").textContent = fmt;
  const f = t.filament;
  $("tag-filament").innerHTML = f
    ? `<span class="sw lg" style="background:${hex(f.color_hex)}"></span><span><b>${esc([f.brand, f.type, f.subtype].filter(Boolean).join(" "))}</b><br><span class="muted small">${esc([f.min_temp && `${f.min_temp}–${f.max_temp} °C`, f.weight && `${f.weight} g`, f.sku && `SKU ${f.sku}`, f.mfg_date && `made ${f.mfg_date}`].filter(Boolean).join(" · "))}</span></span>`
    : "";
  $("tag-notes").textContent = (t.notes || []).join(" ");

  $("opt-pair").checked = t.format === "snapmaker";  // Snapmaker spools have a tag on each side
  const writable = t.family === "ntag";
  $("opt-write").disabled = !writable;
  $("opt-write").checked = writable && state.writePref;
  $("opt-write").parentElement.title = writable ? "" : "Only NTAG stickers can be written. This tag is linked by UID only.";
  updateGoLabel();

  const lk = t.lookup || {};
  const linked = $("linked"); linked.innerHTML = ""; linked.hidden = true;
  $("suggest").hidden = true;
  if (lk.error) { linked.hidden = false; linked.innerHTML = `<div class="result bad">${esc(lk.error)}</div>`; return; }

  if ((lk.linked_spools || []).length) {
    await loadCatalog();  // before showing anything, so the form can't change under the user
    loadSpool(lk.linked_spools[0].id);
    renderLinked(lk.linked_spools);
    return;
  }
  renderSuggestions(t, lk, f);
}

function renderLinked(spools) {
  const linked = $("linked");
  linked.hidden = !spools.length;
  $("suggest").hidden = true;
  linked.innerHTML = spools.map((s) => `
      <div class="linked"><span class="sw lg" style="background:${hex((s.filament || {}).color_hex)}"></span>
        <div class="grow"><b>Linked to ${esc(spoolLabel(s))}</b><div class="muted small">${s.remaining_weight != null ? Math.round(s.remaining_weight) + " g left" : ""}${s.location ? " · " + esc(s.location) : ""}${s.archived ? " · archived" : ""}</div></div>
        <button class="small" data-open="${s.id}">Edit</button></div>`).join("");
  linked.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => loadSpool(+b.dataset.open)));
}

function renderSuggestions(t, lk, f) {
  // Not linked: prefill a new spool from the tag and show closest matches.
  // Start fresh if the form was showing another spool; keep a half-filled new form.
  setMode("new", state.mode === "existing");
  if (f) applyFields(tagToFields(f), true);
  const rows = [];
  for (const s of lk.partner_spools || []) rows.push({ kind: "partner", label: `Other tag of ${spoolLabel(s)}`, sub: `That spool has 1 tag so far${s.location ? " · " + s.location : ""}. Link this one too?`, spool: s, src: "pair" });
  if (lk.legacy_spool) rows.push({ kind: "spool", label: `Tag says spool ${spoolLabel(lk.legacy_spool)}`, sub: "Written by an older app. Link it to that spool?", spool: lk.legacy_spool, src: "tag" });
  for (const s of lk.suggestions || []) rows.push({ kind: "filament", label: filLabel(s.filament), sub: `${s.filament.color_hex ? "#" + s.filament.color_hex : ""} · your filament #${s.filament.id}`, color: s.filament.color_hex, filament: s.filament, fields: s.fields, score: s.score, src: "yours" });
  for (const x of lk.external || []) rows.push({ kind: "external", label: `${x.external.manufacturer} ${x.external.name}`, sub: `${x.external.material} · SpoolmanDB`, color: x.fields.color_hex, fields: x.fields, score: x.score, src: "db" });
  if (rows.length) {
    $("suggest").hidden = false;
    $("suggest-list").innerHTML = rows.map((r, i) => `
      <button class="item" data-i="${i}"><span class="sw" style="background:${hex(r.color || (r.spool && r.spool.filament.color_hex))}"></span>
        <span class="grow"><span class="t">${esc(r.label)}</span><span class="s">${esc(r.sub)}</span></span>
        <span class="src">${r.src}</span></button>`).join("");
    $("suggest-list").querySelectorAll(".item").forEach((b) => (b.onclick = () => {
      const r = rows[+b.dataset.i];
      if (r.kind === "spool") return loadSpool(r.spool.id);
      if (r.kind === "partner") return linkSecondTag(r.spool.id, t);
      setMode("new", false);
      if (r.kind === "filament") chooseBase(r.filament);
      else { chooseBase(null); applyFields(r.fields, true); }
      toast("Form filled from " + r.label);
    }));
  }
}

// ---------------------------------------------------------------- form

function getForm() {
  const out = {};
  for (const el of form.elements) if (el.name && el.type !== "radio") out[el.name] = el.value.trim();
  out.wmode = form.elements.wmode.value;
  out.color_hex = out.color_hex.replace("#", "").toUpperCase();
  return out;
}

function applyFields(fields, mark) {
  for (const [k, v] of Object.entries(fields || {})) {
    const el = form.elements[k];
    if (!el || v == null || v === "") continue;
    el.value = k === "color_hex" ? String(v).replace("#", "").toUpperCase() : v;
    if (mark) el.classList.add("prefilled");
  }
  syncColor();
}

function clearForm(keepMode) {
  form.reset();
  for (const el of form.elements) el.classList && el.classList.remove("prefilled");
  state.original = {}; state.base = null; state.spool = null;
  $("filament-chosen").hidden = true; $("spool-tags").hidden = true; $("result").hidden = true;
  $("upc-result").textContent = "";
  syncColor(); updateWeigh();
  if (!keepMode) setMode("new", false);
}

function filamentToFields(f) {
  const out = { brand: vendorName(f) };
  for (const [k, sk] of Object.entries(FIL_MAP)) {
    const v = sk === "variant" ? decodeExtra((f.extra || {}).variant) : f[sk];
    if (v != null && v !== "") out[k] = v;
  }
  return out;
}

function spoolToFields(s) {
  const out = filamentToFields(s.filament || {});
  if (s.remaining_weight != null) out.remaining_weight = Math.round(s.remaining_weight);
  if (s.spool_weight != null) out.spool_weight = s.spool_weight;
  for (const k of ["location", "lot_nr", "comment"]) if (s[k]) out[k] = s[k];
  return out;
}

function snapshot() { state.original = getForm(); }

function syncColor() {
  const v = form.elements.color_hex.value.replace("#", "");
  if (/^[0-9a-fA-F]{6}$/.test(v)) $("color-pick").value = "#" + v;
}

function setMode(mode, clear = true) {
  state.mode = mode;
  $("tab-new").classList.toggle("active", mode === "new");
  $("tab-existing").classList.toggle("active", mode === "existing");
  $("pick-new").hidden = mode !== "new";
  $("pick-existing").hidden = mode !== "existing";
  if (clear) clearForm(true);
  $("form-title").textContent = mode === "new" ? "New spool" : state.spool ? `Spool #${state.spool.id}` : "Existing spool";
  updateGoLabel();
}

function updateGoLabel() {
  const link = $("opt-link").checked && state.tag, write = $("opt-write").checked && !$("opt-write").disabled && state.tag;
  $("btn-go").textContent = "Save" + (link ? (write ? ", link & write" : " & link") : "");
  $("opt-link").disabled = !state.tag; $("opt-link").parentElement.title = state.tag ? "" : "Scan a tag first";
  $("opt-pair").disabled = !link;
}

function loadSpool(id) {
  const s = state.catalog.spools.find((x) => x.id === id);
  if (!s) return toast(`Spool #${id} not found (archived spools are included).`);
  setMode("existing", true);
  state.spool = s;
  applyFields(spoolToFields(s), false);
  snapshot();
  updateWeigh();
  $("form-title").textContent = `Spool #${s.id}`;
  renderSpoolList(); renderSpoolTags();
}

function renderSpoolTags() {
  const box = $("spool-tags");
  const s = state.spool;
  if (!s) { box.hidden = true; return; }
  box.hidden = false;
  const uids = s.uids || [];
  box.innerHTML = "Tags: " + (uids.length ? uids.map((u) => `<span class="tagchip"><code>${esc(u)}</code><button type="button" title="Unlink" data-unlink="${esc(u)}">×</button></span>`).join("") : "<span>none yet</span>");
  box.querySelectorAll("[data-unlink]").forEach((b) => (b.onclick = async () => {
    if (!(await confirmDialog("Unlink tag?", `Remove ${b.dataset.unlink} from spool #${s.id}? The tag itself isn't changed.`, "Unlink"))) return;
    try { const r = await api("/api/unlink", { spool_id: s.id, uid: b.dataset.unlink }); state.spool.uids = r.uids; renderSpoolTags(); toast("Tag unlinked"); await loadCatalog(); }
    catch (e) { toast(e.message); }
  }));
}

function chooseBase(f) {
  state.base = f;
  const box = $("filament-chosen");
  if (!f) { box.hidden = true; return; }
  for (const el of form.elements) if (el.name && el.type !== "radio" && !SPOOL_FIELDS.includes(el.name) && el.name !== "gross_weight") el.value = "";
  applyFields(filamentToFields(f), false);
  state.original = getForm();
  box.hidden = false;
  box.innerHTML = `<span class="sw lg" style="background:${hex(f.color_hex)}"></span><span class="grow"><b>${esc(filLabel(f))}</b><br><span class="muted small">Your filament #${f.id}. A new spool of it will be created.</span></span><button type="button" class="small ghost" id="unbase">×</button>`;
  $("unbase").onclick = () => { state.base = null; box.hidden = true; };
}

function matches(text, q) { return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.toLowerCase().includes(w)); }

function renderSpoolList() {
  const q = $("spool-search").value;
  const list = state.catalog.spools.filter((s) => !q || matches(`#${s.id} ${spoolLabel(s)} ${(s.filament || {}).color_hex || ""} ${s.location || ""} ${(s.uids || []).join(" ")}`, q))
    .sort((a, b) => (a.archived - b.archived) || b.id - a.id).slice(0, 200);
  $("spool-list").innerHTML = list.map((s) => `
    <button type="button" class="item ${state.spool && state.spool.id === s.id ? "sel" : ""}" data-id="${s.id}">
      <span class="sw" style="background:${hex((s.filament || {}).color_hex)}"></span>
      <span class="grow"><span class="t">${esc(spoolLabel(s))}</span>
      <span class="s">${s.remaining_weight != null ? Math.round(s.remaining_weight) + " g left" : ""}${s.location ? " · " + esc(s.location) : ""}${(s.uids || []).length ? " · " + s.uids.length + " tag(s)" : " · no tag"}${s.archived ? " · archived" : ""}</span></span></button>`).join("")
    || `<div class="muted small" style="padding:8px">No spools${q ? " match" : " yet"}.</div>`;
  $("spool-list").querySelectorAll(".item").forEach((b) => (b.onclick = () => loadSpool(+b.dataset.id)));
}

function renderFilamentList() {
  const q = $("filament-search").value;
  const box = $("filament-list");
  if (!q) { box.hidden = true; return; }
  const list = state.catalog.filaments.filter((f) => matches(`#${f.id} ${filLabel(f)} ${f.color_hex || ""} ${f.article_number || ""}`, q)).slice(0, 50);
  box.hidden = false;
  box.innerHTML = list.map((f) => `
    <button type="button" class="item" data-id="${f.id}"><span class="sw" style="background:${hex(f.color_hex)}"></span>
      <span class="grow"><span class="t">${esc(filLabel(f))}</span><span class="s">#${f.id}${f.color_hex ? " · #" + f.color_hex : ""}${f.article_number ? " · UPC " + esc(f.article_number) : ""}</span></span></button>`).join("")
    || `<div class="muted small" style="padding:8px">No filaments match.</div>`;
  box.querySelectorAll(".item").forEach((b) => (b.onclick = () => {
    chooseBase(state.catalog.filaments.find((f) => f.id === +b.dataset.id));
    $("filament-search").value = ""; box.hidden = true;
  }));
}

// ---------------------------------------------------------------- UPC

async function lookupUpc() {
  const code = $("upc").value.trim();
  if (!code) return;
  $("upc-result").textContent = "Looking up…";
  try {
    const r = await api("/api/lookup/upc", { upc: code });
    setMode("new", false);
    if (r.filaments.length) {
      chooseBase(r.filaments[0].filament);
      $("upc-result").textContent = `Found in your Spoolman: ${filLabel(r.filaments[0].filament)}.`;
    } else {
      chooseBase(null);
      applyFields(r.fields, true);
      const parts = [];
      if (r.product) parts.push(`Product: ${r.product.title}`);
      if (r.external.length) parts.push(`SpoolmanDB match: ${r.external[0].external.manufacturer} ${r.external[0].external.name}`);
      parts.push(...r.notes);
      if (!r.product && !r.external.length) parts.push("Nothing found. Fill in the form; the UPC is saved so the next spool fills in automatically.");
      $("upc-result").innerHTML = parts.map(esc).join("<br>");
    }
    form.elements.upc.value = r.upc;
  } catch (e) { $("upc-result").textContent = e.message; }
}

// ---------------------------------------------------------------- save / link / write

function num(k, v) { if (v === "" || v == null) return null; return NUMERIC.has(k) ? Number(v) : v; }

function emptySpoolWeight(v) {
  if (v.spool_weight !== "") return { g: Number(v.spool_weight), from: "" };
  const vendor = state.catalog.vendors.find((x) => x.name.toLowerCase() === (v.brand || "").toLowerCase());
  if (vendor && vendor.empty_spool_weight) return { g: vendor.empty_spool_weight, from: `${vendor.name}'s usual` };
  return null;
}

function updateWeigh() {
  const v = getForm();
  const scale = v.wmode === "scale";
  $("w-remaining").hidden = scale; $("w-scale").hidden = !scale;
  const calc = $("w-calc");
  calc.hidden = !scale; calc.className = "muted small";
  if (!scale) return;
  const empty = emptySpoolWeight(v);
  if (!v.gross_weight) { calc.textContent = empty ? `The empty spool (${empty.g} g${empty.from ? ", " + empty.from : ""}) is subtracted.` : "Enter the empty spool weight above, or weigh an empty spool of this brand."; return; }
  if (!empty) { calc.className = "small warn"; calc.textContent = "Need the empty spool weight (filament section) to work out what's left."; return; }
  const left = Math.max(0, Math.round(Number(v.gross_weight) - empty.g));
  const net = Number(v.weight) || null;
  calc.innerHTML = `${esc(v.gross_weight)} g − ${empty.g} g empty spool${empty.from ? ` (${esc(empty.from)})` : ""} = <b>${left} g of filament</b>` +
    (net && left > net * 1.05 ? ` <span class="warn">· more than the ${net} g net weight; check the empty spool weight</span>` : "");
}

function buildSave() {
  const v = getForm();
  if (v.wmode === "scale" && v.gross_weight !== "") {
    const empty = emptySpoolWeight(v);
    if (!empty) throw new Error("Enter the empty spool weight so the scale reading can be converted.");
    v.remaining_weight = String(Math.max(0, Math.round(Number(v.gross_weight) - empty.g)));
    if (v.spool_weight === "") v.spool_weight = String(empty.g);
  }
  if (!v.type) throw new Error("Material is required.");
  if (!/^[0-9A-F]{6}$/.test(v.color_hex)) throw new Error("Color must be 6 hex digits, like FF6A13.");
  const o = state.original;
  const changed = (k) => (o[k] ?? "") !== (v[k] ?? "");
  const filAll = {};
  for (const [k, sk] of Object.entries(FIL_MAP)) { const x = num(k, v[k]); if (x != null) filAll[sk] = x; }
  const filChanged = {};
  // Empty spool weight is stored per spool when editing, so it doesn't change the shared filament.
  for (const [k, sk] of Object.entries(FIL_MAP)) if (changed(k) && k !== "spool_weight") filChanged[sk] = num(k, v[k]) ?? (sk === "variant" ? "" : null);

  const payload = {};
  const existingFil = state.mode === "existing" ? state.spool.filament : state.base;
  const identityChanged = IDENTITY.some(changed);
  if (existingFil && !(state.mode === "new" && identityChanged)) {
    payload.filament_id = existingFil.id;
    if (Object.keys(filChanged).length) payload.filament = filChanged;
    if (changed("brand")) payload.vendor_name = v.brand;
  } else {
    payload.filament = filAll;
    payload.vendor_name = v.brand || "Generic";
  }
  const spool = {};
  if (state.mode === "existing") {
    payload.spool_id = state.spool.id;
    for (const k of SPOOL_FIELDS) if (changed(k)) spool[k] = num(k, v[k]);
    if (!changed("remaining_weight")) delete spool.remaining_weight; // printer may have updated it meanwhile
    if (changed("spool_weight")) spool.spool_weight = num("spool_weight", v.spool_weight);
  } else {
    for (const k of SPOOL_FIELDS) { const x = num(k, v[k]); if (x != null) spool[k] = x; }
    if (v.weight) spool.initial_weight = Number(v.weight);
    if (v.spool_weight) spool.spool_weight = Number(v.spool_weight);
  }
  payload.spool = spool;
  return payload;
}

function showResult(kind, html) { const r = $("result"); r.hidden = false; r.className = "result " + kind; r.innerHTML = html; }

async function linkWithConfirm(spoolId, uid, format) {
  // `format` is what the tag will hold after this save: "openspool" when we're about to write it.
  format = format || (state.tag && state.tag.format);
  try { return await api("/api/link", { spool_id: spoolId, uid, format }); }
  catch (e) {
    if (e.status !== 409 || !e.data.conflict) throw e;
    const names = e.data.conflict.map((c) => c.name).join(", ");
    if (!(await confirmDialog("Tag already in use", `${uid} is linked to ${names}. Move it to spool #${spoolId}? It will be removed from the other spool.`, "Move it"))) return null;
    return await api("/api/link", { spool_id: spoolId, uid, move: true, format });
  }
}

async function go(doLink, doWrite) {
  let payload;
  try { payload = buildSave(); } catch (e) { return showResult("bad", esc(e.message)); }
  const tag = state.tag;
  doLink = doLink && !!tag; doWrite = doWrite && !!tag && tag.family === "ntag";
  const doPair = doLink && $("opt-pair").checked;  // read now: loading the saved spool resets the form
  const steps = [];
  $("btn-go").disabled = $("btn-save-only").disabled = true;
  showResult("busy", "Saving to Spoolman…");
  try {
    const spool = await api("/api/save", payload);
    steps.push(`${payload.spool_id ? "Updated" : "Created"} spool #${spool.id} (${esc(filLabel(spool.filament))})`);
    if (doLink) {
      showResult("busy", steps.join("<br>") + "<br>Linking tag…");
      const r = await linkWithConfirm(spool.id, tag.uid, doWrite ? "openspool" : tag.format);
      if (r) steps.push(`Linked tag ${esc(tag.uid)}${r.moved_from.length ? ` (moved from #${r.moved_from.join(", #")})` : ""}`);
      else { steps.push("Tag not linked (cancelled)"); doWrite = false; }
    }
    if (doWrite) {
      await stopPolling();
      showResult("busy", steps.join("<br>") + "<br>Writing tag… hold it still.");
      const w = await api("/api/tag/write", { spool_id: spool.id, expect_uid: tag.uid });
      steps.push(`Wrote and verified OpenSpool data (${w.bytes_written} bytes)`);
    }
    await loadCatalog();
    loadSpool(spool.id);
    if (doLink) {
      renderLinked(state.catalog.spools.filter((s) => (s.uids || []).includes(tag.uid)));
      if (doWrite) $("tag-format").textContent = "OpenSpool";
    }
    showResult("ok", `<b>Done.</b><ul>${steps.map((s) => `<li>${s}</li>`).join("")}</ul>` +
      (doLink ? `<div class="row"><button type="button" class="primary" id="btn-pair">Add a second tag to this spool</button><button type="button" class="ghost" id="btn-next">Next spool</button></div>` : ""));
    if (doLink) {
      $("btn-pair").onclick = () => pairAnother(spool.id);
      $("btn-next").onclick = async () => { await stopPolling(); clearForm(); if ($("autoscan").checked) pollLoop(tag.uid); };
      // "Spool has a second tag": go straight to waiting for the other one.
      const saved = state.catalog.spools.find((s) => s.id === spool.id) || {};
      if (doPair && !(saved.uids || []).some((u) => u !== tag.uid))
        pairAnother(spool.id, `<ul>${steps.map((s) => `<li>${s}</li>`).join("")}</ul><b>Now the second tag:</b> ` +
          (tag.format === "snapmaker" ? "flip the spool and hold the other side to the Flipper." : "hold it to the Flipper."));
    }
  } catch (e) {
    showResult("bad", (steps.length ? `<ul>${steps.map((s) => `<li>${s}</li>`).join("")}</ul>` : "") + `<b>Failed:</b> ${esc(e.message)}`);
  } finally {
    $("btn-go").disabled = $("btn-save-only").disabled = false;
  }
}

async function linkSecondTag(spoolId, t) {
  try {
    const write = t.family === "ntag" && $("opt-write").checked;
    const r = await linkWithConfirm(spoolId, t.uid, write ? "openspool" : t.format);
    if (!r) return showResult("bad", "Second tag not linked.");
    $("tag-uid").textContent = t.uid;
    let msg = `Linked tag ${esc(t.uid)} to spool #${spoolId} as well.`;
    if (write) {
      await api("/api/tag/write", { spool_id: spoolId, expect_uid: t.uid });
      msg += " Wrote OpenSpool data.";
    }
    await loadCatalog(); loadSpool(spoolId);
    renderLinked(state.catalog.spools.filter((s) => s.id === spoolId));
    showResult("ok", `<b>Done.</b> ${msg}`);
    if ($("autoscan").checked) pollLoop(t.uid);
  } catch (e) { showResult("bad", `<b>Failed:</b> ${esc(e.message)}`); }
}

async function pairAnother(spoolId, prompt) {
  showResult("busy", (prompt || "Hold the second tag to the Flipper…") +
    `<div class="row"><button type="button" class="small ghost" id="btn-skip-pair">Skip</button></div>`);
  $("result").scrollIntoView({ block: "nearest", behavior: "smooth" });
  $("btn-skip-pair").onclick = async () => { await stopPolling(); clearForm(); showResult("ok", "Skipped the second tag."); if ($("autoscan").checked) pollLoop(state.lastUid); };
  await stopPolling();
  const firstUid = state.lastUid;
  pollLoop(firstUid, async (t) => {
    state.tag = t; state.lastUid = t.uid;
    setScan("done", "Second tag read", t.uid);
    await linkSecondTag(spoolId, t);
  });
}

// ---------------------------------------------------------------- wiring

form.addEventListener("submit", (e) => { e.preventDefault(); go($("opt-link").checked, $("opt-write").checked); });
$("btn-save-only").onclick = () => go(false, false);
$("btn-clear").onclick = () => clearForm();
$("opt-link").onchange = updateGoLabel;
$("opt-write").onchange = () => { state.writePref = $("opt-write").checked; updateGoLabel(); };
$("tab-new").onclick = () => setMode("new");
$("tab-existing").onclick = () => { setMode("existing"); renderSpoolList(); $("spool-search").focus(); };
$("spool-search").oninput = renderSpoolList;
$("filament-search").oninput = renderFilamentList;
$("btn-upc").onclick = lookupUpc;
$("upc").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); lookupUpc(); } });
$("color-pick").oninput = () => { form.elements.color_hex.value = $("color-pick").value.slice(1).toUpperCase(); };
form.elements.color_hex.oninput = syncColor;
form.addEventListener("input", (e) => { if (e.target.classList) e.target.classList.remove("prefilled"); updateWeigh(); });
form.addEventListener("change", updateWeigh);
$("btn-scan").onclick = () => { stopPolling().then(() => pollLoop(state.lastUid)); };
$("btn-read").onclick = readNow;
$("autoscan").onchange = () => { if ($("autoscan").checked) pollLoop(state.lastUid); else { stopPolling(); setScan("idle", "Auto-scan off", "Use Read now or Scan next tag."); } };
$("btn-settings").onclick = () => $("dlg-settings").showModal();
$("dlg-settings").onclose = async () => {
  if ($("dlg-settings").returnValue !== "ok") return;
  try { await api("/api/settings", { spoolman_url: $("set-url").value, tag_store: $("set-store").value }); toast("Settings saved"); }
  catch (e) { toast(e.message); }
  await refreshStatus(); await loadCatalog();
};
document.querySelectorAll("[data-fake]").forEach((b) => (b.onclick = async () => {
  const k = b.dataset.fake;
  const uid = k === "blank2" ? "04" + Array.from({ length: 6 }, () => Math.floor(Math.random() * 256).toString(16).padStart(2, "0")).join("").toUpperCase() : k === "classic" ? "1D9B9E1A" : undefined;
  await api("/api/fake/tag", { kind: k === "blank2" ? "blank" : k, uid });
  toast("Simulated tag: " + b.textContent);
}));
window.addEventListener("beforeunload", () => { navigator.sendBeacon && navigator.sendBeacon("/api/tag/cancel", new Blob(["{}"], { type: "application/json" })); });

(async function init() {
  syncColor(); updateGoLabel();
  await refreshStatus();
  await loadCatalog();
  if ($("autoscan").checked) pollLoop(null);
  else setScan("idle", "Auto-scan off");
  setInterval(refreshStatusQuiet, 30000);
})();

async function refreshStatusQuiet() { if (!state.polling) await refreshStatus(); }
