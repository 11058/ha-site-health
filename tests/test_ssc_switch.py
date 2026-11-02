"""PS308GV3 / PS208GV3 parsing and VLAN table maths (real answers)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import load_module

sw = load_module("ssc_switch")
F = json.loads((Path(__file__).parent / "fixtures" / "ssc_switch.json").read_text())


def test_parse_house():
    info = sw.parse_switch(F["house_all"], F["house_macs"])
    assert info.model == "PS308GV3" and info.firmware == "6.1.251211"
    assert info.temperature == 50.5 and info.poe_total_w is not None
    names = [p.name for p in info.ports]
    assert names == ["1", "2", "3", "4", "5", "6", "7", "8", "LAN1", "LAN2", "SFP"]
    by = {p.name: p for p in info.ports}
    assert by["LAN1"].uplink and by["LAN1"].trunk and by["LAN1"].vlan is None
    assert by["LAN2"].trunk
    assert by["8"].vlan == 11 and by["1"].vlan == 12 and by["6"].vlan == 20
    assert by["3"].vlan == 1 and not by["3"].up
    assert by["1"].up and by["1"].speed == "100M" and by["1"].poe_capable
    assert by["SFP"].poe_capable is False
    assert sum(len(p.macs) for p in info.ports) == len(F["house_macs"]["MAC"])
    assert all(len(m) == 17 for p in info.ports for m in p.macs)


def test_parse_208_without_temperature():
    info = sw.parse_switch(F["garazh2_all"], None)
    assert info.model == "PS208GV3" and info.temperature is None
    assert len(info.ports) == 10 and info.ports[-1].name == "LAN2"


def test_vlan_move_access_port():
    vlan = F["house_all"]["vlan"]
    new = sw.vlan_table_with(vlan, 0, 30, 11)  # port 1: VLAN 12 -> 30
    tbl = dict(zip(new["vlanid"], new["vlanport"], strict=True))
    assert tbl[12] == 67 & ~1 and tbl[30] == 1
    assert new["porttype"] == vlan["porttype"]
    assert vlan["vlanport"][2] == 67  # input untouched


def test_vlan_default_and_new_id():
    vlan = F["house_all"]["vlan"]
    back = sw.vlan_table_with(vlan, 7, 1, 11)  # port 8 to default VLAN 1
    assert all(not (m >> 7 & 1) for m in back["vlanport"])
    added = sw.vlan_table_with(vlan, 2, 55, 11)
    assert 55 in added["vlanid"]
    assert added["vlanport"][added["vlanid"].index(55)] == 1 << 2


def test_vlan_trunk_rules():
    vlan = F["house_all"]["vlan"]
    t = sw.vlan_table_with(vlan, 3, "trunk", 11)
    assert t["porttype"] & (1 << 3)
    no_trunk = dict(vlan, porttype=1 << 9)
    with pytest.raises(sw.SwitchError):
        sw.vlan_table_with(no_trunk, 9, 20, 11)  # last trunk would vanish
