#!/usr/bin/env bash
# Maakt een LXC-container op deze Proxmox-host en installeert Proxlaber erin.
# Draai als root in de shell van de Proxmox-host, bijvoorbeeld:
#   bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
# of vanuit een uitgepakte kopie van het project:  bash deploy/install.sh
#
# Start een wizard die naar de instellingen vraagt (netwerk, opslag, resources).
# Zonder vragen:  install.sh --yes  (met omgevingsvariabelen, zie install.sh --help)
set -euo pipefail

usage() {
  cat <<'USAGE'
Gebruik: install.sh [--yes]

Zonder opties start een wizard die vraagt naar de instellingen van de LXC (netwerk, opslag, resources).
Met --yes (of PROXLABER_YES=1) wordt de wizard overgeslagen en gelden de omgevingsvariabelen en standaardwaarden:
  CTID  HOSTNAME_CT  STORAGE  TEMPLATE_STORAGE  BRIDGE  VLAN  DISK_GB  MEMORY  CORES
  CT_IP (dhcp of 192.168.1.50/24)  CT_GATEWAY  CT_DNS  CT_PASSWORD  REPO_URL  BRANCH
USAGE
}

ASSUME_YES="${PROXLABER_YES:-}"
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Onbekende optie: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

REPO_URL="${REPO_URL:-http://10.10.100.3:8010/sander/ProxLaber.git}"
BRANCH="${BRANCH:-main}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"

command -v pct >/dev/null || { echo "pct niet gevonden: draai dit script op een Proxmox-host." >&2; exit 1; }
if [ "${PROXLABER_ALLOW_NONROOT:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Draai dit script als root." >&2; exit 1
fi

# ---- invoer -------------------------------------------------------------------------
# Antwoorden komen van de terminal, ook als het script via een pipe binnenkomt.
# WIZARD_INPUT (bestand) is alleen bedoeld voor de tests.
if [ -n "$ASSUME_YES" ]; then :
elif [ -n "${WIZARD_INPUT:-}" ]; then exec 3<"$WIZARD_INPUT"
elif [ -t 0 ]; then exec 3<&0
elif ( : </dev/tty ) 2>/dev/null; then exec 3</dev/tty
else echo "Geen terminal beschikbaar voor de wizard; gebruik --yes met omgevingsvariabelen." >&2; exit 1; fi

# ask VARIABELE "Vraag" "standaard" [secret]
ask() {
  local var="$1" prompt="$2" default="$3" secret="${4:-}" reply=""
  if [ -n "$default" ]; then prompt="$prompt [$default]"; fi
  if [ -n "$secret" ] && [ -t 3 ]; then
    read -r -s -u 3 -p "$prompt: " reply || true; echo >&2
  else
    read -r -u 3 -p "$prompt: " reply || true
  fi
  printf -v "$var" '%s' "${reply:-$default}"
}

valid_ipv4() {
  local ip="$1" o
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -ra parts <<<"$ip"
  for o in "${parts[@]}"; do [ "$o" -le 255 ] || return 1; done
}

valid_cidr() {
  local ip="${1%/*}" prefix="${1#*/}"
  [[ "$1" == */* ]] && [[ "$prefix" =~ ^[0-9]{1,2}$ ]] && [ "$prefix" -le 32 ] && valid_ipv4 "$ip"
}

list_storages() { pvesm status --content rootdir 2>/dev/null | awk 'NR>1 {print $1}'; }

list_bridges() {
  local d
  for d in /sys/class/net/*/bridge; do [ -d "$d" ] && basename "$(dirname "$d")"; done 2>/dev/null || true
}

in_list() { local x="$1"; shift; local i; for i in "$@"; do [ "$i" = "$x" ] && return 0; done; return 1; }

# standaardwaarden (omgevingsvariabelen gelden als voorinvulling)
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${HOSTNAME_CT:-proxlaber}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
VLAN="${VLAN:-}"
CT_IP="${CT_IP:-dhcp}"
CT_GATEWAY="${CT_GATEWAY:-}"
CT_DNS="${CT_DNS:-}"
DISK_GB="${DISK_GB:-16}"      # ruimte voor tijdelijke uploads en uitpakken
MEMORY="${MEMORY:-1024}"
CORES="${CORES:-2}"
CT_PASSWORD="${CT_PASSWORD:-}"

if [ -z "$ASSUME_YES" ]; then
  echo "Proxlaber installeren als LXC. Enter neemt de waarde tussen [haakjes] over."
  echo
  ask CTID "Container-ID" "$CTID"
  ask CT_HOSTNAME "Hostnaam" "$CT_HOSTNAME"

  STORAGES=(); while IFS= read -r l; do [ -n "$l" ] && STORAGES+=("$l"); done < <(list_storages)
  if [ "${#STORAGES[@]}" -gt 0 ]; then
    in_list "$STORAGE" "${STORAGES[@]}" || STORAGE="${STORAGES[0]}"
    echo "Beschikbare storages: ${STORAGES[*]}"
    while :; do
      ask STORAGE "Storage voor de container" "$STORAGE"
      in_list "$STORAGE" "${STORAGES[@]}" && break
      echo "  '$STORAGE' bestaat niet op deze host." >&2
    done
  else
    ask STORAGE "Storage voor de container" "$STORAGE"
  fi

  BRIDGES=(); while IFS= read -r l; do [ -n "$l" ] && BRIDGES+=("$l"); done < <(list_bridges)
  if [ "${#BRIDGES[@]}" -gt 0 ]; then
    in_list "$BRIDGE" "${BRIDGES[@]}" || BRIDGE="${BRIDGES[0]}"
    echo "Beschikbare bridges: ${BRIDGES[*]}"
  fi
  ask BRIDGE "Bridge" "$BRIDGE"
  while :; do
    ask VLAN "VLAN-tag (leeg = geen)" "$VLAN"
    [ -z "$VLAN" ] || { [[ "$VLAN" =~ ^[0-9]+$ ]] && [ "$VLAN" -ge 1 ] && [ "$VLAN" -le 4094 ]; } && break
    echo "  Een VLAN-tag is een getal tussen 1 en 4094." >&2
  done

  NETMODE="dhcp"; [ "$CT_IP" = "dhcp" ] || NETMODE="static"
  while :; do
    ask NETMODE "IP-adres: dhcp of static" "$NETMODE"
    case "$NETMODE" in dhcp|static) break ;; *) echo "  Kies dhcp of static." >&2 ;; esac
  done
  if [ "$NETMODE" = "static" ]; then
    [ "$CT_IP" = "dhcp" ] && CT_IP=""
    while :; do
      ask CT_IP "IP-adres met prefix (bijv. 192.168.1.50/24)" "$CT_IP"
      valid_cidr "$CT_IP" && break
      echo "  Ongeldig adres; gebruik de vorm 192.168.1.50/24." >&2
    done
    while :; do
      ask CT_GATEWAY "Gateway" "$CT_GATEWAY"
      valid_ipv4 "$CT_GATEWAY" && break
      echo "  Ongeldig IPv4-adres." >&2
    done
    while :; do
      ask CT_DNS "DNS-server" "${CT_DNS:-$CT_GATEWAY}"
      valid_ipv4 "$CT_DNS" && break
      echo "  Ongeldig IPv4-adres." >&2
    done
  else
    CT_IP="dhcp"
  fi

  ask DISK_GB "Schijfgrootte in GB (ruimte voor uploads)" "$DISK_GB"
  ask MEMORY "Geheugen in MB" "$MEMORY"
  ask CORES "CPU-cores" "$CORES"
  while :; do
    ask CT_PASSWORD "Root-wachtwoord voor de container (leeg = geen, gebruik 'pct enter')" "" secret
    [ -z "$CT_PASSWORD" ] && break
    ask PW2 "Herhaal wachtwoord" "" secret
    [ "$CT_PASSWORD" = "$PW2" ] && break
    echo "  Wachtwoorden komen niet overeen." >&2
  done

  echo
  echo "Samenvatting"
  echo "  Container   : $CTID ($CT_HOSTNAME) op storage $STORAGE, ${DISK_GB} GB, ${MEMORY} MB, ${CORES} cores"
  echo "  Netwerk     : bridge $BRIDGE${VLAN:+, VLAN $VLAN}, ${CT_IP}${CT_GATEWAY:+, gateway $CT_GATEWAY}${CT_DNS:+, DNS $CT_DNS}"
  echo "  Bron        : ${REPO_URL} (${BRANCH})"
  ask CONFIRM "Doorgaan? (j/n)" "j"
  case "$CONFIRM" in j|J|y|Y|ja|yes) ;; *) echo "Afgebroken." >&2; exit 1 ;; esac
else
  if [ "$CT_IP" != "dhcp" ]; then
    valid_cidr "$CT_IP" || { echo "CT_IP is ongeldig: $CT_IP (verwacht dhcp of 192.168.1.50/24)" >&2; exit 1; }
    valid_ipv4 "$CT_GATEWAY" || { echo "CT_GATEWAY is verplicht en moet een IPv4-adres zijn bij een statisch IP" >&2; exit 1; }
    CT_DNS="${CT_DNS:-$CT_GATEWAY}"
  fi
fi

NET="name=eth0,bridge=${BRIDGE}"
[ -n "$VLAN" ] && NET="${NET},tag=${VLAN}"
if [ "$CT_IP" = "dhcp" ]; then NET="${NET},ip=dhcp"; else NET="${NET},ip=${CT_IP},gw=${CT_GATEWAY}"; fi
EXTRA_ARGS=()
[ -n "$CT_DNS" ] && EXTRA_ARGS+=(--nameserver "$CT_DNS")
[ -n "$CT_PASSWORD" ] && EXTRA_ARGS+=(--password "$CT_PASSWORD")

# Staat dit script in een uitgepakt project? Dan kopiëren we die code; anders clonen we REPO_URL in de container.
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [ -f "$CANDIDATE/pyproject.toml" ] && [ -d "$CANDIDATE/app" ] && SRC_DIR="$CANDIDATE"
fi

echo "==> Debian 12 template zoeken"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
[ -n "$TEMPLATE" ] || { echo "Geen debian-12 template gevonden." >&2; exit 1; }
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Container ${CTID} aanmaken"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" --memory "$MEMORY" --cores "$CORES" \
  --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
  --unprivileged 1 --onboot 1
pct start "$CTID"

echo "==> Wachten op netwerk"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Basispakketten installeren"
pct exec "$CTID" -- bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null
  mkdir -p /opt/proxlaber
'

if [ -n "$SRC_DIR" ]; then
  echo "==> App kopiëren uit ${SRC_DIR}"
  TARBALL="$(mktemp /tmp/proxlaber.XXXXXX.tar.gz)"
  trap 'rm -f "$TARBALL"' EXIT
  tar -C "$SRC_DIR" --exclude='.venv' --exclude='data' --exclude='.dev' --exclude='.git' \
    --exclude='.superpowers' --exclude='docs' --exclude='tests' --exclude='__pycache__' \
    -czf "$TARBALL" app deploy pyproject.toml
  pct push "$CTID" "$TARBALL" /tmp/proxlaber.tar.gz
  pct exec "$CTID" -- bash -euo pipefail -c 'tar -xzf /tmp/proxlaber.tar.gz -C /opt/proxlaber && rm -f /tmp/proxlaber.tar.gz'
else
  echo "==> App ophalen uit ${REPO_URL} (${BRANCH})"
  pct exec "$CTID" -- git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" /opt/proxlaber
fi

echo "==> Installeren en starten"
pct exec "$CTID" -- bash -euo pipefail -c '
  id proxlaber >/dev/null 2>&1 || useradd --system --home /opt/proxlaber --shell /usr/sbin/nologin proxlaber
  python3 -m venv /opt/proxlaber/.venv
  /opt/proxlaber/.venv/bin/pip install -q /opt/proxlaber
  install -m 644 /opt/proxlaber/deploy/proxlaber.service /etc/systemd/system/proxlaber.service
  chown -R proxlaber:proxlaber /opt/proxlaber
  systemctl daemon-reload
  systemctl enable --now proxlaber
'

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
echo "Klaar. Proxlaber draait op http://${IP}:8080"
echo "Bijwerken later:  pct exec ${CTID} -- bash -c 'cd /opt/proxlaber && runuser -u proxlaber -- git pull -q && .venv/bin/pip install -q . && systemctl restart proxlaber'"
