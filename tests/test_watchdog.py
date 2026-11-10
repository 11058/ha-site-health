"""Auto restart: the ladder, waits, limits, notify-only, unknown checks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from conftest import load_module

wd_mod = load_module("watchdog")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def make(monkeypatch, **cfg):
    clock = Clock()
    monkeypatch.setattr(wd_mod.time, "time", clock)
    runtime = SimpleNamespace(
        modems={"m1": SimpleNamespace(device_name="Tandem")},
        devices={},
        switches={},
        site=SimpleNamespace(device_name="Береговая", site=None),
    )
    wd = wd_mod.Watchdog.__new__(wd_mod.Watchdog)
    wd.hass = None
    wd.runtime = runtime
    wd.sub_id = "w1"
    wd.title = "Нет интернета"
    wd.cfg = {**wd_mod.DEFAULTS, "notify_only": False,
              "step1": "modem:m1:reconnect", "step1_wait": 2,
              "step2": "modem:m1:reboot", "step2_wait": 4, **cfg}  # fmt: skip
    wd.st = wd_mod.WatchdogState()
    wd.listeners = []
    wd._busy = False
    wd._save = lambda: None
    wd.ran, wd.sent = [], []
    wd.result = (False, "нет ответа")

    async def check():
        return wd.result

    async def run_action(action):
        wd.ran.append(action)

    async def notify(text):
        wd.sent.append(text)

    wd.check, wd.run_action, wd._notify = check, run_action, notify
    return wd, clock


def tick(wd):
    asyncio.run(wd._tick())


def test_ladder_runs_in_order_and_gives_up(monkeypatch):
    wd, clock = make(monkeypatch)
    tick(wd)  # first failure: only noted
    assert wd.st.state == wd_mod.DOWN and wd.ran == []
    clock.t += 120
    tick(wd)  # 2 min < 3 min grace
    assert wd.ran == []
    clock.t += 61
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect"] and wd.st.state == wd_mod.ACTING
    clock.t += 60
    tick(wd)  # waiting the 2 min of step 1
    assert wd.ran == ["modem:m1:reconnect"]
    clock.t += 61
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect", "modem:m1:reboot"]
    clock.t += 241
    tick(wd)
    assert wd.st.state == wd_mod.GAVE_UP
    assert "больше ничего не делаю" in wd.sent[-1]
    clock.t += 600
    tick(wd)
    assert len(wd.ran) == 2  # gave up: nothing more


def test_recovery_notifies_and_cools_down(monkeypatch):
    wd, clock = make(monkeypatch)
    tick(wd)
    clock.t += 181
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect"]
    wd.result = (True, "ok")
    clock.t += 30
    tick(wd)
    assert wd.st.state == wd_mod.OK
    assert "вернулся" in wd.sent[-1] and "переподключить" in wd.sent[-1]
    # down again right away: cooldown keeps the ladder from restarting
    wd.result = (False, "нет")
    tick(wd)
    clock.t += 181
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect"]
    clock.t += 600
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect", "modem:m1:reconnect"]


def test_unknown_check_counts_for_nothing(monkeypatch):
    wd, clock = make(monkeypatch)
    wd.result = (None, "роутер не ответил")
    for _ in range(20):
        clock.t += 60
        tick(wd)
    assert wd.st.state == wd_mod.OK and wd.ran == [] and wd.sent == []


def test_cycle_limit(monkeypatch):
    wd, clock = make(monkeypatch, max_cycles=1, cooldown_min=0)
    tick(wd)
    clock.t += 181
    tick(wd)
    wd.result = (True, "ok")
    tick(wd)
    wd.result = (False, "нет")
    tick(wd)
    clock.t += 181
    tick(wd)
    assert wd.ran == ["modem:m1:reconnect"]
    assert wd.st.state == wd_mod.GAVE_UP and "лимит" in wd.sent[-1]


def test_notify_only_and_paused(monkeypatch):
    wd, clock = make(monkeypatch, notify_only=True)
    tick(wd)
    clock.t += 181
    tick(wd)
    assert wd.ran == [] and "НЕ выполнен" in wd.sent[-1]

    wd2, clock2 = make(monkeypatch)
    wd2.st.paused = True
    tick(wd2)
    clock2.t += 181
    tick(wd2)
    clock2.t += 600
    tick(wd2)
    assert wd2.ran == [] and len(wd2.sent) == 1 and "паузе" in wd2.sent[0]


def test_labels():
    rt = SimpleNamespace(modems={"m1": SimpleNamespace(device_name="Tandem")},
                         devices={}, switches={})  # fmt: skip
    assert wd_mod.step_label("modem:m1:reboot", rt) == "Tandem: перезагрузить"
    assert "ether3" in wd_mod.step_label("router:core:iface:ether3", rt)
    assert wd_mod.step_label("switch:s1:port:2", rt).endswith("порт 3")


def test_netwatch_any_up_is_ok(monkeypatch):
    wd, _clock = make(monkeypatch)
    rows = [
        {"host": "8.8.8.8", "status": "down"},
        {"host": "1.1.1.1", "status": "up"},
        {"host": "10.5.1.10", "status": "down"},
    ]

    async def netwatch():
        return rows

    wd.runtime.site.client = SimpleNamespace(netwatch=netwatch)
    ok, detail = asyncio.run(wd._check_netwatch(["8.8.8.8", "1.1.1.1"]))
    assert ok is True and "1.1.1.1" in detail
    rows[1]["status"] = "down"
    ok, _ = asyncio.run(wd._check_netwatch(["8.8.8.8", "1.1.1.1"]))
    assert ok is False
    ok, _ = asyncio.run(wd._check_netwatch(["9.9.9.9"]))
    assert ok is None


def test_router_ping_without_rights_uses_netwatch(monkeypatch):
    wd, _clock = make(monkeypatch)
    mt = load_module("mikrotik")

    async def ping(*a, **k):
        raise mt.MikrotikError(
            'POST ping failed: 500 {"detail":"not enough permissions (9)"}'
        )

    async def netwatch():
        return [{"host": "1.1.1.1", "status": "up"}]

    wd.runtime.site.client = SimpleNamespace(ping=ping, netwatch=netwatch)
    ok, detail = asyncio.run(wd._check_router())
    assert ok is True and "netwatch" in detail
