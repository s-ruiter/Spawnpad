"""Test deploy/install.sh met nagebootste Proxmox-commando's (pct, pvesh, pveam, pvesm)."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"

STUBS = {
    "pct": 'echo "$*" >> "$STUB_LOG"\n'
           'case "$*" in *"hostname -I"*) echo "10.0.0.5";; esac\nexit 0\n',
    "pvesh": "echo 105\n",
    "pveam": 'case "$1" in\n'
             '  available) echo "system  debian-12-standard_12.7-1_amd64.tar.zst";;\n'
             '  list) echo "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst";;\n'
             'esac\nexit 0\n',
    "pvesm": 'echo "Name Type Status Total Used Available %"\n'
             'echo "local-lvm lvmthin active 1 1 1 1%"\n'
             'echo "local dir active 1 1 1 1%"\n',
}


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "pct.log"
    log.write_text("")

    def _run(answers=None, args=(), env=None):
        e = {
            **os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_LOG": str(log),
            "PROXLABER_ALLOW_NONROOT": "1", "TMPDIR": str(tmp_path),
        }
        if answers is not None:
            (tmp_path / "answers").write_text("\n".join(answers) + "\n")
            e["WIZARD_INPUT"] = str(tmp_path / "answers")
        e.update(env or {})
        r = subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        r.pct = log.read_text().splitlines()
        r.create = next((l for l in r.pct if l.startswith("create ")), None)
        return r

    return _run


# volgorde van de vragen: CT-ID, hostnaam, storage, bridge, VLAN, netwerk (dhcp/static),
# [IP, gateway, DNS], schijf GB, geheugen MB, cores, wachtwoord [, herhaal], bevestiging
def test_wizard_static_ip(run):
    r = run(["", "fw-deploy", "", "", "20", "static", "192.168.1.50/24", "192.168.1.1", "", "", "", "", "", "y"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert r.create and "--hostname fw-deploy" in r.create
    assert "--net0 name=eth0,bridge=vmbr0,tag=20,ip=192.168.1.50/24,gw=192.168.1.1" in r.create
    assert "--nameserver 192.168.1.1" in r.create
    assert "http://10.0.0.5:8080" in r.stdout


def test_wizard_dhcp_with_defaults(run):
    r = run(["", "", "", "", "", "", "", "", "", "", "y"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert "--net0 name=eth0,bridge=vmbr0,ip=dhcp" in r.create
    assert "--rootfs local-lvm:16" in r.create and "--memory 1024" in r.create and "--cores 2" in r.create
    assert "--nameserver" not in r.create and "--password" not in r.create


def test_wizard_rejects_invalid_ip_then_accepts(run):
    r = run(["", "", "", "", "", "static", "999.1.1.1/24", "10.0.0.5", "10.0.0.5/24", "10.0.0.1", "", "", "", "", "", "y"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert "ip=10.0.0.5/24,gw=10.0.0.1" in r.create
    assert "ip=999" not in r.create


def test_wizard_password_is_confirmed(run):
    r = run(["", "", "", "", "", "", "", "", "", "geheim", "anders", "geheim", "geheim", "y"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert "--password geheim" in r.create


def test_wizard_abort_creates_nothing(run):
    r = run(["", "", "", "", "", "", "", "", "", "", "n"])
    assert r.returncode != 0 and r.create is None


def test_wizard_rejects_unknown_storage(run):
    r = run(["", "", "doesnotexist", "local", "", "", "", "", "", "", "", "y"])
    assert r.returncode == 0, r.stderr + r.stdout
    assert "--rootfs local:16" in r.create


def test_yes_flag_skips_wizard_and_uses_env(run):
    r = run(args=["--yes"], env={"CT_IP": "10.1.1.5/24", "CT_GATEWAY": "10.1.1.1", "VLAN": "30", "DISK_GB": "24"})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "bridge=vmbr0,tag=30,ip=10.1.1.5/24,gw=10.1.1.1" in r.create
    assert "--rootfs local-lvm:24" in r.create


def test_yes_flag_rejects_invalid_static_ip(run):
    r = run(args=["--yes"], env={"CT_IP": "nonsense", "CT_GATEWAY": "10.1.1.1"})
    assert r.returncode != 0 and r.create is None
