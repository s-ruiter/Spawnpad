"""Day-0 configuratie (cloud-init) voor Fortinet-VM's via een config-drive-ISO.

FortiOS zoekt bij de eerste start naar een config drive met `openstack/latest/user_data` (zie Fortinet KVM-gids,
"Cloud-init"). Met een FortiFlex-token wordt user_data een MIME-bericht met een `config`- en een `license`-deel.
"""
import io
import ipaddress
import re
from pathlib import Path

import pycdlib

from .schemas import Day0Spec

BOUNDARY = "===============0740947994048919689=="
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,35}$")
VARS = ("name", "n", "count", "ip")


def substitute(text: str, name: str, n: int, count: int, ip: str) -> str:
    values = {"name": name, "n": str(n), "count": str(count), "ip": ip}
    return re.sub(r"\{(%s)\}" % "|".join(VARS), lambda m: values[m.group(1)], text)


def nth_ip(start: str, n: int) -> str:
    """Het n-de adres (1 = startadres) binnen hetzelfde subnet."""
    try:
        iface = ipaddress.ip_interface(start)
    except ValueError as e:
        raise ValueError(f"Ongeldig IP-adres '{start}' (gebruik de vorm 10.0.0.11/24)") from e
    if "/" not in start or iface.version != 4:
        raise ValueError(f"Ongeldig IP-adres '{start}' (gebruik de vorm 10.0.0.11/24)")
    addr = iface.ip + (n - 1)
    if addr not in iface.network or addr == iface.network.broadcast_address or addr == iface.network.network_address:
        raise ValueError(f"Het IP-adres voor VM {n} ({addr}) valt buiten het subnet {iface.network}")
    return f"{addr}/{iface.network.prefixlen}"


def _q(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Een wachtwoord mag geen regeleinde bevatten")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ipv4(value: str, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError as e:
        raise ValueError(f"Ongeldig {what}: '{value}'") from e


def render_cli(spec: Day0Spec, name: str, n: int, count: int) -> str:
    net = spec.network
    ip_cidr = ""
    if spec.product != "other" and net.mode == "static":
        if not net.ip or not net.gateway:
            raise ValueError("Bij een vast IP zijn IP-adres en gateway verplicht")
        ip_cidr = nth_ip(net.ip, n)
    bare_ip = ip_cidr.split("/")[0]
    sub = lambda t: substitute(t, name, n, count, bare_ip)  # noqa: E731
    lines: list[str] = []

    if spec.product != "other":
        if spec.product in ("fortimanager", "fortianalyzer") and net.mode != "static":
            raise ValueError("FortiManager en FortiAnalyzer hebben een vast IP nodig voor de eerste configuratie")
        host = sub(spec.hostname or "")
        if host:
            if not HOSTNAME_RE.match(host):
                raise ValueError(f"Ongeldige hostname '{host}' (letters, cijfers, - _ . en maximaal 35 tekens)")
            lines += ["config system global", f"    set hostname {host}", "end"]
        gate = spec.product == "fortigate"
        if spec.admin_password:
            lines += ["config system admin" + ("" if gate else " user"), '    edit "admin"', f"        set password {_q(spec.admin_password)}", "    next", "end"]
        iface = net.interface
        lines += ["config system interface", f"    edit {iface}"]
        if ip_cidr:
            mask = str(ipaddress.ip_interface(ip_cidr).netmask)
            lines += (["        set mode static"] if gate else []) + [f"        set ip {bare_ip} {mask}"]
        else:
            lines += ["        set mode dhcp"]
        lines += ["        set allowaccess ping https ssh", "    next", "end"]
        if ip_cidr:
            gw = _ipv4(net.gateway, "gateway")
            lines += ["config router static" if gate else "config system route", "    edit 1"]
            if not gate:
                lines += ["        set dst 0.0.0.0 0.0.0.0"]
            lines += [f"        set gateway {gw}", f"        set device {iface}", "    next", "end"]
        dns = [_ipv4(d, "DNS-server") for d in net.dns if d.strip()]
        if dns:
            lines += ["config system dns", f"    set primary {dns[0]}"] + ([f"    set secondary {dns[1]}"] if len(dns) > 1 else []) + ["end"]

    if spec.custom_cli.strip():
        lines.append(sub(spec.custom_cli.strip("\n")))
    return "\n".join(lines) + ("\n" if lines else "")


def pick_tokens(tokens: list[str], count: int) -> list[str | None]:
    cleaned = [t.strip() for t in tokens if t.strip()]
    if not cleaned:
        return [None] * count
    if len(cleaned) != count:
        raise ValueError(f"{len(cleaned)} tokens opgegeven voor {count} VM's: geef er precies {count} op (één per VM)")
    for t in cleaned:
        if not TOKEN_RE.match(t):
            raise ValueError("Een FortiFlex-token mag alleen letters en cijfers bevatten")
    return cleaned


def build_user_data(cli: str, token: str | None, mode: str) -> str:
    if token is None:
        return cli
    if mode == "cli":
        return cli.rstrip("\n") + ("\n" if cli.strip() else "") + f"execute vm-license {token}\n"

    def part(filename: str, body: str) -> str:
        return (f"--{BOUNDARY}\nContent-Type: text/plain; charset=\"us-ascii\"\nMIME-Version: 1.0\n"
                f"Content-Transfer-Encoding: 7bit\nContent-Disposition: attachment; filename=\"{filename}\"\n\n{body}\n")

    return (f"Content-Type: multipart/mixed; boundary=\"{BOUNDARY}\"\nMIME-Version: 1.0\n\n"
            + part("config", cli.rstrip("\n")) + part("license", f"LICENSE-TOKEN:{token}") + f"--{BOUNDARY}--\n")


def render_user_data(spec: Day0Spec, name: str, n: int, count: int, token: str | None) -> str:
    data = build_user_data(render_cli(spec, name, n, count), token, spec.token_mode)
    try:
        data.encode("ascii")
    except UnicodeEncodeError as e:
        raise ValueError("De Day-0 configuratie mag alleen ASCII-tekens bevatten") from e
    return data


def build_config_drive(user_data: str, dest: Path) -> None:
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge="1.09", vol_ident="config-2")
    iso.add_directory("/OPENSTACK", rr_name="openstack")
    iso.add_directory("/OPENSTACK/LATEST", rr_name="latest")
    raw = user_data.encode("ascii")
    iso.add_fp(io.BytesIO(raw), len(raw), "/OPENSTACK/LATEST/USER_DATA.;1", rr_name="user_data")
    iso.write(str(dest))
    iso.close()
