# Proxlaber Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Een webapp (LXC op Proxmox) waarmee je een qcow2/zip uploadt, een Proxmox-server en VM-instellingen kiest, en met live voortgang een VM uitrolt via de Proxmox API.

**Architecture:** FastAPI-backend met SQLite. Upload is tweefasig: eerst `POST /api/uploads` (streamt naar schijf, pakt zip uit, leest qcow2-virtuele grootte), dan `POST /api/deployments` met de VM-config. Een deployment is een job met opgeslagen stappen en events; een worker-thread voert de stappen uit via een `ProxmoxClient` (httpx, API-token) en publiceert voortgang via SSE. Frontend is statische HTML/JS/CSS zonder build-stap.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx, sqlite3 (stdlib), python-multipart, pytest + httpx.MockTransport. Frontend: vanilla JS, één CSS-bestand.

**Spec:** `docs/superpowers/specs/2026-11-02-proxlaber-design.md`

## Global Constraints

- Alleen Proxmox VE 9+ via API-token; **geen SSH**. Oudere versies niet ondersteund.
- Alleen qcow2 (direct of in een zip). Geen vmdk/raw/ovf.
- CPU-type standaard `host`; VM-standaarden: SeaBIOS, virtio-scsi-single, virtio-netwerk, seriële console; "Geavanceerd" maakt machine, BIOS, NIC-model, controller aanpasbaar.
- Geen login. Geen auto-start van de VM (vinkje, standaard uit).
- Tokens nooit tonen of loggen. DB-bestand mode `0600`.
- Uploads streamen (nooit volledig in geheugen); tijdelijke bestanden altijd opruimen.
- Bij mislukking: halve VM en geüploade import-volume automatisch verwijderen; fout en logboek blijven in Geschiedenis.
- VMID: standaard `/cluster/nextid`, optioneel zelf; bezet = fout vóór upload naar Proxmox.
- Meerdere uitrollen tegelijk toegestaan; bij herstart worden lopende jobs `failed` en opgeruimd.
- UI-teksten Nederlands; licht thema, één accentkleur, indeling "één pagina" (links bestand+server, rechts VM+netwerk, knop onderaan).
- Per server: optie "TLS-certificaat niet controleren".

## Review Focus

- Zip met pad-traversal (`../evil.qcow2`) of corrupte zip: weigeren, niets buiten de tempdir schrijven.
- Bestand met extensie `.qcow2` maar zonder qcow2-magic (`QFI\xfb`): afwijzen bij upload.
- Opgegeven disk-grootte kleiner dan virtuele grootte: 422 vóór de job start.
- Proxmox onbereikbaar of token ongeldig midden in de upload: job `failed`, import-volume en halve VM opgeruimd, geen exception die de worker doodt.
- Twee uitrollen tegelijk met automatisch VMID: geen botsing (tweede krijgt eigen volgende vrije id of duidelijke fout).

---

## File Structure

```
pyproject.toml
app/config.py        # Settings: data_dir, temp_dir, db_path (env PROXLABER_DATA)
app/db.py            # connect(), init_schema(), tabellen servers/jobs/job_events/last_used
app/schemas.py       # Pydantic: ServerIn/ServerOut, VmConfig, NicConfig, UploadInfo, JobOut
app/proxmox.py       # ProxmoxClient
app/images.py        # receive_upload(), inspect_zip(), qcow2_virtual_size()
app/deploy.py        # run_deployment(): de stappen + cleanup
app/jobs.py          # JobManager: start, publish/subscribe, recover_on_startup
app/routes_servers.py
app/routes_deploy.py # uploads, deployments, SSE, history, lookups
app/main.py          # app factory, router wiring, static mount
app/static/{index.html,app.js,style.css}
deploy/install.sh    # draait op Proxmox-host: maakt LXC en installeert app
README.md
tests/...            # per task
```

### Task 1: Scaffold, config en database

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/config.py`, `app/db.py`, `tests/conftest.py`, `tests/test_db.py`, `.gitignore`

**Interfaces:**
- Produces: `Settings(data_dir: Path)` met `.db_path`, `.temp_dir`; `get_settings() -> Settings`; `db.connect(path) -> sqlite3.Connection` (row_factory=Row, WAL, foreign_keys on); `db.init_schema(conn)`. Tabellen: `servers(id INTEGER PK, name UNIQUE, url, token_id, token_secret, verify_tls INT)`, `jobs(id TEXT PK, server_id, node, vm_name, vmid INT NULL, config_json, status, error NULL, created_at, finished_at NULL)`, `job_events(id INTEGER PK, job_id, ts, step, state, message, progress REAL NULL)`, `last_used(server_id PK, datastore, bridge, vlan)`.
- conftest fixture `conn` = in-memory-achtige tmp-db met schema.

- [ ] **Step 1:** `git init`, `.gitignore` (`.venv`, `__pycache__`, `data/`, `.superpowers/`), `pyproject.toml` met bovenstaande dependencies en `[tool.pytest.ini_options] testpaths=["tests"]`.
- [ ] **Step 2:** Test `test_schema_creates_tables` in `tests/test_db.py`: na `init_schema` bevat `sqlite_master` de vier tabellen; `test_db_file_is_private`: na `connect(path)` heeft het bestand mode `0o600`.
- [ ] **Step 3:** Run `pytest tests/test_db.py -v` → FAIL (module ontbreekt).
- [ ] **Step 4:** Implementeer `config.py` en `db.py` met de signatures hierboven (chmod 600 bij aanmaken).
- [ ] **Step 5:** Run `pytest -v` → PASS. Commit `chore: scaffold, config en database-schema`.

### Task 2: Servers-API

**Files:**
- Create: `app/schemas.py` (ServerIn/ServerOut), `app/routes_servers.py`, `app/main.py` (app factory `create_app(settings) -> FastAPI`), `tests/test_servers.py`
- Modify: —

**Interfaces:**
- Consumes: `db.connect`, `Settings`.
- Produces: `GET/POST /api/servers`, `PUT/DELETE /api/servers/{id}`, `POST /api/servers/{id}/test`. `ServerOut` bevat `id, name, url, token_id, verify_tls` en **nooit** `token_secret`. Helper `get_server(conn, id) -> sqlite3.Row` voor latere tasks. `/test` gebruikt `ProxmoxClient.check()` uit Task 3 (in deze task: injecteerbaar via `app.state.client_factory`, standaard nog niet gekoppeld).

- [ ] **Step 1:** Tests: `test_create_and_list_hides_secret` (POST dan GET; response bevat geen `token_secret`, geen waarde van het secret in de body), `test_duplicate_name_409`, `test_update_without_secret_keeps_old_secret` (PUT zonder `token_secret` laat opgeslagen secret intact), `test_delete_server`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer routes met `TestClient`-vriendelijke app factory; `url` genormaliseerd zonder trailing slash.
- [ ] **Step 4:** Run → PASS. Commit `feat: servers-API`.

### Task 3: ProxmoxClient

**Files:**
- Create: `app/proxmox.py`, `tests/test_proxmox.py`

**Interfaces:**
- Produces: `class ProxmoxClient(url, token_id, token_secret, verify_tls, transport=None)` (transport voor tests; `Authorization: PVEAPIToken={token_id}={token_secret}`), `class ProxmoxError(Exception)` met `.status` en `.detail`. Methoden:
  - `check() -> dict` (`GET /version`; `ProxmoxError` als major < 9; `{"version": "9.0.3", "import_storages": [..]}` na `GET /storage?content=import`; leeg → warning-veld `warning="geen storage met content-type import"`)
  - `nodes() -> list[str]`; `storages(node) -> list[{"name","avail"}]` (content=images) en `import_storages(node) -> list[str]` (content=import); `bridges(node) -> list[str]` (`/network?type=any_bridge`); `next_vmid() -> int`; `vmid_free(vmid) -> bool`
  - `upload_import(node, storage, path: Path, filename: str, on_progress: Callable[[float,float],None]) -> str` (multipart `content=import`, gestreamd; callback(fractie, bytes/s); retourneert UPID; wacht via `wait_task`)
  - `create_vm(node, vmid, name, params: dict) -> str` (UPID), `resize_disk(node, vmid, disk, size_gb)`, `wait_task(node, upid, on_tick=None, timeout=3600)` (poll `/tasks/{upid}/status`, `ProxmoxError` bij exitstatus ≠ OK), `delete_vm(node, vmid)` (purge=1, destroy-unreferenced-disks=1), `delete_volume(node, storage, volid)`.
- Pure helper: `build_vm_params(cfg: VmConfig, import_volid: str) -> dict` met o.a. `cpu=cfg.cpu_type`, `cores`, `memory`, `bios`, `machine`, `scsihw`, `scsi0=f"{cfg.datastore}:0,import-from={import_volid}"`, `boot="order=scsi0"`, `serial0="socket"`, `vga="serial0"`, `net{i}=f"{model},bridge={bridge}[,tag={vlan}]"`, `start=0`. (`VmConfig` komt in `schemas.py`; definieer die in deze task: `name, node, datastore, import_storage, vmid|None, cores=2, memory_mb=2048, disk_gb, cpu_type="host", bios="seabios", machine="pc", scsihw="virtio-scsi-single", nics: list[NicConfig(bridge, vlan|None, model="virtio")], start_after=False`.)

- [ ] **Step 1:** Tests met `httpx.MockTransport`: `test_auth_header_uses_api_token`, `test_check_rejects_pve8` (versie `8.4.1` → ProxmoxError), `test_check_warns_without_import_storage`, `test_build_vm_params_fortigate_defaults` (assert op `cpu=="host"`, `bios=="seabios"`, `scsi0` bevat `import-from=local:import/x.qcow2`, `net0=="virtio,bridge=vmbr0,tag=10"`, `net1=="virtio,bridge=vmbr1"` zonder tag, `start==0`), `test_wait_task_raises_on_failed_exitstatus`, `test_upload_reports_progress` (callback minstens één keer met 0<fractie≤1).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implementeer. Upload: eigen multipart-generator over `httpx` streaming body zodat bestand in blokken van 1 MiB gaat en `on_progress` wordt aangeroepen. Foutafhandeling: `httpx.HTTPError` → `ProxmoxError`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Koppel `POST /api/servers/{id}/test` aan `ProxmoxClient.check()` (uit Task 2) en breid `tests/test_servers.py` uit met `test_server_test_endpoint_reports_version` via `client_factory`-override. Commit `feat: ProxmoxClient en server-test`.

### Task 4: Uploads en imagehandling

**Files:**
- Create: `app/images.py`, `tests/test_images.py`

**Interfaces:**
- Produces: `qcow2_virtual_size(path: Path) -> int` (leest magic `QFI\xfb` + big-endian uint64 op offset 24; `ValueError` bij ongeldige magic), `inspect_zip(path: Path, dest: Path) -> list[Path]` (extraheert alleen `*.qcow2`-leden naar `dest` met `Path(name).name` als doelnaam — nooit directory-structuur; `ValueError` bij corrupte zip of geen qcow2), `async receive_upload(upload: UploadFile, dest_dir: Path) -> Path` (schrijft in blokken van 1 MiB). `UploadInfo(upload_id: str, candidates: list[{"name","virtual_gb"}])` (virtual_gb = ceil).

- [ ] **Step 1:** Tests: `test_qcow2_virtual_size_reads_header` (synthetische header van 4 GiB), `test_rejects_non_qcow2_magic`, `test_zip_extracts_single_qcow2`, `test_zip_without_qcow2_raises`, `test_zip_slip_member_is_flattened` (lid `../../evil.qcow2` belandt als `dest/evil.qcow2`, niets erbuiten), `test_corrupt_zip_raises_valueerror`.
- [ ] **Step 2:** Run → FAIL. Implementeer. Run → PASS.
- [ ] **Step 3:** Voeg in `app/routes_deploy.py` toe: `POST /api/uploads` (maakt `temp_dir/{upload_id}/`, bij `.zip` → `inspect_zip`, bij `.qcow2` → valideer magic; bij `ValueError` → 422 en map verwijderd), `DELETE /api/uploads/{id}`, en een opruimer die uploadmappen ouder dan 24 uur verwijdert bij startup. Test: `test_upload_qcow2_returns_candidate`, `test_upload_bad_zip_422_and_dir_removed`.
- [ ] **Step 4:** Run `pytest -v` → PASS. Commit `feat: uploads, zip-uitpakken, qcow2-validatie`.

### Task 5: Deploy-runner

**Files:**
- Create: `app/deploy.py`, `tests/test_deploy.py`

**Interfaces:**
- Consumes: `ProxmoxClient`, `VmConfig`, db-tabellen.
- Produces: `STEPS = ["ontvangen","uitgepakt","uploaden","vm_aanmaken","disk_importeren","resize","netwerk_boot","klaar"]` en `run_deployment(job_id, client: ProxmoxClient, cfg: VmConfig, image: Path, emit: Callable[[str,str,str,float|None],None]) -> int` (retourneert vmid). `emit(step, state, message, progress)` met state `running|done|failed`. Stappen `ontvangen` en `uitgepakt` worden direct als `done` geëmit. Volgorde: bepaal vmid (`cfg.vmid` of `next_vmid`; bezet → fout) → `upload_import` → `create_vm` met `build_vm_params` (disk wordt hierbij geïmporteerd) → `wait_task` → `resize_disk(scsi0, cfg.disk_gb)` indien > virtuele grootte → optioneel start → `delete_volume` van import-bestand. Bij elke exception: `delete_vm` (als aangemaakt) en `delete_volume` (als geüpload), elk in eigen try/except, daarna `emit(..., "failed", ...)` en re-raise.

- [ ] **Step 1:** Tests met een `FakeClient` (opnemende test-double): `test_happy_path_order` (aanroepvolgorde upload → create → wait → resize → delete_volume, alle STEPS eindigen `done`), `test_vmid_taken_fails_before_upload` (geen `upload_import`-aanroep), `test_upload_failure_cleans_up_volume_and_no_vm_delete`, `test_create_failure_deletes_vm_and_volume`, `test_cleanup_error_does_not_mask_original_error`, `test_no_resize_when_disk_equals_virtual_size`.
- [ ] **Step 2:** Run → FAIL. Implementeer. Run → PASS. Commit `feat: deploy-runner met opruimen`.

### Task 6: JobManager, SSE en geschiedenis

**Files:**
- Create: `app/jobs.py`, `tests/test_jobs.py`
- Modify: `app/routes_deploy.py`, `app/main.py`

**Interfaces:**
- Consumes: `run_deployment`, `get_server`, `ProxmoxClient`.
- Produces: `JobManager(settings, client_factory)` met `start(server_id, cfg: VmConfig, upload_id, candidate) -> job_id` (valideert `cfg.disk_gb >= virtuele grootte` → `ValueError`; schrijft job + `last_used`; start `threading.Thread`), `events_since(job_id, after_id) -> list[Row]`, `recover_on_startup()` (jobs met status `running` → `failed`, error `"App herstart tijdens uitrol"`, tempmap en eventueel bekende halve VM/volume proberen op te ruimen via een nieuwe client; fouten daarbij negeren), en tijdelijke upload-map wordt na afloop (ook bij fout) verwijderd.
- Routes: `POST /api/deployments` (422 bij te kleine disk), `GET /api/deployments` (geschiedenis, nieuwste eerst), `GET /api/deployments/{id}`, `GET /api/deployments/{id}/events` (SSE; hervat met `Last-Event-ID`; sluit na `klaar`/`failed`), lookups `GET /api/servers/{id}/nodes`, `/nodes/{node}/storages`, `/nodes/{node}/bridges`, `/last-used`.

- [ ] **Step 1:** Tests: `test_start_rejects_disk_smaller_than_virtual` (422), `test_events_replayed_after_reconnect` (events na `Last-Event-ID` komen terug), `test_recover_marks_running_jobs_failed`, `test_two_parallel_deployments_get_distinct_vmids` (FakeClient die `next_vmid` laat staan tot `create_vm`; bij conflict opnieuw `next_vmid` proberen, max 3×), `test_last_used_is_saved_after_start`.
- [ ] **Step 2:** Run → FAIL. Implementeer; `recover_on_startup()` aanroepen in app-lifespan. Run → PASS.
- [ ] **Step 3:** Voeg in `deploy.py` de retry op VMID-conflict toe (alleen wanneer `cfg.vmid is None`). Run `pytest -v` → PASS. Commit `feat: jobs, SSE, geschiedenis, herstel na herstart`.

### Task 7: Frontend — Uitrollen-pagina

**Files:**
- Create: `app/static/index.html`, `app/static/app.js`, `app/static/style.css`
- Modify: `app/main.py` (mount `/` → static, `html=True`)

**Interfaces:**
- Consumes: alle `/api/*` routes uit Task 2–6.
- Produces: hash-routing `#/uitrollen`, `#/geschiedenis`, `#/servers`, `#/job/{id}`; navigatiebalk "Proxlaber | Uitrollen | Geschiedenis | Servers".

- [ ] **Step 1:** Bouw de Uitrollen-pagina volgens mockup A: twee kolommen. Links: dropzone (XHR met upload-voortgangsbalk; toont bestandsnaam + grootte; bij meerdere qcow2 een keuzelijst), serverkeuze, nodekeuze. Rechts: naam, cores, CPU-type (standaard `host`), RAM, datastore (lijst), disk (GB, `min`=virtuele grootte, standaard = virtuele grootte), netwerkregels (bridge-lijst + VLAN, "+ interface"), uitklapbaar "Geavanceerd" (machine, BIOS, NIC-model, controller, VMID, "VM starten na uitrollen" uit). Knop "Uitrollen" is uitgeschakeld tot bestand + server + naam gezet zijn. Voorinvullen uit `/last-used`. Bij succes → `#/job/{id}`. Foutmeldingen (422) inline bij het veld.
- [ ] **Step 2:** Stijl: licht, achtergrond `#f7f8fa`, kaarten wit met 1px rand en 8px radius, accent `#2563eb`, systeemfont, max-breedte 1100px, responsive (kolommen stapelen onder 800px).
- [ ] **Step 3:** Verifieer handmatig: start `uvicorn app.main:app` met een fake-Proxmox (zie Task 9 `scripts/fake_proxmox.py`) of tegen een testserver; controleer dat het formulier alle velden toont en een uitrol start. Gebruik de browser-pane en screenshot.
- [ ] **Step 4:** Commit `feat: uitrol-pagina`.

### Task 8: Frontend — Voortgang, Geschiedenis, Servers

**Files:**
- Modify: `app/static/app.js`, `app/static/style.css`

- [ ] **Step 1:** `#/job/{id}`: stappenlijst met ✓ ● ○ per stap uit `STEPS`, voortgangsbalk + percentage + snelheid bij `uploaden`, uitklapbaar logboek (alle events), en bij `failed` een rode foutregel met de melding. Gebruik `EventSource` met automatische herverbinding; pagina herladen toont direct de volledige stand (eerst `GET /api/deployments/{id}`).
- [ ] **Step 2:** `#/geschiedenis`: tabel (tijd, naam, server, VMID, status-badge), klik opent `#/job/{id}`.
- [ ] **Step 3:** `#/servers`: lijst + formulier (naam, URL, token-ID, token-secret, "TLS-certificaat niet controleren"), knop "Test verbinding" toont versie of foutmelding en de waarschuwing als er geen `import`-storage is. Secret-veld toont bij bewerken een placeholder, leeg laten behoudt het oude secret.
- [ ] **Step 4:** Verifieer handmatig in de browser-pane met de fake-Proxmox: volledige uitrol zichtbaar met bewegende voortgangsbalk; mislukte uitrol (fake geeft 500 bij create) toont rode fout en staat in Geschiedenis. Commit `feat: voortgang, geschiedenis, servers`.

### Task 9: Fake Proxmox, LXC-installatie en README

**Files:**
- Create: `scripts/fake_proxmox.py`, `deploy/install.sh`, `deploy/proxlaber.service`, `README.md`, `tests/test_e2e.py`

**Interfaces:**
- `scripts/fake_proxmox.py`: minimale FastAPI die de endpoints uit Task 3 nabootst (traag uploaden zodat voortgang zichtbaar is; env `FAKE_FAIL_AT=create` om fouten te simuleren).
- `deploy/install.sh`: draait op de Proxmox-host; parameters `CTID`, `STORAGE`, `BRIDGE`, `DISK_GB` (standaard 16, genoeg ruimte voor uploads/uitpakken); maakt Debian 12/13-LXC via `pveam`/`pct create`, installeert python3-venv, kopieert app naar `/opt/proxlaber`, maakt systemd-service (`PROXLABER_DATA=/var/lib/proxlaber`, poort 8080), start die.
- README: installatie, benodigde token-rechten (`Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`, `VM.Allocate`, `VM.Config.*`, `VM.Audit`, `Sys.Audit`, `SDN.Use`, `Pool.Allocate` — controleren tegen PVE 9 en corrigeren als de fake-vs-echte test iets anders vereist), hoe je `import` als content-type aanzet op een storage (Datacenter → Storage → Content).

- [ ] **Step 1:** E2E-test `test_full_deploy_against_fake_proxmox`: start fake op willekeurige poort in een thread, maak server via API, upload een kleine synthetische qcow2-zip, start uitrol, lees SSE tot `klaar`, assert job `done` en dat fake een VM met `cpu=host` en twee `net`-regels ontving. Tweede test `test_failed_create_cleans_up` met `FAKE_FAIL_AT=create`: fake registreert `delete_volume`-aanroep, job `failed`.
- [ ] **Step 2:** Run → FAIL; implementeer fake; run → PASS.
- [ ] **Step 3:** Schrijf `install.sh`, service-unit en README. Draai `bash -n deploy/install.sh` (syntaxcheck) en `shellcheck` indien beschikbaar.
- [ ] **Step 4:** Draai de volledige suite `pytest -v` → alles PASS. Commit `feat: fake Proxmox, installatie, README`.

> Let op voor de uitvoerder: dit plan is gevalideerd tegen de fake, niet tegen een echte PVE 9. Eerste echte uitrol door de gebruiker blijft de eindtest; de README-sectie token-rechten en de `import-from`-parameters in `build_vm_params` zijn de plekken waar een echte server kan afwijken.

---

## Self-review

- **Spec-dekking:** servers (T2/3/8), upload+zip (T4), VM-config incl. host-CPU, geheugen, disk, datastore, VLAN, meerdere NICs (T3 `VmConfig`, T7), voortgang live (T5/6/8), geschiedenis (T6/8), opruimen en herstart (T5/6), VMID (T5/6), live keuzelijsten en onthouden (T6/7), TLS-optie (T2/3/8), LXC-installatie en README (T9), geen SSH (T3).
- **Types:** `VmConfig`/`NicConfig` gedefinieerd in T3 en gebruikt in T5–7; `STEPS` in T5 gebruikt in T8; `get_server` uit T2 in T6.
- **Proportie:** signatures en testnamen, geen volledige implementaties.
