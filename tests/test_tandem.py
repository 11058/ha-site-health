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


def _hex16(text: str) -> str:
    return "".join(f"{ord(ch):04X}" for ch in text)


def test_decode_and_glue_sms():
    raw = [
        {"sender": "000100", "dcs": 2, "data": _hex16("Баланс: 123,45р. "),
         "c_ref": 7, "c_tot": 2, "c_seq": 1, "ts": 100,
         "date": "02.11", "time": "12:00"},
        {"sender": "000100", "dcs": 2, "data": _hex16("Тариф Интернет"),
         "c_ref": 7, "c_tot": 2, "c_seq": 2, "ts": 100},
        {"sender": "MegaFon", "dcs": 0, "data": "Hello", "ts": 50},
    ]  # fmt: skip
    msgs = tandem.decode_sms(raw)
    assert msgs[0]["text"] == "Баланс: 123,45р. Тариф Интернет"
    assert msgs[1]["text"] == "Hello"
    amount, msg = tandem.parse_balance(msgs, "000100")
    assert amount == 123.45 and msg["sender"] == "000100"


def test_balance_variants():
    msgs = [{"sender": "000100", "text": "Ваш баланс -12.5 руб.", "ts": 1, "when": ""}]
    assert tandem.parse_balance(msgs, "000100")[0] == -12.5
    msgs = [{"sender": "000100", "text": "На счете 340 р", "ts": 1, "when": ""}]
    assert tandem.parse_balance(msgs, "000100")[0] == 340
    assert tandem.parse_balance([], "000100") == (None, None)
    assert tandem.balance_request_for("MegaFon") == ("000100", "Баланс")
    assert tandem.balance_request_for("Unknown") is None


def test_failover_form():
    html = (
        '<select name="cbid.cm.modem0.sim1_control"><option value="">Disabled</option>'
        '<option value="reconnect" selected="selected">Restart</option></select>'
        '<input name="cbid.cm.modem0.sim1_ping" value="10" />'
    )
    assert tandem.parse_failover(html) == {
        "sim1_control": "reconnect",
        "sim1_ping": "10",
    }


def test_luci_token_forms():
    # LuCI 19.07 pages carry the CSRF token either in a hidden input or in L.env
    for html in (
        '<input type="hidden" name="token" value="0123456789abcdef0123456789abcdef" />',
        '<script>L = new LuCI({"token":"0123456789abcdef0123456789abcdef"});</script>',
        "var x = { token: '0123456789abcdef0123456789abcdef' };",
    ):
        m = tandem._TOKEN_RE.search(html)
        assert m and m.group(1) == "0123456789abcdef0123456789abcdef", html
