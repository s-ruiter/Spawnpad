# Proxlaber

A light web app to deploy a **qcow2** or **zip** (for example a Fortinet KVM image) as a VM on one of several Proxmox servers from a single screen, with live progress.

Requires **Proxmox VE 9 or newer**. The app talks to Proxmox exclusively through the API (API tokens); no SSH is needed.

## Install (LXC on a Proxmox host)

Open the Proxmox host shell and run as root:

```bash
bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
```

The script starts a **wizard** that asks for container ID, hostname, storage, bridge, VLAN, network (DHCP or a static IP address with gateway and DNS), disk size, memory, cores and an optional root password. Enter accepts the default; input is validated and you get a summary to confirm. It then creates a Debian 12 container, fetches the code from the repo (`git clone`), installs the app as a systemd service and shows the address (port 8080).

Without questions (for example for automation) use `--yes`; environment variables and defaults then apply:

```bash
CT_IP=192.168.1.50/24 CT_GATEWAY=192.168.1.1 VLAN=20 STORAGE=ssd \
  bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)" -- --yes
```

Available variables: `CTID`, `HOSTNAME_CT`, `STORAGE`, `BRIDGE`, `VLAN`, `CT_IP` (`dhcp` or `192.168.1.50/24`), `CT_GATEWAY`, `CT_DNS`, `CT_PASSWORD`, `DISK_GB`, `MEMORY`, `CORES`, `TEMPLATE_STORAGE`, `REPO_URL`, `BRANCH` (`main`). If the repo is private, put credentials in `REPO_URL` (for example `http://sander:TOKEN@10.10.100.3:8010/sander/ProxLaber.git`), or copy the project to the host and run `bash deploy/install.sh`; the local code is then used.

The container needs disk space for temporary uploads and for extracting zips: for a zip plan at least 3× the file size (upload, extraction and copy), for a plain qcow2 2× (`DISK_GB`). Abandoned uploads are cleaned up on the next upload (after 6 hours). Updating later (the container must be able to reach the git server):

```bash
pct exec <CTID> -- bash -c 'cd /opt/proxlaber && runuser -u proxlaber -- git pull -q && .venv/bin/pip install -q . && systemctl restart proxlaber'
```

## Preparing Proxmox

1. **API token per server.** Create a user and token (Datacenter → Permissions → API Tokens) and add the server on the *Servers* page with token ID (`user@realm!name`) and secret.
2. **Token permissions.** Starting point: the roles `PVEVMAdmin` and `PVEDatastoreAdmin` on path `/` (with *Propagate*). The minimum needed includes `VM.Allocate`, `VM.Config.*`, `VM.Audit`, `Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`, `Sys.Audit` and `SDN.Use` on the bridges used. This has not been tested against a real PVE 9; if a step returns 403, the deployment shows the Proxmox message and you can add the missing permission.
3. **Storage with content type `import`.** The app uploads the image to a storage that has *Import* as a content type (Datacenter → Storage → select storage → Edit → Content → tick *Import*, for example on `local`). Without such a storage, *Test connection* shows a warning.
4. A self-signed certificate? Tick *Do not verify TLS certificate* on the server.

## Usage

*Deploy*: drop the file onto the screen, choose server and node, configure the VM (CPU type defaults to `host`) and click *Deploy*. Under *Advanced* you find machine, BIOS, disk controller, NIC model, VMID and "Start VM after deploy". *History* shows earlier deployments with their log.

**Multiple VMs at once:** click *Add another VM* for a different file (for example a FortiManager and FortiGates), and use *Count* for multiple VMs of the same kind (`fgt-1`, `fgt-2`, ...). Server and node apply to the whole batch. The progress of all VMs is on one page; a VM that fails does not block the others. A fixed VMID is only possible with count 1. All VMs are uploaded at the same time, so plan enough disk space in the LXC and network capacity.

**Day-0 configuration (cloud-init):** under *Day-0 configuration* per VM block you provide a configuration and FortiFlex license at first boot, through a config-drive ISO that the app builds and attaches to the VM as a CD-ROM. You can have hostname, admin password and network generated and/or provide your own CLI (variables `{name}`, `{n}`, `{count}`, `{ip}`). With a static IP and count > 1 the IP address increments per VM. Enter FortiFlex tokens one per line (one per VM); you choose whether they are passed as a MIME message (FortiGate) or as an `execute vm-license` line at the end of the CLI. *Show preview* displays the result with secrets masked. You need a storage with content type **ISO** (`local` has it by default). Enable *Start VM after deploy* or start the VM yourself; the configuration is read at first boot.

On the VM, check with `diagnose debug cloudinit show` whether the config drive was found. FortiManager and FortiAnalyzer: whether this layout and the token part work there is not confirmed; try one VM first, and otherwise choose the CLI variant for the token. The ISO stays attached to the VM and stays on the storage if you later delete the VM in Proxmox.

**Extra disks:** FortiManager and FortiAnalyzer need an empty second disk besides the boot disk for `/var` (data and logs). Add it under *Extra disks*; for file names containing fmg/faz the form does this itself (100 GB, adjustable). FortiGate only needs an extra disk for local logs.

Default VM hardware: SeaBIOS and virtio network; display and SCSI controller stay on the Proxmox default (SCSI controller selectable under "Advanced"). The disk is grown to the given size (at least the virtual size of the image).

When a deployment fails, the half-created VM and the uploaded import file are removed automatically; the error and log are kept. If the container is restarted during a deployment, it is marked as failed and cleaned up as far as possible.

## Development

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python scripts/fake_proxmox.py          # fake Proxmox on :8006
.venv/bin/uvicorn --factory app.main:create_app    # the app on :8000
```

`FAKE_FAIL_AT=create` makes the fake Proxmox fail on create, `FAKE_UPLOAD_DELAY=0.02` slows uploads so progress is visible. Data lives in `./data` (or `PROXLABER_DATA`).

The app has no login screen; only run it on a trusted network.
