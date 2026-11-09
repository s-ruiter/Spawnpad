#!/usr/bin/env bash
# Creates an LXC container on this Proxmox host and installs Spawnpad in it.
# Run as root in the Proxmox host shell, for example:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/s-ruiter/Spawnpad/main/deploy/install.sh)"
# or from an extracted copy of the project:  bash deploy/install.sh
#
# Starts a wizard that asks for the settings (network, storage, resources).
# Without questions:  install.sh --yes  (with environment variables, see install.sh --help)
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: install.sh [--yes]

Without options a wizard starts that asks for the LXC settings (network, storage, resources).
With --yes (or PROXLABER_YES=1) the wizard is skipped and environment variables and defaults apply:
  CTID  HOSTNAME_CT  STORAGE  TEMPLATE_STORAGE  BRIDGE  VLAN  DISK_GB  MEMORY  CORES
  CT_IP (dhcp or 192.168.1.50/24)  CT_GATEWAY  CT_DNS  CT_PASSWORD  REPO_URL  BRANCH
USAGE
}

ASSUME_YES="${PROXLABER_YES:-}"
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

REPO_URL="${REPO_URL:-https://github.com/s-ruiter/Spawnpad.git}"
BRANCH="${BRANCH:-main}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"

command -v pct >/dev/null || { echo "pct not found: run this script on a Proxmox host." >&2; exit 1; }
if [ "${PROXLABER_ALLOW_NONROOT:-}" != 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root." >&2; exit 1
fi

# ---- input -------------------------------------------------------------------------
# Answers come from the terminal, even when the script arrives through a pipe.
# WIZARD_INPUT (file) is only meant for the tests.
if [ -n "$ASSUME_YES" ]; then :
elif [ -n "${WIZARD_INPUT:-}" ]; then exec 3<"$WIZARD_INPUT"
elif [ -t 0 ]; then exec 3<&0
elif ( : </dev/tty ) 2>/dev/null; then exec 3</dev/tty
else echo "No terminal available for the wizard; use --yes with environment variables." >&2; exit 1; fi

# ask VARIABLE "Question" "default" [secret]
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

# defaults (environment variables are used as prefill)
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${HOSTNAME_CT:-proxlaber}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
VLAN="${VLAN:-}"
CT_IP="${CT_IP:-dhcp}"
CT_GATEWAY="${CT_GATEWAY:-}"
CT_DNS="${CT_DNS:-}"
DISK_GB="${DISK_GB:-16}"      # space for temporary uploads and extraction
MEMORY="${MEMORY:-1024}"
CORES="${CORES:-2}"
CT_PASSWORD="${CT_PASSWORD:-}"

if [ -z "$ASSUME_YES" ]; then
  echo "Installing Spawnpad as an LXC. Enter accepts the value in [brackets]."
  echo
  ask CTID "Container ID" "$CTID"
  ask CT_HOSTNAME "Hostname" "$CT_HOSTNAME"

  STORAGES=(); while IFS= read -r l; do [ -n "$l" ] && STORAGES+=("$l"); done < <(list_storages)
  if [ "${#STORAGES[@]}" -gt 0 ]; then
    in_list "$STORAGE" "${STORAGES[@]}" || STORAGE="${STORAGES[0]}"
    echo "Available storages: ${STORAGES[*]}"
    while :; do
      ask STORAGE "Storage for the container" "$STORAGE"
      in_list "$STORAGE" "${STORAGES[@]}" && break
      echo "  '$STORAGE' does not exist on this host." >&2
    done
  else
    ask STORAGE "Storage for the container" "$STORAGE"
  fi

  BRIDGES=(); while IFS= read -r l; do [ -n "$l" ] && BRIDGES+=("$l"); done < <(list_bridges)
  if [ "${#BRIDGES[@]}" -gt 0 ]; then
    in_list "$BRIDGE" "${BRIDGES[@]}" || BRIDGE="${BRIDGES[0]}"
    echo "Available bridges: ${BRIDGES[*]}"
  fi
  ask BRIDGE "Bridge" "$BRIDGE"
  while :; do
    ask VLAN "VLAN tag (empty = none)" "$VLAN"
    [ -z "$VLAN" ] || { [[ "$VLAN" =~ ^[0-9]+$ ]] && [ "$VLAN" -ge 1 ] && [ "$VLAN" -le 4094 ]; } && break
    echo "  A VLAN tag is a number between 1 and 4094." >&2
  done

  NETMODE="dhcp"; [ "$CT_IP" = "dhcp" ] || NETMODE="static"
  while :; do
    ask NETMODE "IP address: dhcp or static" "$NETMODE"
    case "$NETMODE" in dhcp|static) break ;; *) echo "  Choose dhcp or static." >&2 ;; esac
  done
  if [ "$NETMODE" = "static" ]; then
    [ "$CT_IP" = "dhcp" ] && CT_IP=""
    while :; do
      ask CT_IP "IP address with prefix (e.g. 192.168.1.50/24)" "$CT_IP"
      valid_cidr "$CT_IP" && break
      echo "  Invalid address; use the form 192.168.1.50/24." >&2
    done
    while :; do
      ask CT_GATEWAY "Gateway" "$CT_GATEWAY"
      valid_ipv4 "$CT_GATEWAY" && break
      echo "  Invalid IPv4 address." >&2
    done
    while :; do
      ask CT_DNS "DNS server" "${CT_DNS:-$CT_GATEWAY}"
      valid_ipv4 "$CT_DNS" && break
      echo "  Invalid IPv4 address." >&2
    done
  else
    CT_IP="dhcp"
  fi

  ask DISK_GB "Disk size in GB (space for uploads)" "$DISK_GB"
  ask MEMORY "Memory in MB" "$MEMORY"
  ask CORES "CPU cores" "$CORES"
  while :; do
    ask CT_PASSWORD "Root password for the container (empty = none, use 'pct enter')" "" secret
    [ -z "$CT_PASSWORD" ] && break
    ask PW2 "Repeat password" "" secret
    [ "$CT_PASSWORD" = "$PW2" ] && break
    echo "  Passwords do not match." >&2
  done

  echo
  echo "Summary"
  echo "  Container   : $CTID ($CT_HOSTNAME) on storage $STORAGE, ${DISK_GB} GB, ${MEMORY} MB, ${CORES} cores"
  echo "  Network     : bridge $BRIDGE${VLAN:+, VLAN $VLAN}, ${CT_IP}${CT_GATEWAY:+, gateway $CT_GATEWAY}${CT_DNS:+, DNS $CT_DNS}"
  echo "  Source      : ${REPO_URL} (${BRANCH})"
  ask CONFIRM "Continue? (y/n)" "y"
  case "$CONFIRM" in j|J|y|Y|ja|yes) ;; *) echo "Aborted." >&2; exit 1 ;; esac
else
  if [ "$CT_IP" != "dhcp" ]; then
    valid_cidr "$CT_IP" || { echo "CT_IP is invalid: $CT_IP (expected dhcp or 192.168.1.50/24)" >&2; exit 1; }
    valid_ipv4 "$CT_GATEWAY" || { echo "CT_GATEWAY is required and must be an IPv4 address with a static IP" >&2; exit 1; }
    CT_DNS="${CT_DNS:-$CT_GATEWAY}"
  fi
fi

NET="name=eth0,bridge=${BRIDGE}"
[ -n "$VLAN" ] && NET="${NET},tag=${VLAN}"
if [ "$CT_IP" = "dhcp" ]; then NET="${NET},ip=dhcp"; else NET="${NET},ip=${CT_IP},gw=${CT_GATEWAY}"; fi
EXTRA_ARGS=()
[ -n "$CT_DNS" ] && EXTRA_ARGS+=(--nameserver "$CT_DNS")
[ -n "$CT_PASSWORD" ] && EXTRA_ARGS+=(--password "$CT_PASSWORD")

# Is this script inside an extracted project? Then we copy that code; otherwise we clone REPO_URL in the container.
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [ -f "$CANDIDATE/pyproject.toml" ] && [ -d "$CANDIDATE/app" ] && SRC_DIR="$CANDIDATE"
fi

echo "==> Looking for Debian 12 template"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
[ -n "$TEMPLATE" ] || { echo "No debian-12 template found." >&2; exit 1; }
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Creating container ${CTID}"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" --memory "$MEMORY" --cores "$CORES" \
  --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
  --unprivileged 1 --onboot 1
pct start "$CTID"

echo "==> Waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Installing base packages"
pct exec "$CTID" -- bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null
  mkdir -p /opt/proxlaber
'

if [ -n "$SRC_DIR" ]; then
  echo "==> Copying app from ${SRC_DIR}"
  TARBALL="$(mktemp /tmp/proxlaber.XXXXXX.tar.gz)"
  trap 'rm -f "$TARBALL"' EXIT
  tar -C "$SRC_DIR" --exclude='.venv' --exclude='data' --exclude='.dev' --exclude='.git' \
    --exclude='.superpowers' --exclude='docs' --exclude='tests' --exclude='__pycache__' \
    -czf "$TARBALL" app deploy pyproject.toml
  pct push "$CTID" "$TARBALL" /tmp/proxlaber.tar.gz
  pct exec "$CTID" -- bash -euo pipefail -c 'tar -xzf /tmp/proxlaber.tar.gz -C /opt/proxlaber && rm -f /tmp/proxlaber.tar.gz'
else
  echo "==> Fetching app from ${REPO_URL} (${BRANCH})"
  pct exec "$CTID" -- git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" /opt/proxlaber
fi

echo "==> Installing and starting"
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
echo "Done. Spawnpad is running at http://${IP}:8080"
echo "Update later:  pct exec ${CTID} -- bash -c 'cd /opt/proxlaber && runuser -u proxlaber -- git pull -q && .venv/bin/pip install -q . && systemctl restart proxlaber'"
