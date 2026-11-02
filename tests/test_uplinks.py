"""Uplinks across site shapes: BitBarn (PPPoE + LTE box, recursive routes,
policy routing), Palniki (USB LTE + spare DHCP), DPI tables that are no uplink.
"""

from __future__ import annotations

from conftest import load_module

up = load_module("uplinks")

T, F = "true", "false"


def _bitbarn():
    ups = up.build_uplinks(
        dhcp_clients=[],
        pppoe_clients=[{"name": "ISP1", "interface": "sfp-sfpplus12",
                        "running": T, "disabled": F}],
        lte=[],
        addresses=[
            {"address": "100.64.0.10/32", "interface": "ISP1"},
            {"address": "10.100.250.1/30", "interface": "sfp-sfpplus11"},
            {"address": "10.100.10.1/24", "interface": "vlan10-mgmt"},
        ],
        running={"ISP1": True, "sfp-sfpplus11": True},
        comments={"sfp-sfpplus11": "LTE (GW-LTE)"},
        wan_members=[{"list": "WAN", "interface": "ISP1"},
                     {"list": "WAN", "interface": "sfp-sfpplus11"}],
    )  # fmt: skip
    default = [
        {"dst-address": "0.0.0.0/0", "gateway": "77.88.8.8", "distance": "1",
         "routing-table": "main", "active": T, "immediate-gw": "ISP1",
         "comment": "DEF-main-ISP1-backup"},
        {"dst-address": "0.0.0.0/0", "gateway": "77.88.8.1", "distance": "2",
         "routing-table": "main", "active": F, "comment": "DEF-main-LTE"},
        {"dst-address": "0.0.0.0/0", "gateway": "77.88.8.1", "distance": "1",
         "routing-table": "to_LTE", "active": T,
         "immediate-gw": "10.100.250.2%sfp-sfpplus11", "comment": "DEF-LTE"},
        {"dst-address": "0.0.0.0/0", "gateway": "77.88.8.8", "distance": "1",
         "routing-table": "to_ISP1", "active": T, "immediate-gw": "ISP1"},
        {"dst-address": "0.0.0.0/0", "gateway": "WG-niderlandy", "distance": "1",
         "routing-table": "niderlandy", "active": F},
    ]  # fmt: skip
    hosts = [
        {"dst-address": "77.88.8.8/32", "gateway": "ISP1", "routing-table": "main"},
        {"dst-address": "77.88.8.1/32", "gateway": "10.100.250.2%sfp-sfpplus11",
         "routing-table": "main"},
    ]  # fmt: skip
    service = up.attach_routes(
        ups, default, hosts,
        static_addresses={"sfp-sfpplus11": "10.100.250.1/30"},
    )  # fmt: skip
    return ups, service


def test_bitbarn_kinds_and_roles():
    ups, service = _bitbarn()
    assert ups["ISP1"].kind == "pppoe" and ups["ISP1"].role == "active"
    lte = ups["sfp-sfpplus11"]
    assert lte.kind == "static" and lte.bound and lte.comment == "LTE (GW-LTE)"
    assert lte.role == "backup"  # in main only as distance 2
    tables = {r.table for r in lte.routes}
    assert tables == {"main", "to_LTE"}
    inactive_main = next(r for r in lte.routes if r.table == "main")
    assert inactive_main.via == "77.88.8.1" and not inactive_main.active
    assert lte.address == "10.100.250.1"
    assert [s.table for s in service] == ["niderlandy"]


def test_route_rules():
    rules = up.parse_route_rules(
        [
            {"action": "mark-routing", "new-routing-mark": "to_ISP1",
             "in-interface": "vlan30-worker", "disabled": F,
             "comment": "VLAN30-servers-ISP1"},
            {"action": "mark-routing", "new-routing-mark": "to_LTE",
             "in-interface-list": "LAN", "disabled": T, "comment": "Other-LAN-LTE"},
            {"action": "mark-connection", "new-connection-mark": "x"},
        ]
    )  # fmt: skip
    assert [(r.table, r.disabled) for r in rules] == [
        ("to_ISP1", False),
        ("to_LTE", True),
    ]
    assert rules[0].match == "in-interface=vlan30-worker"


def test_palniki_lte_and_dhcp_merge():
    ups = up.build_uplinks(
        dhcp_clients=[
            {"interface": "lte1", "status": "bound", "address": "192.168.225.33/24",
             "gateway": "192.168.225.1", "dynamic": T},
            {"interface": "ether1", "status": "searching...", "comment": "backup"},
        ],
        pppoe_clients=[],
        lte=[{"name": "lte1", "running": T, "disabled": F}],
        addresses=[{"address": "192.168.225.33/24", "interface": "lte1"}],
        running={"lte1": True, "ether1": False},
        comments={},
    )  # fmt: skip
    lte = ups["lte1"]
    assert lte.kind == "lte" and lte.bound and lte.gateway == "192.168.225.1"
    assert ups["ether1"].kind == "dhcp" and not ups["ether1"].bound
    service = up.attach_routes(
        ups,
        [
            {"dst-address": "0.0.0.0/0", "gateway": "192.168.225.1", "distance": "2",
             "active": T, "immediate-gw": "192.168.225.1%lte1"},
            {"dst-address": "0.0.0.0/0", "gateway": "10.255.0.1", "distance": "1",
             "routing-table": "to_dom_dpi", "active": T},
        ],
        [],
    )  # fmt: skip
    assert lte.role == "active" and ups["ether1"].role == "no-route"
    assert [s.table for s in service] == ["to_dom_dpi"]


def test_lte_monitor():
    sig = up.parse_lte_monitor(
        {"registration-status": "registered", "current-operator": "Beeline",
         "access-technology": "LTE", "rsrp": "-97dBm", "rsrq": "-12dB",
         "sinr": "15dB", "rssi": "-68dBm", "primary-band": "B3@20Mhz earfcn: 1300"}
    )  # fmt: skip
    assert sig["operator"] == "Beeline" and sig["band"] == "B3"
    assert (sig["rsrp"], sig["sinr"]) == (-97, 15) and sig["quality"] == "fair"
