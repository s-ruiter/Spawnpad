# Proxlaber

Licht webprogramma om een **qcow2** of **zip** (bijvoorbeeld een Fortinet KVM-image) met één scherm als VM uit te rollen op een van meerdere Proxmox-servers, met live voortgang.

Vereist **Proxmox VE 9 of nieuwer**. De app praat uitsluitend via de Proxmox-API (API-tokens), er is geen SSH nodig.

## Installeren (LXC op een Proxmox-host)

Open de shell van de Proxmox-host en draai als root:

```bash
bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
```

Het script start een **wizard** die vraagt naar container-ID, hostnaam, storage, bridge, VLAN, netwerk (DHCP of een vast IP-adres met gateway en DNS), schijfgrootte, geheugen, cores en een optioneel root-wachtwoord. Enter neemt de standaardwaarde over; invoer wordt gecontroleerd en je krijgt een samenvatting om te bevestigen. Daarna maakt het een Debian 12 container, haalt de code uit de repo (`git clone`), installeert de app als systemd-service en toont het adres (poort 8080).

Zonder vragen (bijvoorbeeld voor automatisering) gebruik je `--yes`; dan gelden omgevingsvariabelen en standaardwaarden:

```bash
CT_IP=192.168.1.50/24 CT_GATEWAY=192.168.1.1 VLAN=20 STORAGE=ssd \
  bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)" -- --yes
```

Beschikbare variabelen: `CTID`, `HOSTNAME_CT`, `STORAGE`, `BRIDGE`, `VLAN`, `CT_IP` (`dhcp` of `192.168.1.50/24`), `CT_GATEWAY`, `CT_DNS`, `CT_PASSWORD`, `DISK_GB`, `MEMORY`, `CORES`, `TEMPLATE_STORAGE`, `REPO_URL`, `BRANCH` (`main`). Staat de repo op privé, geef dan inloggegevens mee in `REPO_URL` (bijvoorbeeld `http://sander:TOKEN@10.10.100.3:8010/sander/ProxLaber.git`), of kopieer het project naar de host en draai `bash deploy/install.sh`; dan wordt de lokale code gebruikt.

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

**Meerdere VM's tegelijk:** klik op *Nog een VM toevoegen* voor een ander bestand (bijvoorbeeld een FortiManager én FortiGates), en gebruik *Aantal* voor meerdere VM's van hetzelfde type (`fgt-1`, `fgt-2`, ...). Server en node gelden voor de hele batch. De voortgang van alle VM's staat op één pagina; een VM die mislukt hindert de andere niet. Een vast VMID kan alleen bij aantal 1. Alle VM's worden tegelijk geüpload, dus plan voldoende schijfruimte in de LXC en netwerkcapaciteit.

**Day-0 configuratie (cloud-init):** onder *Day-0 configuratie* per VM-blok geef je bij de eerste start een configuratie en FortiFlex-licentie mee, via een config-drive-ISO die de app maakt en als cd-rom aan de VM hangt. Je kunt hostname, admin-wachtwoord en netwerk laten genereren en/of eigen CLI meegeven (variabelen `{name}`, `{n}`, `{count}`, `{ip}`). Bij een vast IP en aantal > 1 telt het IP-adres per VM op. FortiFlex-tokens zet je één per regel (één per VM); je kiest of ze als MIME-bericht (FortiGate) of als regel `execute vm-license` onderaan de CLI worden meegegeven. *Voorbeeld bekijken* toont de uitkomst met afgeschermde geheimen. Je hebt een storage met content-type **ISO** nodig (`local` heeft dat standaard). Zet *VM starten na uitrollen* aan of start de VM zelf; de configuratie wordt bij de eerste start gelezen.

Controleer op de VM met `diagnose debug cloudinit show` of de config drive is gevonden. FortiManager en FortiAnalyzer: of deze indeling en het token-deel daar werken is niet bevestigd; probeer eerst één VM, en kies anders de CLI-variant voor het token. De ISO blijft aan de VM gekoppeld en blijft op de storage staan als je de VM later in Proxmox verwijdert.

**Extra disks:** FortiManager en FortiAnalyzer hebben naast de boot-disk een lege tweede disk nodig voor `/var` (data en logs). Voeg die toe onder *Extra disks*; bij bestandsnamen met fmg/faz doet het formulier dat zelf (100 GB, aan te passen). FortiGate heeft een extra disk alleen nodig voor lokale logs.

Standaard-VM-hardware: SeaBIOS en virtio-netwerk; beeldscherm en SCSI-controller blijven op de Proxmox-standaard (SCSI-controller te kiezen onder "Geavanceerd"). De disk wordt vergroot tot de opgegeven grootte (minimaal de virtuele grootte van het image).

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
