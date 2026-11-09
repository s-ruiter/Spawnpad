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
  return `Error ${status}`;
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/api" + path, opt);
  if (r.status === 204) return null;
  let data = null;
  try { data = await r.json(); } catch (_) { /* no json */ }
  if (!r.ok) throw new Error(errText(data, r.status));
  return data;
}

function fmtBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1e3)) + " KB";
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "medium" }) : "";
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

// lucide icons, 16px, stroke 1.5 (see .i in style.css)
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
  const hash = location.hash || "#/deploy";
  const [, page, arg] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === (page === "job" || page === "batch" ? "history" : page)));
  view.replaceChildren();
  const views = { deploy: viewDeploy, history: viewHistory, servers: viewServers, job: viewJob, batch: viewBatch };
  (views[page] || viewDeploy)(arg);
}
window.addEventListener("hashchange", route);

// ---- Deploy -----------------------------------------------------------------
// One page with a shared server/node and one block per VM kind (file + settings + count).
function viewDeploy() {
  const shared = { serverId: null, node: null, storages: [], importStorages: [], isoStorages: [], bridges: [], lastUsed: {}, listeners: [] };
  const blocks = [];
  const root = h("div");
  view.append(h("h1", {}, "Deploy"), root);

  const errBox = h("div", { class: "error", hidden: true });
  const nodeSel = h("select", { onchange: () => loadNode(nodeSel.value) });
  const serverSel = h("select", { onchange: () => loadServer(Number(serverSel.value)) });
  const importWarn = h("div", { class: "warn", hidden: true }, "This node has no storage with content type 'import'. Enable it under Datacenter → Storage.");
  const blocksBox = h("div");
  const summary = h("span", { class: "muted" });
  const submit = h("button", { class: "primary", disabled: true, onclick: doDeploy }, "Deploy");
  const addBtn = h("button", { class: "link", onclick: () => addBlock() }, ico("plus"), "Add another VM");

  root.append(errBox,
    h("div", { class: "card" }, h("h2", {}, "Proxmox server"), h("div", { class: "row" }, field("Server", serverSel), field("Node", nodeSel)), importWarn),
    blocksBox, addBtn,
    h("div", { class: "actions space" }, summary, submit));

  function showErr(msg) { errBox.textContent = msg || ""; errBox.hidden = !msg; if (msg) window.scrollTo({ top: 0, behavior: "smooth" }); }

  function refresh() {
    blocks.forEach((b, i) => b.setIndex(i + 1, blocks.length > 1));
    const ok = shared.serverId && shared.node && shared.importStorages.length && blocks.length && blocks.every((b) => b.ready());
    submit.disabled = !ok;
    const total = blocks.reduce((n, b) => n + b.count(), 0);
    summary.textContent = blocks.length > 1 || total > 1 ? blocks.map((b) => `${b.label()} ×${b.count()}`).join(" · ") : "";
    submit.textContent = total > 1 ? `Deploy ${total} VMs` : "Deploy";
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
    } catch (e) { showErr(`Could not fetch server data: ${e.message}`); }
  }

  async function loadNode(node) {
    shared.node = node; showErr("");
    try {
      const [s, b] = await Promise.all([api("GET", `/servers/${shared.serverId}/nodes/${node}/storages`), api("GET", `/servers/${shared.serverId}/nodes/${node}/bridges`)]);
      shared.storages = s.storages; shared.importStorages = s.import_storages; shared.isoStorages = s.iso_storages || []; shared.bridges = b;
      importWarn.hidden = shared.importStorages.length > 0;
      blocks.forEach((blk) => blk.refreshShared());
      refresh();
    } catch (e) { showErr(`Could not fetch node data: ${e.message}`); }
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
      root.prepend(h("div", { class: "warn" }, "There are no Proxmox servers yet. ", h("a", { href: "#/servers" }, "Add one first.")));
      return;
    }
    serverSel.replaceChildren(...servers.map((s) => h("option", { value: s.id }, s.name)));
    loadServer(servers[0].id);
  }).catch((e) => showErr(e.message));
}

// One VM block: file, name, count, hardware, extra disks and network.
function createVmBlock(shared, onChange, onRemove) {
  const st = { extra: [], upload: null, candidate: null, nics: [], uploading: false };
  const nameIn = h("input", { placeholder: "e.g. fgt-lab", oninput: () => { updateHead(); updateCountHint(); onChange(); } });
  const countIn = h("input", { type: "number", min: 1, max: 20, value: 1, oninput: () => { updateCountHint(); onChange(); } });
  const countHint = h("div", { class: "hint" });
  const cores = h("input", { type: "number", min: 1, value: 2 });
  const cpuType = h("input", { value: "host" });
  const mem = h("input", { type: "number", min: 256, step: 256, value: 2048 });
  const diskIn = h("input", { type: "number", min: 1, value: 2 });
  const dsSel = h("select", { onchange: onChange });
  const candSel = h("select", { onchange: () => pickCandidate(candSel.value) });
  const candWrap = h("div", { hidden: true }, field("Image in the zip", candSel));
  const nicBox = h("div");
  const extraBox = h("div", { class: "tight" });
  const machine = h("input", { value: "pc" });
  const bios = select(["seabios", "ovmf"], "seabios");
  const scsihw = select([["", "Default (Proxmox)"], "virtio-scsi-single", "virtio-scsi-pci", "lsi"], "");
  const nicModel = select(["virtio", "e1000", "vmxnet3"], "virtio");
  const vmidIn = h("input", { type: "number", min: 100, placeholder: "automatic" });
  const startAfter = h("input", { type: "checkbox" });
  const extraHint = h("div", { class: "warn", hidden: true }, "This looks like a FortiManager or FortiAnalyzer. It needs a second disk for /var (data and logs), otherwise you get 'ERROR: /var not mounted'. A disk has already been added.");

  // Day-0 configuration (cloud-init via config-drive ISO)
  const d0On = h("input", { type: "checkbox", onchange: () => { d0Box.hidden = !d0On.checked; onChange(); } });
  const prodSel = select([["fortigate", "FortiGate"], ["fortimanager", "FortiManager"], ["fortianalyzer", "FortiAnalyzer"], ["other", "Other (custom CLI only)"]], "fortigate", { onchange: () => { if (prodSel.value === "fortimanager" || prodSel.value === "fortianalyzer") netMode.value = "static"; syncD0(); } });
  const hostIn = h("input", { value: "{name}", placeholder: "{name}" });
  const pwIn = h("input", { type: "password", autocomplete: "new-password", placeholder: "Empty = do not set" });
  const netMode = select([["dhcp", "DHCP"], ["static", "Static IP"]], "dhcp", { onchange: syncD0 });
  const ifIn = h("input", { value: "port1" });
  const ipIn = h("input", { placeholder: "10.0.0.11/24" });
  const gwIn = h("input", { placeholder: "10.0.0.1" });
  const dns1 = h("input", { placeholder: "DNS 1" });
  const dns2 = h("input", { placeholder: "DNS 2 (optional)" });
  const staticBox = h("div", { hidden: true }, h("div", { class: "row" }, field("IP address with prefix", ipIn, "With count > 1 the address increments per VM"), field("Gateway", gwIn)), h("div", { class: "row" }, field("DNS 1", dns1), field("DNS 2", dns2)));
  const cliIn = h("textarea", { placeholder: "config system global\n    set timezone 26\nend", spellcheck: "false" });
  const tokIn = h("textarea", { placeholder: "One FortiFlex token per line (one per VM)", spellcheck: "false", oninput: () => { syncD0(); } });
  const tokHint = h("div", { class: "hint" });
  const isoSel = h("select", { onchange: onChange });
  const isoWarn = h("div", { class: "warn", hidden: true }, "This node has no storage with content type 'ISO'. Enable it under Datacenter → Storage.");
  const previewOut = h("pre", { class: "log", hidden: true });
  const previewErr = h("div", { class: "error", hidden: true });
  const previewBtn = h("button", { onclick: showPreview }, "Show preview");
  const d0Box = h("div", { class: "d0", hidden: true },
    h("div", { class: "row" }, field("Type", prodSel), field("Hostname", hostIn, "Variables: {name} {n} {count} {ip}"), field("Admin password", pwIn)),
    h("div", { class: "row" }, field("Network", netMode), field("Interface", ifIn)),
    staticBox,
    field("Custom CLI (placed after the generated config)", cliIn, "Variables: {name} {n} {count} {ip}. May be left empty."),
    field("FortiFlex tokens", tokIn, "Applied with 'execute vm-license <token>' at the end of the CLI"), tokHint,
    field("Storage for the config drive (ISO)", isoSel), isoWarn,
    h("div", {}, previewBtn), previewErr, previewOut);
  const d0Details = h("details", {}, h("summary", {}, "Day-0 configuration (cloud-init)"),
    h("div", { class: "inline" }, d0On, h("label", {}, "Provide configuration and license at first boot")), d0Box);
  const dzText = h("div", {}, "Drop a .qcow2 or .zip here or click to choose");
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
  const removeBtn = h("button", { class: "icon", "aria-label": "Remove VM", onclick: () => onRemove(api_) }, ico("x"));
  const el = h("div", { class: "card vmcard" },
    h("div", { class: "vm-head" }, h("h2", {}, num, title), removeBtn),
    errLine,
    h("div", { class: "vmgrid" },
      h("div", {}, dz, candWrap, extraHint),
      h("div", {},
        h("div", { class: "row" }, field("Name", nameIn), h("div", { class: "narrow" }, field("Count", countIn))),
        countHint,
        h("div", { class: "row" }, field("CPU cores", cores), field("CPU type", cpuType), field("RAM (MB)", mem)),
        h("div", { class: "row" }, field("Datastore", dsSel), field("Disk (GB)", diskIn, "At least the size of the image")),
        h("div", {}, h("label", {}, "Extra disks"), h("div", { class: "hint", style: "margin-top: 0" }, "Empty data disks, e.g. /var for FortiManager"), extraBox,
          h("button", { class: "link", onclick: () => { st.extra.push({ size: 100, datastore: "" }); renderExtra(); } }, ico("plus"), "Add disk")),
        h("div", {}, h("label", {}, "Network"), nicBox,
          h("button", { class: "link", onclick: () => { st.nics.push({ bridge: shared.bridges[0] || "", vlan: "" }); renderNics(); } }, ico("plus"), "Add interface")),
        d0Details,
        h("details", {}, h("summary", {}, "Advanced"),
          h("div", { class: "row" }, field("Machine", machine), field("BIOS", bios), field("Disk controller", scsihw)),
          h("div", { class: "row" }, field("NIC-model", nicModel), field("VMID", vmidIn, "Empty = next free; only with count 1")),
          h("div", { class: "inline" }, startAfter, h("label", {}, "Start VM after deploy"))))));

  function tokens() { return tokIn.value.split(/\s+/).filter(Boolean); }

  function syncD0() {
    staticBox.hidden = netMode.value !== "static";
    const n = tokens().length;
    tokHint.textContent = n ? `${n} token${n === 1 ? "" : "s"} for ${count()} VM${count() === 1 ? "" : "s"}${n !== count() ? " — these must match" : ""}` : "";
    onChange();
  }

  function d0Spec() {
    return {
      product: prodSel.value, hostname: hostIn.value.trim() || null, admin_password: pwIn.value || null,
      network: { mode: netMode.value, interface: ifIn.value.trim() || "port1", ip: ipIn.value.trim() || null, gateway: gwIn.value.trim() || null, dns: [dns1.value.trim(), dns2.value.trim()].filter(Boolean) },
      custom_cli: cliIn.value, tokens: tokens(),
    };
  }

  async function showPreview() {
    previewErr.hidden = true; previewOut.hidden = true;
    try {
      const r = await api("POST", "/cloudinit/preview", { day0: d0Spec(), name: count() > 1 ? `${nameIn.value.trim() || "vm"}-1` : nameIn.value.trim() || "vm", n: 1, count: count() });
      previewOut.textContent = r.user_data || "(empty)"; previewOut.hidden = false;
    } catch (e) { previewErr.textContent = e.message; previewErr.hidden = false; }
  }

  function updateHead() { title.textContent = nameIn.value.trim(); }

  function count() { return Math.max(1, Math.min(20, Number(countIn.value) || 1)); }

  function updateCountHint() {
    const n = count(); const name = nameIn.value.trim() || "name";
    countHint.textContent = n > 1 ? `Will be deployed as ${name}-1 to ${name}-${n}` : "";
    vmidIn.disabled = n > 1; if (n > 1) vmidIn.value = "";
    if (typeof tokHint !== "undefined") syncD0();
  }

  function renderNics() {
    nicBox.replaceChildren(...st.nics.map((n, i) => {
      const br = select(shared.bridges.length ? shared.bridges : [n.bridge || ""], n.bridge, { onchange: () => { n.bridge = br.value; } });
      const vl = h("input", { class: "vlan", type: "number", min: 1, max: 4094, placeholder: "VLAN", value: n.vlan ?? "", oninput: () => { n.vlan = vl.value; } });
      return h("div", { class: "nicrow" }, h("span", { class: "nlabel" }, "net" + i), br, vl,
        st.nics.length > 1 ? h("button", { class: "icon", "aria-label": "Remove interface", onclick: () => { st.nics.splice(i, 1); renderNics(); onChange(); } }, ico("x")) : null);
    }));
    onChange();
  }

  function renderExtra() {
    extraBox.replaceChildren(...st.extra.map((d, i) => {
      const size = h("input", { type: "number", min: 1, value: d.size, oninput: () => { d.size = size.value; } });
      const ds = select([["", "same datastore as boot disk"], ...shared.storages.map((x) => x.name)], d.datastore, { onchange: () => { d.datastore = ds.value; } });
      return h("div", { class: "nicrow" }, h("span", { class: "nlabel" }, "scsi" + (i + 1)), size, h("span", { class: "muted" }, "GB"), ds,
        h("button", { class: "icon", "aria-label": "Remove disk", onclick: () => { st.extra.splice(i, 1); renderExtra(); } }, ico("x")));
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
    dzText.textContent = file.name; dzSub.textContent = "Uploading…";
    onChange();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { setBar(bar.firstChild, e.loaded / e.total); dzSub.textContent = `Uploading… ${Math.round(100 * e.loaded / e.total)}%`; } };
    xhr.onload = () => {
      st.uploading = false; bar.hidden = true;
      let body = null; try { body = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status !== 200) { dzText.textContent = "Drop a .qcow2 or .zip here or click to choose"; dzSub.textContent = ""; showErr(errText(body, xhr.status)); onChange(); return; }
      st.upload = body; dz.classList.add("has");
      dzSub.textContent = `${fmtBytes(file.size)} · ${body.candidates.length} image(s) found`;
      candSel.replaceChildren(...body.candidates.map((c) => h("option", { value: c.name }, `${c.name} (${c.virtual_gb} GB)`)));
      candWrap.hidden = body.candidates.length < 2;
      if (!nameIn.value) nameIn.value = body.candidates[0].name.replace(/\.qcow2$/i, "").replace(/[^A-Za-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
      updateHead(); updateCountHint();
      pickCandidate(body.candidates[0].name);
      const needsDataDisk = /fmg|fortimanager|faz|fortianalyzer/i.test(file.name + " " + body.candidates[0].name);
      extraHint.hidden = !needsDataDisk;
      const fname = (file.name + " " + body.candidates[0].name).toLowerCase();
      if (/fmg|fortimanager/.test(fname)) prodSel.value = "fortimanager"; else if (/faz|fortianalyzer/.test(fname)) prodSel.value = "fortianalyzer";
      if (prodSel.value !== "fortigate" && netMode.value === "dhcp") netMode.value = "static";
      syncD0();
      if (needsDataDisk && !st.extra.length) { st.extra.push({ size: 100, datastore: "" }); renderExtra(); }
    };
    xhr.onerror = () => { st.uploading = false; bar.hidden = true; showErr("Upload failed: no connection to the app"); onChange(); };
    const fd = new FormData(); fd.append("file", file); xhr.send(fd);
  }

  const api_ = {
    el,
    count,
    label() { return nameIn.value.trim() || "(unnamed)"; },
    dispose() { if (st.upload) api("DELETE", `/uploads/${st.upload.upload_id}`).catch(() => {}); },
    setIndex(n, removable) { num.textContent = "VM " + n; removeBtn.hidden = !removable; },
    ready() { return !!(st.upload && st.candidate && nameIn.value.trim() && dsSel.value && st.nics.length && !st.uploading && (!d0On.checked || isoSel.value)); },
    refreshShared() {
      const lu = shared.lastUsed || {};
      const want = dsSel.value || lu.datastore;
      dsSel.replaceChildren(...shared.storages.map((x) => h("option", { value: x.name }, x.name)));
      if (want && shared.storages.some((x) => x.name === want)) dsSel.value = want;
      if (!st.nics.length) st.nics.push({ bridge: lu.bridge || shared.bridges[0] || "", vlan: lu.vlan ?? "" });
      st.nics.forEach((n) => { if (!shared.bridges.includes(n.bridge)) n.bridge = lu.bridge && shared.bridges.includes(lu.bridge) ? lu.bridge : shared.bridges[0] || ""; });
      isoSel.replaceChildren(...shared.isoStorages.map((x) => h("option", { value: x }, x)));
      isoWarn.hidden = shared.isoStorages.length > 0;
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
          start_after: startAfter.checked, iso_storage: d0On.checked ? isoSel.value || null : null,
        },
        day0: d0On.checked ? d0Spec() : null,
      };
    },
  };
  api_.refreshShared();
  return api_;
}

// ---- Progress -------------------------------------------------------------------
const STEPS = [
  ["ontvangen", "File received"], ["uitgepakt", "Image ready"], ["uploaden", "Upload to Proxmox"], ["cloudinit", "Day-0 configuration"],
  ["vm_aanmaken", "Create VM"], ["disk_importeren", "Import disk"], ["resize", "Resize disk"],
  ["netwerk_boot", "Configure network and boot"], ["klaar", "Done"],
];
function stateIcon(state) {
  if (state === "done") return ico("check");
  if (state === "failed") return ico("x");
  return h("span", { class: "dot " + state });
}

function viewJob(id) {
  const stepState = {}; const stepMsg = {}; let progress = null; let speed = "";
  const log = []; let job = null; let es = null; let stopped = false;
  const title = h("h1", {}, "Deployment");
  const banner = h("div");
  const list = h("ul", { class: "steps" });
  const barFill = h("div"); const bar = h("div", { class: "bar", hidden: true }, barFill);
  const logPre = h("pre", { class: "log" });
  view.append(title, banner, h("div", { class: "card" }, list, bar),
    h("div", { class: "card" }, h("details", {}, h("summary", {}, "Log"), logPre)),
    h("a", { href: "#/deploy" }, ico("arrow-left"), "New deployment"));

  function apply(e) {
    stepState[e.step] = e.state;
    if (e.message) stepMsg[e.step] = e.message;
    if (e.step === "uploaden" && e.progress != null) { progress = e.progress; speed = e.message; }
    log.push(`${new Date(e.ts * 1000).toLocaleTimeString("en-GB")}  [${e.step}] ${e.state}  ${e.message || ""}`);
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
      title.textContent = `Deployment: ${job.vm_name}${job.server_name ? " on " + job.server_name : ""}`;
      banner.className = job.status === "done" ? "ok" : job.status === "failed" ? "error" : "";
      banner.textContent = job.status === "done" ? `VM ${job.vmid} has been created.` : job.status === "failed" ? `Failed: ${job.error || "unknown error"}` : "";
    }
  }

  const seen = new Set();
  function applyOnce(e) { if (!seen.has(e.id)) { seen.add(e.id); apply(e); } }

  async function refreshJob() { job = await api("GET", `/deployments/${id}`); return job; }

  function stream() {
    if (stopped) return;
    es = new EventSource(`/api/deployments/${id}/events`);
    es.addEventListener("step", (m) => { applyOnce(JSON.parse(m.data)); render(); });
    // The server closes the stream when the deployment finishes; onerror follows. Only reconnect while the job is still running.
    es.onerror = async () => {
      es.close();
      try { const j = await refreshJob(); j.events.forEach(applyOnce); } catch (_) { /* retry later */ }
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

// ---- Batch progress ---------------------------------------------------------------
function viewBatch(id) {
  const title = h("h1", {}, "Deployment");
  const banner = h("div");
  const list = h("div", { class: "card batch" });
  view.append(title, banner, list, h("a", { href: "#/deploy" }, ico("arrow-left"), "New deployment"));
  const LABEL = Object.fromEntries(STEPS);
  let timer = null; let stopped = false;

  function render(b) {
    const n = b.jobs.length;
    const done = b.jobs.filter((j) => j.status === "done").length;
    const failed = b.jobs.filter((j) => j.status === "failed").length;
    const running = n - done - failed;
    title.textContent = `Deployment of ${n} VM${n === 1 ? "" : "s"}${b.server_name ? " on " + b.server_name : ""}`;
    banner.className = running ? "" : failed ? "error" : "ok";
    banner.textContent = running ? `${done} of ${n} done${failed ? `, ${failed} failed` : ""}` : failed ? `${failed} of ${n} failed, ${done} done` : `All ${n} VMs have been created`;
    list.replaceChildren(...b.jobs.map((j) => {
      const open = () => { location.hash = `#/job/${j.id}`; };
      const up = j.state === "running" && j.step === "uploaden" && j.upload_progress != null;
      const fill = h("div"); setBar(fill, j.upload_progress || 0);
      const what = j.status === "failed" ? (j.error || "Failed") : j.status === "done" ? `Done${j.vmid ? " · VMID " + j.vmid : ""}` :
        `${LABEL[j.step] || "Waiting"}${up ? ` · ${Math.round(j.upload_progress * 100)}%` : ""}`;
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

// ---- History ----------------------------------------------------------------
function viewHistory() {
  const holder = h("div", { class: "card" }, "Loading…");
  view.append(h("h1", {}, "History"), holder);
  api("GET", "/deployments").then((jobs) => {
    if (!jobs.length) { holder.replaceChildren(h("span", { class: "muted" }, "No deployments yet")); return; }
    holder.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Time", "Name", "Server", "VMID", "Status"].map((t) => h("th", {}, t)))),
      h("tbody", {}, jobs.map((j) => {
        const open = () => { location.hash = `#/job/${j.id}`; };
        return h("tr", { class: "click", tabindex: 0, onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
          h("td", { class: "mono" }, fmtTime(j.created_at)), h("td", { class: "mono" }, j.vm_name), h("td", {}, j.server_name || "—"), h("td", { class: "mono" }, j.vmid ?? "—"),
          h("td", {}, h("span", { class: "status" }, h("span", { class: "dot " + j.status }), { done: "Done", failed: "Failed", running: "Running" }[j.status] || j.status)));
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
    if (!servers.length) { listBox.replaceChildren(h("span", { class: "muted" }, "No servers yet. Add one below.")); return; }
    listBox.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "URL", "Token ID", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, servers.map((s) => {
        const result = h("div", { class: "hint" });
        return h("tr", {}, h("td", {}, s.name), h("td", { class: "mono" }, s.url, s.verify_tls ? null : h("span", { class: "muted" }, " (TLS not verified)")), h("td", { class: "mono" }, s.token_id),
          h("td", { class: "actions-cell" },
            h("button", { class: "link", onclick: async () => {
              result.textContent = "Testing…"; result.className = "hint";
              try { const r = await api("POST", `/servers/${s.id}/test`); result.textContent = `Proxmox VE ${r.version}` + (r.warning ? ` — ${r.warning}` : ""); result.className = r.warning ? "warn" : "ok"; }
              catch (e) { result.textContent = e.message; result.className = "error"; }
            } }, "Test connection"),
            h("button", { class: "link", onclick: () => { editing = s; renderForm(); } }, "Edit"),
            h("button", { class: "link danger", onclick: async () => { if (confirm(`Delete server '${s.name}'?`)) { await api("DELETE", `/servers/${s.id}`); load(); } } }, "Delete"),
            result));
      }))));
  }

  function renderForm() {
    const s = editing;
    const name = h("input", { value: s ? s.name : "" });
    const url = h("input", { value: s ? s.url : "", placeholder: "https://pve1.lan:8006" });
    const tid = h("input", { value: s ? s.token_id : "", placeholder: "user@realm!tokenname" });
    const sec = h("input", { type: "password", placeholder: s ? "Leave empty to keep" : "", autocomplete: "off" });
    const tls = h("input", { type: "checkbox", id: "skiptls" }); tls.checked = s ? !s.verify_tls : false;
    const err = h("div", { class: "error", hidden: true });
    formBox.replaceChildren(h("h2", {}, s ? `Edit: ${s.name}` : "Add server"), err,
      h("div", { class: "row" }, field("Name", name), field("URL", url)),
      h("div", { class: "row" }, field("Token ID", tid), field("Token secret", sec)),
      h("div", { class: "inline" }, tls, h("label", { for: "skiptls" }, "Do not verify TLS certificate (self-signed certificate)")),
      h("div", { class: "actions" }, s ? h("button", { onclick: () => { editing = null; renderForm(); } }, "Cancel") : null,
        h("button", { class: "primary", onclick: async () => {
          const body = { name: name.value.trim(), url: url.value.trim(), token_id: tid.value.trim(), token_secret: sec.value || null, verify_tls: !tls.checked };
          try { await api(s ? "PUT" : "POST", s ? `/servers/${s.id}` : "/servers", body); editing = null; renderForm(); load(); }
          catch (e) { err.textContent = e.message; err.hidden = false; }
        } }, s ? "Save" : "Add")));
  }
  renderForm(); load().catch((e) => listBox.replaceChildren(h("div", { class: "error" }, e.message)));
}

route();
