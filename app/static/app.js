"use strict";

// ---- helpers ---------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function errText(body, status) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
  return `Fout ${status}`;
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opt);
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch (_) { /* geen json */ }
  if (!r.ok) throw new Error(errText(data, r.status));
  return data;
}

function fmtBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1e3)) + " KB";
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("nl-NL", { dateStyle: "short", timeStyle: "medium" }) : "";
}

function select(options, value, attrs) {
  const s = h("select", attrs || {});
  for (const o of options) {
    const [val, label] = Array.isArray(o) ? o : [o, o];
    const opt = h("option", { value: val }, label);
    if (String(val) === String(value)) opt.selected = true;
    s.append(opt);
  }
  return s;
}

function heading(n, text) { return h("h2", {}, h("span", { class: "num" }, n), text); }

function field(label, input, hint) {
  return h("div", {}, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);
}

// lucide-iconen, 16px, lijndikte 1.5 (zie .i in style.css)
const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
};
function ico(name) {
  const s = h("span", { class: "i", "aria-hidden": "true" });
  s.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[name]}</svg>`;
  return s;
}

function setBar(fill, fraction) { fill.style.transform = `scaleX(${Math.min(1, Math.max(0, fraction))})`; }

// ---- router ------------------------------------------------------------------
const view = document.getElementById("view");
let cleanup = null;

function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || "#/uitrollen";
  const [, page, arg] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === (page === "job" || page === "batch" ? "geschiedenis" : page)));
  view.replaceChildren();
  const views = { uitrollen: viewDeploy, geschiedenis: viewHistory, servers: viewServers, job: viewJob, batch: viewBatch };
  (views[page] || viewDeploy)(arg);
}
window.addEventListener("hashchange", route);

// ---- Uitrollen -----------------------------------------------------------------
// Eén pagina met een gedeelde server/node en één blok per VM-soort (bestand + instellingen + aantal).
function viewDeploy() {
  const shared = { serverId: null, node: null, storages: [], importStorages: [], bridges: [], lastUsed: {}, listeners: [] };
  const blocks = [];
  const root = h("div");
  view.append(h("h1", {}, "Uitrollen"), root);

  const errBox = h("div", { class: "error", hidden: true });
  const nodeSel = h("select", { onchange: () => loadNode(nodeSel.value) });
  const serverSel = h("select", { onchange: () => loadServer(Number(serverSel.value)) });
  const importWarn = h("div", { class: "warn", hidden: true }, "Deze node heeft geen storage met content-type 'import'. Zet dat aan bij Datacenter → Storage.");
  const blocksBox = h("div");
  const summary = h("span", { class: "muted" });
  const submit = h("button", { class: "primary", disabled: true, onclick: doDeploy }, "Uitrollen");
  const addBtn = h("button", { class: "link", onclick: () => addBlock() }, ico("plus"), "Nog een VM toevoegen");

  root.append(errBox,
    h("div", { class: "card" }, h("h2", {}, "Proxmox-server"), h("div", { class: "row" }, field("Server", serverSel), field("Node", nodeSel)), importWarn),
    blocksBox, addBtn,
    h("div", { class: "actions space" }, summary, submit));

  function showErr(msg) { errBox.textContent = msg || ""; errBox.hidden = !msg; if (msg) window.scrollTo({ top: 0, behavior: "smooth" }); }

  function refresh() {
    blocks.forEach((b, i) => b.setIndex(i + 1, blocks.length > 1));
    const ok = shared.serverId && shared.node && shared.importStorages.length && blocks.length && blocks.every((b) => b.ready());
    submit.disabled = !ok;
    const total = blocks.reduce((n, b) => n + b.count(), 0);
    summary.textContent = blocks.length > 1 || total > 1 ? blocks.map((b) => `${b.label()} ×${b.count()}`).join(" · ") : "";
    submit.textContent = total > 1 ? `${total} VM's uitrollen` : "Uitrollen";
  }

  function addBlock() {
    const block = createVmBlock(shared, refresh, (b) => {
      b.dispose();
      blocks.splice(blocks.indexOf(b), 1);
      b.el.remove();
      refresh();
    });
    blocks.push(block);
    blocksBox.append(block.el);
    refresh();
    return block;
  }

  async function loadServer(id) {
    shared.serverId = id; shared.node = null;
    nodeSel.replaceChildren(); refresh();
    if (!id) return;
    try {
      const nodes = await api("GET", `/servers/${id}/nodes`);
      shared.lastUsed = await api("GET", `/servers/${id}/last-used`);
      nodeSel.replaceChildren(...nodes.map((n) => h("option", { value: n }, n)));
      await loadNode(nodes[0]);
    } catch (e) { showErr(`Kon gegevens van de server niet ophalen: ${e.message}`); }
  }

  async function loadNode(node) {
    shared.node = node; showErr("");
    try {
      const [s, b] = await Promise.all([api("GET", `/servers/${shared.serverId}/nodes/${node}/storages`), api("GET", `/servers/${shared.serverId}/nodes/${node}/bridges`)]);
      shared.storages = s.storages; shared.importStorages = s.import_storages; shared.bridges = b;
      importWarn.hidden = shared.importStorages.length > 0;
      blocks.forEach((blk) => blk.refreshShared());
      refresh();
    } catch (e) { showErr(`Kon gegevens van de node niet ophalen: ${e.message}`); }
  }

  async function doDeploy() {
    showErr(""); submit.disabled = true;
    try {
      const r = await api("POST", "/batches", { server_id: shared.serverId, items: blocks.map((b) => b.toItem()) });
      location.hash = r.jobs.length === 1 ? `#/job/${r.jobs[0].id}` : `#/batch/${r.id}`;
    } catch (e) { showErr(e.message); refresh(); }
  }

  addBlock();
  api("GET", "/servers").then((servers) => {
    if (!servers.length) {
      root.prepend(h("div", { class: "warn" }, "Er zijn nog geen Proxmox-servers. ", h("a", { href: "#/servers" }, "Voeg er eerst een toe.")));
      return;
    }
    serverSel.replaceChildren(...servers.map((s) => h("option", { value: s.id }, s.name)));
    loadServer(servers[0].id);
  }).catch((e) => showErr(e.message));
}

// Eén VM-blok: bestand, naam, aantal, hardware, extra disks en netwerk.
function createVmBlock(shared, onChange, onRemove) {
  const st = { extra: [], upload: null, candidate: null, nics: [], uploading: false };
  const nameIn = h("input", { placeholder: "bijv. fgt-lab", oninput: () => { updateHead(); updateCountHint(); onChange(); } });
  const countIn = h("input", { type: "number", min: 1, max: 20, value: 1, oninput: () => { updateCountHint(); onChange(); } });
  const countHint = h("div", { class: "hint" });
  const cores = h("input", { type: "number", min: 1, value: 2 });
  const cpuType = h("input", { value: "host" });
  const mem = h("input", { type: "number", min: 256, step: 256, value: 2048 });
  const diskIn = h("input", { type: "number", min: 1, value: 2 });
  const dsSel = h("select", { onchange: onChange });
  const candSel = h("select", { onchange: () => pickCandidate(candSel.value) });
  const candWrap = h("div", { hidden: true }, field("Image in de zip", candSel));
  const nicBox = h("div");
  const extraBox = h("div", { class: "tight" });
  const machine = h("input", { value: "pc" });
  const bios = select(["seabios", "ovmf"], "seabios");
  const scsihw = select(["virtio-scsi-single", "virtio-scsi-pci", "lsi"], "virtio-scsi-single");
  const nicModel = select(["virtio", "e1000", "vmxnet3"], "virtio");
  const vmidIn = h("input", { type: "number", min: 100, placeholder: "automatisch" });
  const startAfter = h("input", { type: "checkbox" });
  const extraHint = h("div", { class: "warn", hidden: true }, "Dit lijkt een FortiManager of FortiAnalyzer. Die heeft een tweede disk nodig voor /var (data en logs), anders krijg je 'ERROR: /var not mounted'. Er is alvast een disk toegevoegd.");

  const dzText = h("div", {}, "Sleep een .qcow2 of .zip hierheen of klik om te kiezen");
  const dzSub = h("small", {}, "");
  const bar = h("div", { class: "bar", hidden: true }, h("div"));
  const fileIn = h("input", { type: "file", accept: ".qcow2,.zip", hidden: true, onchange: () => fileIn.files[0] && sendFile(fileIn.files[0]) });
  const dz = h("div", { class: "dropzone", onclick: () => fileIn.click() }, dzText, dzSub, bar, fileIn);
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) sendFile(e.dataTransfer.files[0]); });
  const errLine = h("div", { class: "error", hidden: true });

  const num = h("span", { class: "num" }, "VM 1");
  const title = h("span", {});
  const removeBtn = h("button", { class: "icon", "aria-label": "VM verwijderen", onclick: () => onRemove(api_) }, ico("x"));
  const el = h("div", { class: "card vmcard" },
    h("div", { class: "vm-head" }, h("h2", {}, num, title), removeBtn),
    errLine,
    h("div", { class: "vmgrid" },
      h("div", {}, dz, candWrap, extraHint),
      h("div", {},
        h("div", { class: "row" }, field("Naam", nameIn), h("div", { class: "narrow" }, field("Aantal", countIn))),
        countHint,
        h("div", { class: "row" }, field("CPU cores", cores), field("CPU type", cpuType), field("RAM (MB)", mem)),
        h("div", { class: "row" }, field("Datastore", dsSel), field("Disk (GB)", diskIn, "Minimaal de grootte van het image")),
        h("div", {}, h("label", {}, "Extra disks"), h("div", { class: "hint", style: "margin-top: 0" }, "Lege data-disks, bijv. /var voor FortiManager"), extraBox,
          h("button", { class: "link", onclick: () => { st.extra.push({ size: 100, datastore: "" }); renderExtra(); } }, ico("plus"), "Disk toevoegen")),
        h("div", {}, h("label", {}, "Netwerk"), nicBox,
          h("button", { class: "link", onclick: () => { st.nics.push({ bridge: shared.bridges[0] || "", vlan: "" }); renderNics(); } }, ico("plus"), "Interface toevoegen")),
        h("details", {}, h("summary", {}, "Geavanceerd"),
          h("div", { class: "row" }, field("Machine", machine), field("BIOS", bios), field("Disk-controller", scsihw)),
          h("div", { class: "row" }, field("NIC-model", nicModel), field("VMID", vmidIn, "Leeg = volgende vrije; alleen bij aantal 1")),
          h("div", { class: "inline" }, startAfter, h("label", {}, "VM starten na uitrollen"))))));

  function updateHead() { title.textContent = nameIn.value.trim(); }

  function count() { return Math.max(1, Math.min(20, Number(countIn.value) || 1)); }

  function updateCountHint() {
    const n = count(); const name = nameIn.value.trim() || "naam";
    countHint.textContent = n > 1 ? `Wordt uitgerold als ${name}-1 t/m ${name}-${n}` : "";
    vmidIn.disabled = n > 1; if (n > 1) vmidIn.value = "";
  }

  function renderNics() {
    nicBox.replaceChildren(...st.nics.map((n, i) => {
      const br = select(shared.bridges.length ? shared.bridges : [n.bridge || ""], n.bridge, { onchange: () => { n.bridge = br.value; } });
      const vl = h("input", { class: "vlan", type: "number", min: 1, max: 4094, placeholder: "VLAN", value: n.vlan ?? "", oninput: () => { n.vlan = vl.value; } });
      return h("div", { class: "nicrow" }, h("span", { class: "nlabel" }, "net" + i), br, vl,
        st.nics.length > 1 ? h("button", { class: "icon", "aria-label": "Interface verwijderen", onclick: () => { st.nics.splice(i, 1); renderNics(); onChange(); } }, ico("x")) : null);
    }));
    onChange();
  }

  function renderExtra() {
    extraBox.replaceChildren(...st.extra.map((d, i) => {
      const size = h("input", { type: "number", min: 1, value: d.size, oninput: () => { d.size = size.value; } });
      const ds = select([["", "zelfde datastore als boot-disk"], ...shared.storages.map((x) => x.name)], d.datastore, { onchange: () => { d.datastore = ds.value; } });
      return h("div", { class: "nicrow" }, h("span", { class: "nlabel" }, "scsi" + (i + 1)), size, h("span", { class: "muted" }, "GB"), ds,
        h("button", { class: "icon", "aria-label": "Disk verwijderen", onclick: () => { st.extra.splice(i, 1); renderExtra(); } }, ico("x")));
    }));
  }

  function pickCandidate(name) {
    st.candidate = name;
    const c = st.upload.candidates.find((x) => x.name === name);
    diskIn.min = c.virtual_gb;
    diskIn.value = c.virtual_gb;
    onChange();
  }

  function showErr(msg) { errLine.textContent = msg || ""; errLine.hidden = !msg; }

  function sendFile(file) {
    showErr("");
    if (st.upload) api("DELETE", `/uploads/${st.upload.upload_id}`).catch(() => {});
    st.upload = null; st.candidate = null; st.uploading = true; candWrap.hidden = true;
    dz.classList.remove("has"); bar.hidden = false; setBar(bar.firstChild, 0);
    dzText.textContent = file.name; dzSub.textContent = "Uploaden…";
    onChange();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { setBar(bar.firstChild, e.loaded / e.total); dzSub.textContent = `Uploaden… ${Math.round(100 * e.loaded / e.total)}%`; } };
    xhr.onload = () => {
      st.uploading = false; bar.hidden = true;
      let body = null; try { body = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status !== 200) { dzText.textContent = "Sleep een .qcow2 of .zip hierheen of klik om te kiezen"; dzSub.textContent = ""; showErr(errText(body, xhr.status)); onChange(); return; }
      st.upload = body; dz.classList.add("has");
      dzSub.textContent = `${fmtBytes(file.size)} · ${body.candidates.length} image(s) gevonden`;
      candSel.replaceChildren(...body.candidates.map((c) => h("option", { value: c.name }, `${c.name} (${c.virtual_gb} GB)`)));
      candWrap.hidden = body.candidates.length < 2;
      if (!nameIn.value) nameIn.value = body.candidates[0].name.replace(/\.qcow2$/i, "").replace(/[^A-Za-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
      updateHead(); updateCountHint();
      pickCandidate(body.candidates[0].name);
      const needsDataDisk = /fmg|fortimanager|faz|fortianalyzer/i.test(file.name + " " + body.candidates[0].name);
      extraHint.hidden = !needsDataDisk;
      if (needsDataDisk && !st.extra.length) { st.extra.push({ size: 100, datastore: "" }); renderExtra(); }
    };
    xhr.onerror = () => { st.uploading = false; bar.hidden = true; showErr("Upload mislukt: geen verbinding met de app"); onChange(); };
    const fd = new FormData(); fd.append("file", file); xhr.send(fd);
  }

  const api_ = {
    el,
    count,
    label() { return nameIn.value.trim() || "(naamloos)"; },
    dispose() { if (st.upload) api("DELETE", `/uploads/${st.upload.upload_id}`).catch(() => {}); },
    setIndex(n, removable) { num.textContent = "VM " + n; removeBtn.hidden = !removable; },
    ready() { return !!(st.upload && st.candidate && nameIn.value.trim() && dsSel.value && st.nics.length && !st.uploading); },
    refreshShared() {
      const lu = shared.lastUsed || {};
      const want = dsSel.value || lu.datastore;
      dsSel.replaceChildren(...shared.storages.map((x) => h("option", { value: x.name }, x.name)));
      if (want && shared.storages.some((x) => x.name === want)) dsSel.value = want;
      if (!st.nics.length) st.nics.push({ bridge: lu.bridge || shared.bridges[0] || "", vlan: lu.vlan ?? "" });
      st.nics.forEach((n) => { if (!shared.bridges.includes(n.bridge)) n.bridge = lu.bridge && shared.bridges.includes(lu.bridge) ? lu.bridge : shared.bridges[0] || ""; });
      renderExtra(); renderNics();
    },
    toItem() {
      const n = count();
      return {
        upload_id: st.upload.upload_id, candidate: st.candidate, count: n,
        config: {
          name: nameIn.value.trim(), node: shared.node, datastore: dsSel.value, import_storage: shared.importStorages[0],
          disk_gb: Number(diskIn.value), vmid: n === 1 && vmidIn.value ? Number(vmidIn.value) : null,
          cores: Number(cores.value), memory_mb: Number(mem.value), cpu_type: cpuType.value.trim() || "host",
          bios: bios.value, machine: machine.value.trim() || "pc", scsihw: scsihw.value,
          nics: st.nics.map((x) => ({ bridge: x.bridge, vlan: x.vlan === "" || x.vlan == null ? null : Number(x.vlan), model: nicModel.value })),
          extra_disks: st.extra.map((d) => ({ size_gb: Number(d.size), datastore: d.datastore || null })),
          start_after: startAfter.checked,
        },
      };
    },
  };
  api_.refreshShared();
  return api_;
}

// ---- Voortgang -------------------------------------------------------------------
const STEPS = [
  ["ontvangen", "Bestand ontvangen"], ["uitgepakt", "Image klaar"], ["uploaden", "Uploaden naar Proxmox"], ["cloudinit", "Day-0 configuratie"],
  ["vm_aanmaken", "VM aanmaken"], ["disk_importeren", "Disk importeren"], ["resize", "Disk vergroten"],
  ["netwerk_boot", "Netwerk en boot instellen"], ["klaar", "Klaar"],
];
function stateIcon(state) {
  if (state === "done") return ico("check");
  if (state === "failed") return ico("x");
  return h("span", { class: "dot " + state });
}

function viewJob(id) {
  const stepState = {}; const stepMsg = {}; let progress = null; let speed = "";
  const log = []; let job = null; let es = null; let stopped = false;
  const title = h("h1", {}, "Uitrol");
  const banner = h("div");
  const list = h("ul", { class: "steps" });
  const barFill = h("div"); const bar = h("div", { class: "bar", hidden: true }, barFill);
  const logPre = h("pre", { class: "log" });
  view.append(title, banner, h("div", { class: "card" }, list, bar),
    h("div", { class: "card" }, h("details", {}, h("summary", {}, "Logboek"), logPre)),
    h("a", { href: "#/uitrollen" }, ico("arrow-left"), "Nieuwe uitrol"));

  function apply(e) {
    stepState[e.step] = e.state;
    if (e.message) stepMsg[e.step] = e.message;
    if (e.step === "uploaden" && e.progress != null) { progress = e.progress; speed = e.message; }
    log.push(`${new Date(e.ts * 1000).toLocaleTimeString("nl-NL")}  [${e.step}] ${e.state}  ${e.message || ""}`);
  }

  function render() {
    list.replaceChildren(...STEPS.map(([key, label]) => {
      const state = stepState[key] || "pending";
      const msg = key === "uploaden" && state === "running" && progress != null ? `${Math.round(progress * 100)}% · ${speed}` : stepMsg[key];
      return h("li", { class: state }, h("span", { class: "ic" }, stateIcon(state)), h("span", {}, label), msg && key !== "ontvangen" ? h("span", { class: "msg" }, msg) : null);
    }));
    const up = stepState.uploaden === "running" && progress != null;
    bar.hidden = !up; setBar(barFill, progress || 0);
    logPre.textContent = log.join("\n");
    if (job) {
      title.textContent = `Uitrol: ${job.vm_name}${job.server_name ? " op " + job.server_name : ""}`;
      banner.className = job.status === "done" ? "ok" : job.status === "failed" ? "error" : "";
      banner.textContent = job.status === "done" ? `VM ${job.vmid} is aangemaakt.` : job.status === "failed" ? `Mislukt: ${job.error || "onbekende fout"}` : "";
    }
  }

  const seen = new Set();
  function applyOnce(e) { if (!seen.has(e.id)) { seen.add(e.id); apply(e); } }

  async function refreshJob() { job = await api("GET", `/deployments/${id}`); return job; }

  function stream() {
    if (stopped) return;
    es = new EventSource(`/api/deployments/${id}/events`);
    es.addEventListener("step", (m) => { applyOnce(JSON.parse(m.data)); render(); });
    // De server sluit de stream als de uitrol klaar is; dan volgt onerror. Alleen opnieuw verbinden als de job nog loopt.
    es.onerror = async () => {
      es.close();
      try { const j = await refreshJob(); j.events.forEach(applyOnce); } catch (_) { /* later opnieuw */ }
      render();
      if (!stopped && (!job || job.status === "running")) setTimeout(stream, 1500);
    };
  }

  refreshJob().then((j) => {
    j.events.forEach(applyOnce); render();
    if (j.status === "running") stream();
  }).catch((e) => { banner.className = "error"; banner.textContent = e.message; });

  cleanup = () => { stopped = true; if (es) es.close(); };
}

// ---- Batch-voortgang ---------------------------------------------------------------
function viewBatch(id) {
  const title = h("h1", {}, "Uitrol");
  const banner = h("div");
  const list = h("div", { class: "card batch" });
  view.append(title, banner, list, h("a", { href: "#/uitrollen" }, ico("arrow-left"), "Nieuwe uitrol"));
  const LABEL = Object.fromEntries(STEPS);
  let timer = null; let stopped = false;

  function render(b) {
    const n = b.jobs.length;
    const done = b.jobs.filter((j) => j.status === "done").length;
    const failed = b.jobs.filter((j) => j.status === "failed").length;
    const running = n - done - failed;
    title.textContent = `Uitrol van ${n} VM${n === 1 ? "" : "'s"}${b.server_name ? " op " + b.server_name : ""}`;
    banner.className = running ? "" : failed ? "error" : "ok";
    banner.textContent = running ? `${done} van ${n} klaar${failed ? `, ${failed} mislukt` : ""}` : failed ? `${failed} van ${n} mislukt, ${done} klaar` : `Alle ${n} VM's zijn aangemaakt`;
    list.replaceChildren(...b.jobs.map((j) => {
      const open = () => { location.hash = `#/job/${j.id}`; };
      const up = j.state === "running" && j.step === "uploaden" && j.upload_progress != null;
      const fill = h("div"); setBar(fill, j.upload_progress || 0);
      const what = j.status === "failed" ? (j.error || "Mislukt") : j.status === "done" ? `Klaar${j.vmid ? " · VMID " + j.vmid : ""}` :
        `${LABEL[j.step] || "Wachten"}${up ? ` · ${Math.round(j.upload_progress * 100)}%` : ""}`;
      return h("div", { class: "batchrow click", tabindex: 0, onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
        h("span", { class: "ic" }, stateIcon(j.status === "running" ? "running" : j.status)),
        h("span", { class: "mono name" }, j.vm_name),
        h("span", { class: j.status === "failed" ? "what bad" : "what" }, what),
        up ? h("div", { class: "bar" }, fill) : null);
    }));
  }

  async function tick() {
    if (stopped) return;
    try {
      const b = await api("GET", `/batches/${id}`);
      render(b);
      if (b.jobs.some((j) => j.status === "running")) timer = setTimeout(tick, 1000);
    } catch (e) { banner.className = "error"; banner.textContent = e.message; }
  }
  tick();
  cleanup = () => { stopped = true; clearTimeout(timer); };
}

// ---- Geschiedenis ----------------------------------------------------------------
function viewHistory() {
  const holder = h("div", { class: "card" }, "Laden…");
  view.append(h("h1", {}, "Geschiedenis"), holder);
  api("GET", "/deployments").then((jobs) => {
    if (!jobs.length) { holder.replaceChildren(h("span", { class: "muted" }, "Nog geen uitrollen")); return; }
    holder.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Tijd", "Naam", "Server", "VMID", "Status"].map((t) => h("th", {}, t)))),
      h("tbody", {}, jobs.map((j) => {
        const open = () => { location.hash = `#/job/${j.id}`; };
        return h("tr", { class: "click", tabindex: 0, onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
          h("td", { class: "mono" }, fmtTime(j.created_at)), h("td", { class: "mono" }, j.vm_name), h("td", {}, j.server_name || "—"), h("td", { class: "mono" }, j.vmid ?? "—"),
          h("td", {}, h("span", { class: "status" }, h("span", { class: "dot " + j.status }), { done: "Klaar", failed: "Mislukt", running: "Bezig" }[j.status] || j.status)));
      }))));
  }).catch((e) => holder.replaceChildren(h("div", { class: "error" }, e.message)));
}

// ---- Servers ---------------------------------------------------------------------
function viewServers() {
  const listBox = h("div", { class: "card" }); const formBox = h("div", { class: "card" });
  view.append(h("h1", {}, "Servers"), listBox, formBox);
  let editing = null;

  async function load() {
    const servers = await api("GET", "/servers");
    if (!servers.length) { listBox.replaceChildren(h("span", { class: "muted" }, "Nog geen servers. Voeg er hieronder een toe.")); return; }
    listBox.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Naam", "URL", "Token-ID", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, servers.map((s) => {
        const result = h("div", { class: "hint" });
        return h("tr", {}, h("td", {}, s.name), h("td", { class: "mono" }, s.url, s.verify_tls ? null : h("span", { class: "muted" }, " (TLS niet gecontroleerd)")), h("td", { class: "mono" }, s.token_id),
          h("td", { class: "actions-cell" },
            h("button", { class: "link", onclick: async () => {
              result.textContent = "Testen…"; result.className = "hint";
              try { const r = await api("POST", `/servers/${s.id}/test`); result.textContent = `Proxmox VE ${r.version}` + (r.warning ? ` — ${r.warning}` : ""); result.className = r.warning ? "warn" : "ok"; }
              catch (e) { result.textContent = e.message; result.className = "error"; }
            } }, "Test verbinding"),
            h("button", { class: "link", onclick: () => { editing = s; renderForm(); } }, "Bewerken"),
            h("button", { class: "link danger", onclick: async () => { if (confirm(`Server '${s.name}' verwijderen?`)) { await api("DELETE", `/servers/${s.id}`); load(); } } }, "Verwijderen"),
            result));
      }))));
  }

  function renderForm() {
    const s = editing;
    const name = h("input", { value: s ? s.name : "" });
    const url = h("input", { value: s ? s.url : "", placeholder: "https://pve1.lan:8006" });
    const tid = h("input", { value: s ? s.token_id : "", placeholder: "user@realm!tokenname" });
    const sec = h("input", { type: "password", placeholder: s ? "Leeg laten om te behouden" : "", autocomplete: "off" });
    const tls = h("input", { type: "checkbox", id: "skiptls" }); tls.checked = s ? !s.verify_tls : false;
    const err = h("div", { class: "error", hidden: true });
    formBox.replaceChildren(h("h2", {}, s ? `Bewerken: ${s.name}` : "Server toevoegen"), err,
      h("div", { class: "row" }, field("Naam", name), field("URL", url)),
      h("div", { class: "row" }, field("Token-ID", tid), field("Token-secret", sec)),
      h("div", { class: "inline" }, tls, h("label", { for: "skiptls" }, "TLS-certificaat niet controleren (zelfgetekend certificaat)")),
      h("div", { class: "actions" }, s ? h("button", { onclick: () => { editing = null; renderForm(); } }, "Annuleren") : null,
        h("button", { class: "primary", onclick: async () => {
          const body = { name: name.value.trim(), url: url.value.trim(), token_id: tid.value.trim(), token_secret: sec.value || null, verify_tls: !tls.checked };
          try { await api(s ? "PUT" : "POST", s ? `/servers/${s.id}` : "/servers", body); editing = null; renderForm(); load(); }
          catch (e) { err.textContent = e.message; err.hidden = false; }
        } }, s ? "Opslaan" : "Toevoegen")));
  }
  renderForm(); load().catch((e) => listBox.replaceChildren(h("div", { class: "error" }, e.message)));
}

route();
