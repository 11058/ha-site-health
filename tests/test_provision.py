"""Onboarding script and core-router provisioning helpers."""

from __future__ import annotations

from conftest import load_module

provision = load_module("provision")
parsers = load_module("parsers")


def test_script_core_and_device():
    core = provision.onboarding_script(ha_ip="10.5.20.5", password="pw-1", core=True)
    dev = provision.onboarding_script(ha_ip="10.5.20.5", password="pw-2", core=False)
    assert 'password="pw-1"' in core and 'password="pw-2"' in dev
    assert "address=10.5.20.5/32" in core
    assert "chain=input" in core and "chain=input" not in dev
    assert "read,write," in core
    assert "read,api,rest-api,!write" in dev
    # Re-running must not duplicate anything: every add sits behind a find.
    for line in core.splitlines()[1:]:
        if " add " in line:
            assert line.startswith(":if (") or line.startswith(":local")


def test_password_is_routeros_safe():
    for _ in range(50):
        pw = provision.new_password()
        assert len(pw) >= 20
        assert not set(pw) & set(r'"$\;{}[] ')


def _site(gw):
    ifaces = parsers.parse_interfaces(gw["interfaces"])
    segs = parsers.parse_segments(gw["addresses"], gw["vlans"], ifaces, {"ether4-WAN1"})
    return parsers.parse_neighbors(gw["neighbors"]), segs


def test_device_networks(gw):
    neighbors, segs = _site(gw)
    assert provision.device_networks(neighbors.values(), segs, "10.5.20.5") == [
        "10.5.10.0/24"
    ]
    # HA already in the radios' subnet: no forward rule needed.
    assert provision.device_networks(neighbors.values(), segs, "10.5.10.77") == []


def test_first_forward_drop(gw):
    assert provision.first_forward_drop(gw["filter"]) == "*4"
    assert provision.first_forward_drop([]) is None
