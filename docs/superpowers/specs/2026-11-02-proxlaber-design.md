# Proxlaber — ontwerp

## Doel en reden

Proxlaber is een kleine webapp om qcow2- of zip-bestanden (vooral Fortinet-VMs, bv. FortiGate KVM: een zip met `fortios.qcow2`) met één handeling als VM uit te rollen op een van meerdere Proxmox-servers. De gebruiker vindt de handmatige route (bestand naar de host, lege VM maken, disk importeren, boot en netwerk instellen, zip uitpakken) te omslachtig en doet dit vaak.

De app draait als LXC-container op een Proxmox-host.

## Doelen

- Een bestand uploaden (.qcow2 of .zip) en daarmee in één scherm een volledig geconfigureerde VM op een gekozen Proxmox-server krijgen.
- Meerdere Proxmox-servers beheren en per uitrol kiezen (server en node).
- VM configureren vanuit de app: naam, CPU (cores, CPU-type; standaard `host`), geheugen, disk-grootte, datastore, netwerk (aantal interfaces; per interface bridge en VLAN-tag).
- Live voortgang zien tijdens de uitrol, zonder pagina te verversen.
- Licht, rustig, overzichtelijk uiterlijk.

## Niet-doelen

- Geen volledig Proxmox-beheer (geen VM-lijst, console, snapshots, backups).
- Geen gebruikersbeheer of multi-tenancy.
- Geen LXC-containers of ISO's uitrollen; alleen VM's uit disk-images.

## Anti-doelen (mislukking, ook als het technisch werkt)

- Een uitrol die stilletjes half blijft hangen zonder dat zichtbaar is wat er gebeurd is.
- Een formulier dat meer klikken kost dan de handmatige route.
- Achtergebleven rommel (tijdelijke bestanden in de LXC of op Proxmox, half-aangemaakte VM's) na een mislukte uitrol.

## Beslissingen van de gebruiker

- **Hoofdscherm: één pagina** (optie A uit de mockups), geen wizard. Links: bestand-dropzone en serverkeuze (server, node). Rechts: VM-instellingen (naam, CPU-cores, CPU-type, RAM, datastore, disk-grootte) en netwerk (lijst interfaces met bridge + VLAN, knop "+ interface"). Onderaan één knop "Uitrollen".
- CPU-type staat standaard op `host`; alle andere instellingen kunnen per uitrol verschillen.
- Voortgangsscherm: stappenlijst met vinkjes, voortgangsbalk bij het uploaden (percentage, snelheid), uitklapbaar logboek. Stappen: bestand ontvangen, zip uitgepakt, uploaden naar Proxmox, VM aanmaken, disk importeren, netwerk en boot instellen, klaar.
- Navigatie: Uitrollen, Geschiedenis, Servers.
- Technische keuzes zijn aan de bouwer overgelaten (zie hieronder); de gebruiker heeft "naar eigen inzicht" gezegd.

## Aannames (voorstel van de ontwerper, door de gebruiker bevestigd als "klopt")

- Pagina "Servers": naam, adres, API-token per Proxmox-server.
- Pagina "Geschiedenis": eerdere uitrollen met uitkomst en logboek.
- Geen login; de app draait binnen het eigen netwerk.
- De VM wordt na uitrollen niet automatisch gestart (vinkje, standaard uit).
- Licht uiterlijk, veel witruimte, één accentkleur.

## Keuzes van de ontwerper (niet door de gebruiker besloten)

- Backend: Python met FastAPI; lichte webinterface (geen zwaar frontend-framework nodig); alles in één LXC.
- Communicatie met Proxmox via de API met een API-token per server.
- Live voortgang via server-sent events of WebSocket.
- **Besloten door de gebruiker:** alle servers draaien Proxmox VE 9 of nieuwer en de app krijgt API-gebruikers (API-tokens). Er is dus **geen SSH** nodig: het bestand wordt via de API geüpload naar een storage met content-type `import`, waarna de VM wordt aangemaakt met de disk geïmporteerd uit dat bestand. Oudere Proxmox-versies zijn niet ondersteund. Een server waarvan geen storage `import` toestaat, krijgt een duidelijke melding op de Servers-pagina (en in de README hoe dat aan te zetten).

- **Extra disks (toegevoegd na eerste gebruik, 2026-11-02):** naast de boot-disk kunnen één of meer lege data-disks worden aangemaakt (grootte in GB, optioneel andere datastore), als `scsi1`, `scsi2`, ... Reden: FortiManager en FortiAnalyzer hebben een tweede disk nodig voor `/var`; zonder die disk start de VM met "/var not mounted". De UI voegt automatisch een extra disk van 100 GB toe met een melding als de bestandsnaam op fmg/fortimanager/faz/fortianalyzer lijkt.

## Aanvullende keuzes van de ontwerper (na review, niet door de gebruiker besloten)

- **VM-hardware:** standaarden geschikt voor FortiGate KVM (SeaBIOS, virtio-netwerk, virtio-scsi, seriële console), CPU-type `host`. Deze zijn per uitrol aanpasbaar in een uitklapbaar blok "Geavanceerd" (machine, BIOS, NIC-model, controller). Alleen qcow2 wordt ondersteund; andere vendors kunnen dan werken als ze dezelfde hardware accepteren.
- **Zip-inhoud:** staat er één qcow2 in, dan wordt die gebruikt. Zijn het er meer, dan kiest de gebruiker. Staat er geen in, dan een duidelijke foutmelding. Overige bestanden worden genegeerd.
- **Disk-grootte:** de geïmporteerde disk wordt vergroot tot de opgegeven grootte. De virtuele grootte van het image is de ondergrens en het standaardveld; een kleinere waarde wordt geweigerd.
- **Jobs:** een uitrol is een job met opgeslagen status. De voortgangspagina kan na herladen of sluiten opnieuw aanhaken. Meerdere uitrollen tegelijk mogen. Bij herstart van de LXC worden lopende jobs als mislukt gemarkeerd en opgeruimd.
- **Bij mislukking:** een half aangemaakte VM en tijdelijke bestanden worden automatisch verwijderd; fout en logboek blijven in de Geschiedenis.
- **VMID:** standaard het volgende vrije nummer van de server; optioneel zelf in te vullen. Bezet nummer geeft een foutmelding vóór de upload begint.
- **Keuzelijsten:** datastores, bridges en nodes worden live bij de gekozen server opgehaald. De benodigde token-rechten worden in de README gedocumenteerd.
- **Onthouden:** per server worden de laatst gebruikte datastore, bridge en VLAN voorgesteld.
- **Beveiliging:** per server optie "TLS-certificaat niet controleren" (Proxmox gebruikt vaak een zelfgetekend certificaat). Tokens staan in de database met bestandsrechten alleen voor de appgebruiker; ze worden nooit getoond of gelogd.

## Randvoorwaarden

- Bestanden zijn typisch 100 MB tot enkele GB; upload moet streamen (niet volledig in geheugen) en tijdelijke bestanden na afloop opruimen.
- De LXC moet genoeg tijdelijke schijfruimte hebben voor uploaden en uitpakken.
- Moet installeerbaar zijn als LXC op Proxmox (bv. met een installatiescript of template); de bouwer bepaalt hoe.
- Tokens en SSH-gegevens worden niet in logs of de UI getoond.

## Aan de bouwer overgelaten

Frontend-aanpak, opslag voor servers en geschiedenis (bv. SQLite), foutafhandeling en opruimen bij mislukking, validatie van formulier, ondersteunde Proxmox-versies, installatiemethode voor de LXC, naamgeving en indeling van de code.
