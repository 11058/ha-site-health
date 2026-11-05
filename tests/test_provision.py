"""Onboarding script and core-router provisioning helpers."""

from __future__ import annotations

import asyncio

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


def _fw(*rules):
    return [{".id": f"*{i}", "chain": "forward", **r} for i, r in enumerate(rules, 1)]


def test_drop_counts_even_when_firewall_switched_off():
    # The owner disabled every rule: our accept must still go above the drop,
    # otherwise switching the firewall back on cuts HA off.
    rules = _fw(
        {"action": "accept", "connection-state": "established,related",
         "disabled": "true"},
        {"action": "drop", "connection-state": "invalid", "disabled": "true"},
        {"action": "drop", "disabled": "true", "comment": "drop all else"},
    )  # fmt: skip
    assert provision.first_forward_drop(rules) == "*3"
    assert provision.first_forward_drop(_fw({"action": "accept"})) == "*1"


def test_rule_below_drop_is_detected():
    comment = "site-health: HA web -> switches 10.5.1.0/24"
    below = _fw(
        {"action": "accept"},
        {"action": "drop"},
        {"action": "accept", "comment": comment},
    )
    ours, placed = provision.rule_state(below, comment)
    assert ours[".id"] == "*3" and not placed
    above = _fw({"action": "accept", "comment": comment}, {"action": "drop"})
    assert provision.rule_state(above, comment)[1]
    assert provision.rule_state(above, "other") == (None, False)


def test_ensure_moves_and_adds():
    asyncio.run(_ensure_moves_and_adds())


async def _ensure_moves_and_adds():
    comment = "site-health: HA REST -> 10.5.10.0/24"
    rules = _fw({"action": "drop"}, {"action": "accept", "comment": comment})

    class Fake:
        def __init__(self):
            self.calls = []

        async def _list(self, path):
            return rules

        async def _request(self, method, path, payload):
            self.calls.append((method, path, payload))

    fake = Fake()
    res = provision.ProvisionResult()
    await provision._ensure_forward(fake, res, comment=comment, what="x", payload={})
    assert fake.calls == [
        ("POST", "ip/firewall/filter/move", {"numbers": "*2", "destination": "*1"})
    ]
    fake2 = Fake()
    res2 = provision.ProvisionResult()
    await provision._ensure_forward(
        fake2, res2, comment="new", what="y", payload={"chain": "forward"}
    )
    assert fake2.calls[0][2]["place-before"] == "*1" and res2.done == ["y"]
