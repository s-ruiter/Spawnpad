# Proxlaber

Licht webprogramma om een **qcow2** of **zip** (bijvoorbeeld een Fortinet KVM-image) met één scherm als VM uit te rollen op een van meerdere Proxmox-servers, met live voortgang.

Vereist **Proxmox VE 9 of nieuwer**. De app praat uitsluitend via de Proxmox-API (API-tokens), er is geen SSH nodig.

## Installeren (LXC op een Proxmox-host)

Open de shell van de Proxmox-host en draai als root:

```bash
bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
```

Het script maakt een Debian 12 container, haalt de code uit de repo (`git clone`), installeert de app als systemd-service en toont daarna het adres (poort 8080). Staat de repo op privé, geef dan inloggegevens mee in `REPO_URL` (bijvoorbeeld `REPO_URL=http://sander:TOKEN@10.10.100.3:8010/sander/ProxLaber.git`) en download het script met een token of kopieer het project naar de host en draai `bash deploy/install.sh`; dan wordt de lokale code gebruikt.

Optioneel aan te passen via omgevingsvariabelen: `REPO_URL`, `BRANCH` (`main`), `CTID`, `HOSTNAME_CT`, `STORAGE` (`local-lvm`), `TEMPLATE_STORAGE` (`local`), `BRIDGE` (`vmbr0`), `VLAN`, `DISK_GB` (16), `MEMORY` (1024). Bijvoorbeeld:

```bash
BRIDGE=vmbr1 VLAN=20 STORAGE=ssd bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
```

De container heeft schijfruimte nodig voor tijdelijke uploads en het uitpakken van zips: reken bij een zip op minstens 3× de grootte van het bestand (upload, uitpakken en kopie), bij een losse qcow2 op 2× (`DISK_GB`). Verlaten uploads worden bij de eerstvolgende upload opgeruimd (na 6 uur). Bijwerken later (de container moet bij de git-server kunnen):

```bash
pct exec <CTID> -- bash -c 'cd /opt/proxlaber && runuser -u proxlaber -- git pull -q && .venv/bin/pip install -q . && systemctl restart proxlaber'
```

## Proxmox voorbereiden

1. **API-token per server.** Maak een gebruiker en token aan (Datacenter → Permissions → API Tokens) en voeg de server toe op de pagina *Servers* met token-ID (`user@realm!naam`) en secret.
2. **Rechten van het token.** Startpunt: de rollen `PVEVMAdmin` en `PVEDatastoreAdmin` op pad `/` (met *Propagate*). Minimaal nodig zijn onder meer `VM.Allocate`, `VM.Config.*`, `VM.Audit`, `Datastore.AllocateSpace`, `Datastore.AllocateTemplate`, `Datastore.Audit`, `Sys.Audit` en `SDN.Use` op de gebruikte bridges. Dit is niet getest tegen een echte PVE 9; geeft een stap een 403, dan toont de uitrol de Proxmox-melding en kun je het ontbrekende recht toevoegen.
3. **Storage met content-type `import`.** De app uploadt het image naar een storage die *Import* als content-type heeft (Datacenter → Storage → kies storage → Edit → Content → *Import* aanvinken, bijvoorbeeld op `local`). Zonder zo'n storage toont *Test verbinding* een waarschuwing.
4. Een zelfgetekend certificaat? Vink bij de server *TLS-certificaat niet controleren* aan.

## Gebruik

*Uitrollen*: sleep het bestand in het scherm, kies server en node, stel de VM in (CPU-type staat standaard op `host`) en klik *Uitrollen*. Onder *Geavanceerd* staan machine, BIOS, disk-controller, NIC-model, VMID en "VM starten na uitrollen". *Geschiedenis* toont eerdere uitrollen met logboek.

Standaard-VM-hardware: SeaBIOS, virtio-scsi-single, virtio-netwerk, seriële console. De disk wordt vergroot tot de opgegeven grootte (minimaal de virtuele grootte van het image).

Bij een mislukte uitrol worden de half aangemaakte VM en het geüploade import-bestand automatisch verwijderd; fout en logboek blijven bewaard. Wordt de container herstart tijdens een uitrol, dan wordt die als mislukt gemarkeerd en (voor zover mogelijk) opgeruimd.

## Ontwikkelen

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python scripts/fake_proxmox.py          # nagebootste Proxmox op :8006
.venv/bin/uvicorn --factory app.main:create_app    # de app op :8000
```

`FAKE_FAIL_AT=create` laat de nep-Proxmox bij het aanmaken falen, `FAKE_UPLOAD_DELAY=0.02` maakt uploaden traag zodat de voortgang zichtbaar is. Data staat in `./data` (of `PROXLABER_DATA`).

De app heeft geen inlogscherm; draai hem alleen op een vertrouwd netwerk.
