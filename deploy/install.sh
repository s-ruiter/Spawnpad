#!/usr/bin/env bash
# Maakt een LXC-container op deze Proxmox-host en installeert Proxlaber erin.
# Draai als root in de shell van de Proxmox-host, bijvoorbeeld:
#   bash -c "$(curl -fsSL http://10.10.100.3:8010/sander/ProxLaber/raw/branch/main/deploy/install.sh)"
# of vanuit een uitgepakte kopie van het project:  bash deploy/install.sh
#
# Instelbaar via omgevingsvariabelen:
#   REPO_URL  (git-adres, standaard jouw ProxLaber-repo)   BRANCH (standaard main)
#   CTID  HOSTNAME_CT  STORAGE  TEMPLATE_STORAGE  BRIDGE  VLAN  DISK_GB  MEMORY
set -euo pipefail

REPO_URL="${REPO_URL:-http://10.10.100.3:8010/sander/ProxLaber.git}"
BRANCH="${BRANCH:-main}"
CT_HOSTNAME="${HOSTNAME_CT:-proxlaber}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
DISK_GB="${DISK_GB:-16}"      # ruimte voor tijdelijke uploads en uitpakken
MEMORY="${MEMORY:-1024}"

command -v pct >/dev/null || { echo "pct niet gevonden: draai dit script op een Proxmox-host." >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Draai dit script als root." >&2; exit 1; }

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
NET="name=eth0,bridge=${BRIDGE},ip=dhcp"
[ -n "${VLAN:-}" ] && NET="${NET},tag=${VLAN}"

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
  --hostname "$CT_HOSTNAME" --memory "$MEMORY" --cores 2 \
  --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET" \
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
