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

function field(label, input, hint) {
  return h("div", {}, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);
}

// ---- router ------------------------------------------------------------------
const view = document.getElementById("view");
let cleanup = null;

function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || "#/uitrollen";
  const [, page, arg] = hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === (page === "job" ? "geschiedenis" : page)));
  view.replaceChildren();
  const views = { uitrollen: viewDeploy, geschiedenis: viewHistory, servers: viewServers, job: viewJob };
  (views[page] || viewDeploy)(arg);
}
window.addEventListener("hashchange", route);

// ---- Uitrollen -----------------------------------------------------------------
function viewDeploy() {
  const st = { upload: null, candidate: null, servers: [], serverId: null, node: null, storages: [], importStorages: [], bridges: [], nodes: [], nics: [], uploading: false };
  const root = h("div");
  view.append(h("h1", {}, "Uitrollen"), root);

  const errBox = h("div", { class: "error", hidden: true });
  const nameIn = h("input", { placeholder: "bijv. fgt-lab-01", oninput: refreshBtn });
  const cores = h("input", { type: "number", min: 1, value: 2 });
  const cpuType = h("input", { value: "host" });
  const mem = h("input", { type: "number", min: 256, step: 256, value: 2048 });
  const diskIn = h("input", { type: "number", min: 1, value: 2 });
  const dsSel = h("select", { onchange: refreshBtn });
  const nodeSel = h("select", { onchange: () => loadNode(nodeSel.value) });
  const serverSel = h("select", { onchange: () => loadServer(Number(serverSel.value)) });
  const candSel = h("select", { onchange: () => pickCandidate(candSel.value) });
  const candWrap = h("div", { hidden: true }, field("Welk image uit de zip?", candSel));
  const nicBox = h("div");
  const machine = h("input", { value: "pc" });
  const bios = select(["seabios", "ovmf"], "seabios");
  const scsihw = select(["virtio-scsi-single", "virtio-scsi-pci", "lsi"], "virtio-scsi-single");
  const nicModel = select(["virtio", "e1000", "vmxnet3"], "virtio");
  const vmidIn = h("input", { type: "number", min: 100, placeholder: "automatisch" });
  const startAfter = h("input", { type: "checkbox", id: "startAfter" });
  const importWarn = h("div", { class: "warn", hidden: true }, "Deze node heeft geen storage met content-type 'import'. Zet dat aan bij Datacenter → Storage.");
  const submit = h("button", { class: "primary", disabled: true, onclick: doDeploy }, "Uitrollen");

  const dzText = h("div", {}, "Sleep een .qcow2 of .zip hierheen of klik om te kiezen");
  const dzSub = h("small", {}, "");
  const bar = h("div", { class: "bar", hidden: true }, h("div"));
  const fileIn = h("input", { type: "file", accept: ".qcow2,.zip", hidden: true, onchange: () => fileIn.files[0] && sendFile(fileIn.files[0]) });
  const dz = h("div", { class: "dropzone", onclick: () => fileIn.click() }, dzText, dzSub, bar, fileIn);
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) sendFile(e.dataTransfer.files[0]); });

  root.append(errBox, h("div", { class: "grid" },
    h("div", {},
      h("div", { class: "card" }, h("h2", {}, "1 · Bestand"), dz, candWrap),
      h("div", { class: "card" }, h("h2", {}, "2 · Proxmox server"), field("Server", serverSel), field("Node", nodeSel))),
    h("div", {},
      h("div", { class: "card" }, h("h2", {}, "3 · VM"),
        field("Naam", nameIn),
        h("div", { class: "row" }, field("CPU cores", cores), field("CPU type", cpuType), field("RAM (MB)", mem)),
        h("div", { class: "row" }, field("Datastore", dsSel), field("Disk (GB)", diskIn, "Minimaal de grootte van het image")),
        importWarn,
        h("details", {}, h("summary", {}, "Geavanceerd"),
          h("div", { class: "row" }, field("Machine", machine), field("BIOS", bios), field("Disk-controller", scsihw)),
          h("div", { class: "row" }, field("NIC-model", nicModel), field("VMID", vmidIn, "Leeg = volgende vrije")),
          h("div", { class: "inline" }, startAfter, h("label", { for: "startAfter" }, "VM starten na uitrollen")))),
      h("div", { class: "card" }, h("h2", {}, "4 · Netwerk"), nicBox,
        h("button", { class: "link", onclick: () => { st.nics.push({ bridge: st.bridges[0] || "", vlan: "" }); renderNics(); } }, "+ interface")),
      h("div", { class: "actions" }, submit))));

  function showErr(msg) { errBox.textContent = msg || ""; errBox.hidden = !msg; if (msg) window.scrollTo({ top: 0, behavior: "smooth" }); }

  function refreshBtn() {
    submit.disabled = !(st.upload && st.candidate && st.serverId && st.node && nameIn.value.trim() && dsSel.value && st.importStorages.length && st.nics.length && !st.uploading);
  }

  function renderNics() {
    nicBox.replaceChildren(...st.nics.map((n, i) => {
      const br = select(st.bridges.length ? st.bridges : [n.bridge || ""], n.bridge, { onchange: () => { n.bridge = br.value; } });
      const vl = h("input", { class: "vlan", type: "number", min: 1, max: 4094, placeholder: "VLAN", value: n.vlan ?? "", oninput: () => { n.vlan = vl.value; } });
      return h("div", { class: "nicrow" }, h("span", { class: "nlabel" }, "net" + i), br, vl,
        st.nics.length > 1 ? h("button", { class: "link danger", onclick: () => { st.nics.splice(i, 1); renderNics(); refreshBtn(); } }, "✕") : null);
    }));
    refreshBtn();
  }

  function pickCandidate(name) {
    st.candidate = name;
    const c = st.upload.candidates.find((x) => x.name === name);
    diskIn.min = c.virtual_gb;
    diskIn.value = c.virtual_gb;
    refreshBtn();
  }

  function sendFile(file) {
    showErr("");
    if (st.upload) api("DELETE", `/uploads/${st.upload.upload_id}`).catch(() => {});
    st.upload = null; st.candidate = null; st.uploading = true; candWrap.hidden = true;
    dz.classList.remove("has"); bar.hidden = false; bar.firstChild.style.width = "0";
    dzText.textContent = file.name; dzSub.textContent = "Uploaden…";
    refreshBtn();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.firstChild.style.width = (100 * e.loaded / e.total) + "%"; dzSub.textContent = `Uploaden… ${Math.round(100 * e.loaded / e.total)}%`; } };
    xhr.onload = () => {
      st.uploading = false; bar.hidden = true;
      let body = null; try { body = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status !== 200) { dzText.textContent = "Sleep een .qcow2 of .zip hierheen of klik om te kiezen"; dzSub.textContent = ""; showErr(errText(body, xhr.status)); refreshBtn(); return; }
      st.upload = body; dz.classList.add("has");
      dzSub.textContent = `${fmtBytes(file.size)} · ${body.candidates.length} image(s) gevonden`;
      candSel.replaceChildren(...body.candidates.map((c) => h("option", { value: c.name }, `${c.name} (${c.virtual_gb} GB)`)));
      candWrap.hidden = body.candidates.length < 2;
      if (!nameIn.value) nameIn.value = body.candidates[0].name.replace(/\.qcow2$/i, "").replace(/[^A-Za-z0-9-]+/g, "-").toLowerCase();
      pickCandidate(body.candidates[0].name);
    };
    xhr.onerror = () => { st.uploading = false; bar.hidden = true; showErr("Upload mislukt: geen verbinding met de app"); refreshBtn(); };
    const fd = new FormData(); fd.append("file", file); xhr.send(fd);
  }

  async function loadServer(id) {
    st.serverId = id; st.node = null;
    nodeSel.replaceChildren(); dsSel.replaceChildren(); refreshBtn();
    if (!id) return;
    try {
      st.nodes = await api("GET", `/servers/${id}/nodes`);
      st.lastUsed = await api("GET", `/servers/${id}/last-used`);
      nodeSel.replaceChildren(...st.nodes.map((n) => h("option", { value: n }, n)));
      await loadNode(st.nodes[0]);
    } catch (e) { showErr(`Kon gegevens van de server niet ophalen: ${e.message}`); }
  }

  async function loadNode(node) {
    st.node = node; showErr("");
    try {
      const [s, b] = await Promise.all([api("GET", `/servers/${st.serverId}/nodes/${node}/storages`), api("GET", `/servers/${st.serverId}/nodes/${node}/bridges`)]);
      st.storages = s.storages; st.importStorages = s.import_storages; st.bridges = b;
      const lu = st.lastUsed || {};
      dsSel.replaceChildren(...st.storages.map((x) => h("option", { value: x.name }, x.name)));
      if (lu.datastore && st.storages.some((x) => x.name === lu.datastore)) dsSel.value = lu.datastore;
      importWarn.hidden = st.importStorages.length > 0;
      if (!st.nics.length) st.nics.push({ bridge: lu.bridge || b[0] || "", vlan: lu.vlan ?? "" });
      st.nics.forEach((n) => { if (!b.includes(n.bridge)) n.bridge = lu.bridge && b.includes(lu.bridge) ? lu.bridge : b[0] || ""; });
      renderNics();
    } catch (e) { showErr(`Kon gegevens van de node niet ophalen: ${e.message}`); }
  }

  async function doDeploy() {
    showErr(""); submit.disabled = true;
    const config = {
      name: nameIn.value.trim(), node: st.node, datastore: dsSel.value, import_storage: st.importStorages[0],
      disk_gb: Number(diskIn.value), vmid: vmidIn.value ? Number(vmidIn.value) : null,
      cores: Number(cores.value), memory_mb: Number(mem.value), cpu_type: cpuType.value.trim() || "host",
      bios: bios.value, machine: machine.value.trim() || "pc", scsihw: scsihw.value,
      nics: st.nics.map((n) => ({ bridge: n.bridge, vlan: n.vlan === "" || n.vlan == null ? null : Number(n.vlan), model: nicModel.value })),
      start_after: startAfter.checked,
    };
    try {
      const r = await api("POST", "/deployments", { server_id: st.serverId, upload_id: st.upload.upload_id, candidate: st.candidate, config });
      location.hash = `#/job/${r.id}`;
    } catch (e) { showErr(e.message); refreshBtn(); }
  }

  api("GET", "/servers").then((servers) => {
    st.servers = servers;
    if (!servers.length) {
      root.prepend(h("div", { class: "warn" }, "Er zijn nog geen Proxmox-servers. ", h("a", { href: "#/servers" }, "Voeg er eerst een toe.")));
      return;
    }
    serverSel.replaceChildren(...servers.map((s) => h("option", { value: s.id }, s.name)));
    loadServer(servers[0].id);
  }).catch((e) => showErr(e.message));
}

// ---- Voortgang -------------------------------------------------------------------
const STEPS = [
  ["ontvangen", "Bestand ontvangen"], ["uitgepakt", "Image klaar"], ["uploaden", "Uploaden naar Proxmox"],
  ["vm_aanmaken", "VM aanmaken"], ["disk_importeren", "Disk importeren"], ["resize", "Disk vergroten"],
  ["netwerk_boot", "Netwerk en boot instellen"], ["klaar", "Klaar"],
];
const ICON = { done: "✓", running: "●", failed: "✗", pending: "○" };

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
    h("a", { href: "#/uitrollen" }, "← Nieuwe uitrol"));

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
      return h("li", { class: state }, h("span", { class: "ic" }, ICON[state]), h("span", {}, label), msg && key !== "ontvangen" ? h("span", { class: "msg" }, msg) : null);
    }));
    const up = stepState.uploaden === "running" && progress != null;
    bar.hidden = !up; barFill.style.width = (progress || 0) * 100 + "%";
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

// ---- Geschiedenis ----------------------------------------------------------------
function viewHistory() {
  const holder = h("div", { class: "card" }, "Laden…");
  view.append(h("h1", {}, "Geschiedenis"), holder);
  api("GET", "/deployments").then((jobs) => {
    if (!jobs.length) { holder.replaceChildren(h("span", { class: "muted" }, "Nog geen uitrollen.")); return; }
    holder.replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Tijd", "Naam", "Server", "VMID", "Status"].map((t) => h("th", {}, t)))),
      h("tbody", {}, jobs.map((j) => h("tr", { class: "click", onclick: () => { location.hash = `#/job/${j.id}`; } },
        h("td", {}, fmtTime(j.created_at)), h("td", {}, j.vm_name), h("td", {}, j.server_name || "—"), h("td", {}, j.vmid ?? "—"),
        h("td", {}, h("span", { class: "badge " + j.status }, { done: "Klaar", failed: "Mislukt", running: "Bezig" }[j.status] || j.status)))))));
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
        return h("tr", {}, h("td", {}, s.name), h("td", {}, s.url, s.verify_tls ? null : h("span", { class: "muted" }, " (TLS niet gecontroleerd)")), h("td", {}, s.token_id),
          h("td", { style: "text-align:right;white-space:nowrap" },
            h("button", { class: "link", onclick: async () => {
              result.textContent = "Testen…"; result.className = "hint";
              try { const r = await api("POST", `/servers/${s.id}/test`); result.textContent = `Proxmox VE ${r.version}` + (r.warning ? ` — ${r.warning}` : ""); result.className = r.warning ? "warn" : "ok"; }
              catch (e) { result.textContent = e.message; result.className = "error"; }
            } }, "Test verbinding"), " ",
            h("button", { class: "link", onclick: () => { editing = s; renderForm(); } }, "Bewerken"), " ",
            h("button", { class: "link danger", onclick: async () => { if (confirm(`Server '${s.name}' verwijderen?`)) { await api("DELETE", `/servers/${s.id}`); load(); } } }, "Verwijderen"),
            result));
      }))));
  }

  function renderForm() {
    const s = editing;
    const name = h("input", { value: s ? s.name : "" });
    const url = h("input", { value: s ? s.url : "", placeholder: "https://pve1.lan:8006" });
    const tid = h("input", { value: s ? s.token_id : "", placeholder: "user@realm!tokenname" });
    const sec = h("input", { type: "password", placeholder: s ? "(ongewijzigd laten = oude behouden)" : "", autocomplete: "off" });
    const tls = h("input", { type: "checkbox", id: "skiptls" }); tls.checked = s ? !s.verify_tls : false;
    const err = h("div", { class: "error", hidden: true });
    formBox.replaceChildren(h("h2", {}, s ? `Bewerken: ${s.name}` : "Server toevoegen"), err,
      h("div", { class: "row" }, field("Naam", name), field("URL", url)),
      h("div", { class: "row" }, field("Token-ID", tid), field("Token-secret", sec)),
      h("div", { class: "inline" }, tls, h("label", { for: "skiptls" }, "TLS-certificaat niet controleren (zelfgetekend certificaat)")),
      h("div", { class: "actions" }, s ? h("button", { onclick: () => { editing = null; renderForm(); } }, "Annuleren") : null, " ",
        h("button", { class: "primary", onclick: async () => {
          const body = { name: name.value.trim(), url: url.value.trim(), token_id: tid.value.trim(), token_secret: sec.value || null, verify_tls: !tls.checked };
          try { await api(s ? "PUT" : "POST", s ? `/servers/${s.id}` : "/servers", body); editing = null; renderForm(); load(); }
          catch (e) { err.textContent = e.message; err.hidden = false; }
        } }, s ? "Opslaan" : "Toevoegen")));
  }
  renderForm(); load().catch((e) => listBox.replaceChildren(h("div", { class: "error" }, e.message)));
}

route();
