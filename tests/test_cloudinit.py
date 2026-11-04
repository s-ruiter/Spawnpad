import email
import io

import pycdlib
import pytest

from app import cloudinit as ci
from app.schemas import Day0Network, Day0Spec


def spec(**kw):
    return Day0Spec(**kw)


def test_substitute_known_variables():
    out = ci.substitute("hostname {name} #{n} of {count} at {ip}", name="fgt-2", n=2, count=3, ip="10.0.0.12")
    assert out == "hostname fgt-2 #2 of 3 at 10.0.0.12"


def test_unknown_braces_are_left_alone():
    assert ci.substitute("{foo} {name}", name="x", n=1, count=1, ip="") == "{foo} x"


def test_nth_ip_increments_within_subnet():
    assert ci.nth_ip("10.0.0.11/24", 1) == "10.0.0.11/24"
    assert ci.nth_ip("10.0.0.11/24", 3) == "10.0.0.13/24"


def test_nth_ip_rejects_leaving_the_subnet():
    with pytest.raises(ValueError):
        ci.nth_ip("10.0.0.254/24", 3)


def test_nth_ip_rejects_garbage():
    with pytest.raises(ValueError):
        ci.nth_ip("nonsense", 1)


def test_fortigate_cli_static():
    s = spec(product="fortigate", hostname="{name}", admin_password="Sup3r",
             network=Day0Network(mode="static", ip="10.0.0.11/24", gateway="10.0.0.1", dns=["1.1.1.1", "9.9.9.9"]))
    cli = ci.render_cli(s, name="fgt-2", n=2, count=3)
    assert "set hostname fgt-2" in cli
    assert "set ip 10.0.0.12 255.255.255.0" in cli
    assert "set mode static" in cli
    assert "set gateway 10.0.0.1" in cli and "set device port1" in cli
    assert "set primary 1.1.1.1" in cli and "set secondary 9.9.9.9" in cli
    assert 'edit "admin"' in cli and 'set password "Sup3r"' in cli


def test_fortigate_cli_dhcp():
    cli = ci.render_cli(spec(product="fortigate", hostname="fw"), name="fw", n=1, count=1)
    assert "set mode dhcp" in cli and "set gateway" not in cli


def test_password_is_quoted_and_escaped():
    cli = ci.render_cli(spec(product="fortigate", admin_password='pa"ss\\w'), name="x", n=1, count=1)
    assert 'set password "pa\\"ss\\\\w"' in cli


def test_fortimanager_cli_uses_its_own_syntax():
    s = spec(product="fortimanager", hostname="fmg", admin_password="pw",
             network=Day0Network(mode="static", ip="10.0.0.5/24", gateway="10.0.0.1", dns=["10.0.0.2"]))
    cli = ci.render_cli(s, name="fmg", n=1, count=1)
    assert "config system admin user" in cli
    assert "set ip 10.0.0.5 255.255.255.0" in cli
    assert "config system route" in cli and "set gateway 10.0.0.1" in cli
    assert "set mode" not in cli


def test_fortimanager_needs_static_ip():
    with pytest.raises(ValueError, match="vast IP"):
        ci.render_cli(spec(product="fortimanager", hostname="fmg"), name="fmg", n=1, count=1)


def test_static_requires_ip_and_gateway():
    with pytest.raises(ValueError):
        ci.render_cli(spec(product="fortigate", network=Day0Network(mode="static")), name="x", n=1, count=1)


def test_other_product_only_uses_custom_cli():
    cli = ci.render_cli(spec(product="other", hostname="ignored", custom_cli="config system global\n set hostname {name}\nend"),
                        name="box-1", n=1, count=1)
    assert cli.strip() == "config system global\n set hostname box-1\nend"


def test_custom_cli_is_appended_with_variables():
    s = spec(product="fortigate", hostname="x", custom_cli="config system global\n set timezone 26 # {name}\nend")
    cli = ci.render_cli(s, name="fgt-9", n=9, count=9)
    assert cli.index("set hostname") < cli.index("set timezone 26 # fgt-9")


def test_plain_user_data_without_token():
    assert ci.build_user_data("config system global\nend\n", token=None, mode="mime") == "config system global\nend\n"


def test_mime_user_data_with_token():
    cli = "config system global\n set hostname fgt\nend\n"
    data = ci.build_user_data(cli, token="ABC123DEF456", mode="mime")
    msg = email.message_from_string(data)
    parts = msg.get_payload()
    assert [p.get_filename() for p in parts] == ["config", "license"]
    assert "set hostname fgt" in parts[0].get_payload()
    assert parts[1].get_payload().strip() == "LICENSE-TOKEN:ABC123DEF456"


def test_cli_mode_appends_execute_command():
    data = ci.build_user_data("config system global\nend\n", token="ABC123", mode="cli")
    assert data.rstrip().endswith("execute vm-license ABC123")
    assert not data.startswith("Content-Type")


def test_token_without_cli_still_works():
    data = ci.build_user_data("", token="ABC123", mode="mime")
    assert "LICENSE-TOKEN:ABC123" in data


def test_pick_tokens_requires_matching_count():
    assert ci.pick_tokens([], 3) == [None, None, None]
    assert ci.pick_tokens(["AAA111", "BBB222"], 2) == ["AAA111", "BBB222"]
    with pytest.raises(ValueError, match="2 tokens"):
        ci.pick_tokens(["AAA111", "BBB222"], 3)


@pytest.mark.parametrize("bad", ["abc def", "abc\nINJECT", "tok;en", "x" * 200])
def test_pick_tokens_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        ci.pick_tokens([bad], 1)


def test_config_drive_layout_and_label(tmp_path):
    iso_path = tmp_path / "x.iso"
    ci.build_config_drive("hello user data\n", iso_path)
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    buf = io.BytesIO()
    iso.get_file_from_iso_fp(buf, rr_path="/openstack/latest/user_data")
    assert buf.getvalue() == b"hello user data\n"
    assert iso.pvd.volume_identifier.rstrip(b" ") == b"config-2"
    iso.close()
