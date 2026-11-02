"""Tandem-4GT LuCI status parsing (real answer, identifiers replaced)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import load_module

tandem = load_module("tandem")
models = load_module("models")

STATUS = json.loads(
    (Path(__file__).parent / "fixtures" / "tandem_status.json").read_text()
)


def test_parse_status():
    info = tandem.parse_status(STATUS["out"])
    assert info.connected is True and info.uptime_s == 164559
    assert info.active_sim == 1 and info.sim_slots == 2
    assert info.sim_status == "READY" and info.operator == "MegaFon"
    assert info.registered and not info.roaming
    assert info.tech == "LTE" and info.band == "B3" and info.bandwidth_mhz == 15
    assert (info.rsrp, info.rsrq, info.sinr) == (-97, -12, 13)
    assert info.signal_pct == 75 and info.temperature == 27
    assert info.last_by_slot["1"]["operator"] == "MegaFon"
    assert tandem.signal_quality(info) == "fair"


def test_slot_memory_survives_switch():
    first = tandem.parse_status(STATUS["out"])
    out = json.loads(json.dumps(STATUS["out"]))
    out["sim"].update(cur=2, spn="Tele2", status="READY")
    out["system"].update(spn="Tele2", rsrp=-85, sinr=20)
    second = tandem.parse_status(out, first)
    assert second.active_sim == 2 and second.operator == "Tele2"
    assert set(second.last_by_slot) == {"1", "2"}
    assert second.last_by_slot["1"]["operator"] == "MegaFon"
    assert tandem.signal_quality(second) == "good"


def test_connecting_and_no_sim():
    out = {"sim": {"cur": 2, "num": 2, "status": "ABSENT"}, "connect": {"up": 2}}
    info = tandem.parse_status(out)
    assert info.connecting and info.connected is False
    assert info.sim_status == "ABSENT" and info.registered is None
