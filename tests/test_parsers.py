"""Parser tests against RouterOS 7 REST-shaped fixtures."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("3d4h5m6s", 3 * 86400 + 4 * 3600 + 5 * 60 + 6),
        ("1w2d", 9 * 86400),
        ("12s", 12),
        ("1d02:03:04", 86400 + 2 * 3600 + 3 * 60 + 4),
        ("00:00:30", 30),
        ("23ms456us", 0.023456),
        ("never", None),
        ("", None),
    ],
)
def test_parse_duration(parsers, text, seconds):
    result = parsers.parse_duration(text)
    if seconds is None:
        assert result is None
    else:
        assert result == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("text", "mbps"),
    [
        ("866.6Mbps-80MHz/2S/SGI", 866.6),
        ("1.2Gbps", 1200.0),
        ("72.2Mbps", 72.2),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_rate(parsers, text, mbps):
    assert parsers.parse_rate_mbps(text) == mbps


def test_system(parsers, gw):
    info = parsers.parse_system(
        gw["identity"], gw["resource"], gw["routerboard"], gw["health"]
    )
    assert info.identity == "GW"
    assert info.version == "7.25"
    assert info.model == "C53UiG+5HPaxD2HPaxD"
    assert info.cpu_load == 7
    assert info.memory_used_pct == 25.0
    assert info.temperature == 52.0
    assert info.uptime_s == 3 * 86400 + 4 * 3600 + 5 * 60 + 6


def test_rates(parsers, gw):
    old = parsers.parse_interfaces(gw["interfaces"])
    rows = [dict(r) for r in gw["interfaces"]]
    for row in rows:
        if row["name"] == "ether4-WAN1":
            row["rx-byte"] = str(5_000_000 + 1_250_000)  # 10 Mbit in 1 s
    new = parsers.parse_interfaces(rows)
    parsers.apply_rates(new, old, 1.0)
    assert new["ether4-WAN1"].rx_bps == 10_000_000
    assert new["ether4-WAN1"].tx_bps == 0


def test_rates_counter_reset(parsers, gw):
    old = parsers.parse_interfaces(gw["interfaces"])
    rows = [dict(r, **{"rx-byte": "1"}) for r in gw["interfaces"]]
    new = parsers.parse_interfaces(rows)
    parsers.apply_rates(new, old, 30.0)
    assert new["ether4-WAN1"].rx_bps is None


def test_wans(parsers, gw):
    ifaces = parsers.parse_interfaces(gw["interfaces"])
    wans = parsers.parse_wans(gw["dhcp_clients"], ifaces)
    assert wans["ether4-WAN1"].bound
    assert wans["ether4-WAN1"].gateway == "192.168.8.1"
    assert not wans["ether3-WAN2"].bound
    assert wans["ether3-WAN2"].disabled
    assert wans["ether3-WAN2"].comment == "Reserve ISP"


def test_probes(parsers, gw):
    probes = parsers.parse_probes(gw["netwatch"])
    wan = probes["wan_8_8_8_8"]
    assert wan.up is True
    assert wan.rtt_ms == pytest.approx(23.46, abs=0.01)
    assert wan.loss_pct == 0
    sw = probes["10_5_1_12"]
    assert sw.up is False
    assert sw.name == "SW-Garazh2"


def test_tunnels(parsers, gw):
    tunnels = parsers.parse_tunnels(gw["wg_peers"], stale_s=180)
    dom = tunnels["dom_hub"]
    assert dom.connected
    assert dom.handshake_age_s == 105
    assert (
        parsers.parse_tunnels(gw["wg_peers"], stale_s=60)["dom_hub"].connected is False
    )


def test_segments(parsers, gw):
    ifaces = parsers.parse_interfaces(gw["interfaces"])
    segs = parsers.parse_segments(
        gw["addresses"], gw["vlans"], ifaces, exclude_interfaces={"ether4-WAN1"}
    )
    kinds = {s.network: s.kind for s in segs.values()}
    assert kinds == {
        "10.5.1.0/24": "switches",
        "10.5.20.0/24": "iot",
        "10.5.30.0/24": "family",
        "10.5.40.0/24": "guest",
        "10.5.99.0/24": "admin",
    }


def test_clients(parsers, gw):
    ifaces = parsers.parse_interfaces(gw["interfaces"])
    segs = parsers.parse_segments(gw["addresses"], gw["vlans"], ifaces, {"ether4-WAN1"})
    neighbors = parsers.parse_neighbors(gw["neighbors"])
    clients = parsers.build_clients(
        leases=gw["leases"],
        arp=gw["arp"],
        registrations=gw["registrations"],
        access_list=gw["access_list"],
        segments=segs,
        family_vlan=30,
        exclude_macs=set(neighbors),
    )
    masha = clients["AA:00:00:00:00:01"]
    assert masha.family and masha.online and masha.wireless
    assert masha.segment_kind == "family"
    assert masha.signal_dbm == -58
    assert masha.family_entry_id == "*2"

    guest = clients["AA:00:00:00:00:02"]
    assert guest.segment_kind == "guest" and guest.online and not guest.family

    ha = clients["AA:00:00:00:00:03"]
    assert ha.static_lease and ha.online and ha.segment_kind == "iot"

    assert clients["AA:00:00:00:00:04"].online is False  # lease seen 3 days ago
    assert clients["AA:00:00:00:00:05"].online is False  # stale ARP only

    tablet = clients["AA:00:00:00:00:09"]  # family entry, never seen
    assert tablet.family and not tablet.online and tablet.ip == ""

    admin = clients["60:00:00:00:00:99"]
    assert admin.access_vlan == 99 and not admin.family

    assert "02:00:00:00:01:01" not in clients  # BS is infrastructure


def test_radio_links(parsers, gw):
    links = parsers.parse_radio_links(
        gw["radio_registrations"], {"02:00:00:00:01:05": "BR-Banya"}
    )
    (link,) = links.values()
    assert link.peer_name == "BR-Banya"
    assert link.signal_dbm == -62
    assert link.tx_rate_mbps == 432.2
    assert link.uptime_s == 2 * 86400 + 3600


def test_candidates(parsers, gw):
    found = parsers.mikrotik_candidates(parsers.parse_neighbors(gw["neighbors"]))
    assert [n.identity for n in found] == ["BS", "BR-Banya"]


def test_translit(parsers):
    assert parsers.translit("Телефон Маши") == "Telefon Mashi"
    assert parsers.family_comment("Планшет «детский»", 30) == (
        "Family: Planshet detskiy -> VLAN 30 (HA)"
    )
    assert parsers.slug("wan-8.8.8.8") == "wan_8_8_8_8"
