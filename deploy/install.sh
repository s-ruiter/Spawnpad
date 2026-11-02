#!/usr/bin/env bash
# Maakt een LXC-container op deze Proxmox-host en installeert Proxlaber erin.
# Draai als root op de Proxmox-host, vanuit een uitgepakte kopie van dit project:
#   bash deploy/install.sh
# Instelbaar via omgevingsvariabelen: CTID, HOSTNAME, STORAGE, TEMPLATE_STORAGE, BRIDGE, VLAN, DISK_GB, MEMORY
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${HOSTNAME_CT:-proxlaber}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
DISK_GB="${DISK_GB:-16}"      # ruimte voor tijdelijke uploads en uitpakken
MEMORY="${MEMORY:-1024}"
NET="name=eth0,bridge=${BRIDGE},ip=dhcp"
[ -n "${VLAN:-}" ] && NET="${NET},tag=${VLAN}"

command -v pct >/dev/null || { echo "pct niet gevonden: draai dit script op een Proxmox-host." >&2; exit 1; }

echo "==> Debian 12 template zoeken"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -n1)"
[ -n "$TEMPLATE" ] || { echo "Geen debian-12 template gevonden." >&2; exit 1; }
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Container ${CTID} aanmaken"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CT_HOSTNAME" --memory "$MEMORY" --cores 2 \
  --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET" \
  --unprivileged 1 --features nesting=0 --onboot 1
pct start "$CTID"

echo "==> Wachten op netwerk"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> App kopiëren"
TARBALL="$(mktemp /tmp/proxlaber.XXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT
tar -C "$ROOT" --exclude='.venv' --exclude='data' --exclude='.dev' --exclude='.git' \
  --exclude='.superpowers' --exclude='docs' --exclude='tests' --exclude='__pycache__' \
  -czf "$TARBALL" app pyproject.toml
pct push "$CTID" "$TARBALL" /tmp/proxlaber.tar.gz
pct push "$CTID" "$ROOT/deploy/proxlaber.service" /etc/systemd/system/proxlaber.service

echo "==> Installeren"
pct exec "$CTID" -- bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
  id proxlaber >/dev/null 2>&1 || useradd --system --home /opt/proxlaber --shell /usr/sbin/nologin proxlaber
  mkdir -p /opt/proxlaber
  tar -xzf /tmp/proxlaber.tar.gz -C /opt/proxlaber
  rm -f /tmp/proxlaber.tar.gz
  python3 -m venv /opt/proxlaber/.venv
  /opt/proxlaber/.venv/bin/pip install -q /opt/proxlaber
  chown -R proxlaber:proxlaber /opt/proxlaber
  systemctl daemon-reload
  systemctl enable --now proxlaber
'

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
echo "Klaar. Proxlaber draait op http://${IP}:8080"
