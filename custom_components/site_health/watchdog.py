"""Automatic restart when a site loses the internet.

One rule (a «watchdog» subentry) = how to check + a ladder of actions.

Check, every `interval_s`:
  router   – the core MikroTik pings the targets (REST /ping) in a routing
             table, so a backup uplink can be checked on its own table;
  netwatch – an existing netwatch entry on the core router is down;
  ha       – Home Assistant itself opens TCP to the targets (one-uplink sites).
"No internet" = none of the targets answered. A failed check (HA cannot reach
the router, e.g. while it reboots) is "unknown" and counts for nothing.

After `down_after_min` without the internet, the steps run one by one; after
each step the rule waits its own time for the internet to come back before
the next one. Steps:
  modem:<sub>:reconnect | modem:<sub>:sim | modem:<sub>:reboot   (Tandem)
  router:core:reboot | router:<sub>:reboot                        (MikroTik)
  router:core:iface:<name>                     (disable + enable an interface)
  switch:<sub>:port:<n>                        (PoE switch port power cycle)
  power                                        (HA switch entity off → on)

Safeguards: at most `max_cycles` ladders in `window_h` hours, a cooldown after
a ladder, «только уведомлять» (notify what would be done, do nothing), and a
pause switch. Notifications go to the admins' phones (HA mobile app) and to
any notify services listed; a persistent notification is always kept.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from . import SiteRuntime

_LOGGER = logging.getLogger(__name__)

STEPS = 4
DEFAULTS: dict[str, Any] = {
    "probe": "router",
    "routing_table": "main",
    "netwatch_host": "",
    "targets": "1.1.1.1, 8.8.8.8, 77.88.8.8",
    "interval_s": 30,
    "down_after_min": 3,
    "power_entity": "",
    "power_off_s": 15,
    "max_cycles": 3,
    "window_h": 6,
    "cooldown_min": 10,
    "notify_admins": True,
    "notify_services": [],
    "notify_only": True,
}
STEP_WAIT_DEFAULT = {1: 2, 2: 4, 3: 5, 4: 5}  # minutes

# state
OK, DOWN, ACTING, GAVE_UP = "ok", "down", "acting", "gave_up"


def step_label(action: str, runtime: SiteRuntime | None = None) -> str:
    """Russian name of a step for notifications and the panel."""
    if not action:
        return ""
    parts = action.split(":")
    kind = parts[0]
    if kind == "power":
        return "передёрнуть питание (розетка/реле)"
    if kind == "modem":
        name = _modem_name(runtime, parts[1])
        return {
            "reconnect": f"{name}: переподключить сотовую связь",
            "sim": f"{name}: перейти на другую SIM",
            "reboot": f"{name}: перезагрузить",
        }.get(parts[2], action)
    if kind == "router":
        who = (
            "главный роутер" if parts[1] == "core" else _device_name(runtime, parts[1])
        )
        if parts[2] == "reboot":
            return f"{who}: перезагрузить"
        if parts[2] == "iface":
            return f"{who}: выключить и включить {parts[3]}"
    if kind == "switch":
        from .ssc_switch import port_name

        port = port_name(int(parts[3]))
        return f"{_switch_name(runtime, parts[1])}: перезапустить порт {port}"
    return action


def _modem_name(runtime, sub: str) -> str:
    m = runtime.modems.get(sub) if runtime else None
    return m.device_name if m else "4G-роутер"


def _device_name(runtime, sub: str) -> str:
    d = runtime.devices.get(sub) if runtime else None
    return d.device_name if d else "MikroTik"


def _switch_name(runtime, sub: str) -> str:
    s = runtime.switches.get(sub) if runtime else None
    return s.device_name if s else "коммутатор"


def action_choices(
    runtime: SiteRuntime | None, *, has_power: bool = True
) -> dict[str, str]:
    """All steps this site can do: value -> label."""
    out: dict[str, str] = {}
    if runtime is None:
        return out
    for sub in runtime.modems:
        for what in ("reconnect", "sim", "reboot"):
            key = f"modem:{sub}:{what}"
            out[key] = step_label(key, runtime)
    site = runtime.site.site
    if site is not None:
        for name in site.wans:
            key = f"router:core:iface:{name}"
            out[key] = step_label(key, runtime)
    out["router:core:reboot"] = step_label("router:core:reboot", runtime)
    for sub in runtime.devices:
        key = f"router:{sub}:reboot"
        out[key] = step_label(key, runtime)
    for sub, sw in runtime.switches.items():
        ports = getattr(sw.data, "ports", None) or []
        for port in ports:
            if port.uplink:
                continue  # never the link to the router
            key = f"switch:{sub}:port:{port.index}"
            out[key] = step_label(key, runtime) + (" (PoE)" if port.poe_enabled else "")
    if has_power:
        out["power"] = step_label("power", runtime)
    return out


@dataclass
class WatchdogState:
    state: str = OK
    since: float = 0.0  # when the internet went away
    step: int = 0  # index into steps of the current ladder
    step_at: float = 0.0
    done: list[str] = field(default_factory=list)  # steps of the current ladder
    cycles: list[float] = field(default_factory=list)  # ladder start times
    cooldown_until: float = 0.0
    last_check: float = 0.0
    last_result: bool | None = None
    last_detail: str = ""
    paused: bool = False
    log: list[dict[str, Any]] = field(default_factory=list)


class Watchdog:
    """One auto-restart rule of a site."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: SiteRuntime,
        sub_id: str,
        title: str,
        data: dict,
    ) -> None:
        self.hass = hass
        self.runtime = runtime
        self.sub_id = sub_id
        self.title = title
        self.cfg = {**DEFAULTS, **data}
        self.st = WatchdogState()
        self._store: Store = Store(hass, 1, f"{DOMAIN}.watchdog.{sub_id}")
        self._unsub = None
        self._busy = False
        self.listeners: list = []

    # --- config -----------------------------------------------------------
    @property
    def steps(self) -> list[tuple[str, int]]:
        out = []
        for i in range(1, STEPS + 1):
            action = self.cfg.get(f"step{i}") or ""
            if action:
                wait = int(self.cfg.get(f"step{i}_wait", STEP_WAIT_DEFAULT[i]))
                out.append((action, max(wait, 1)))
        return out

    @property
    def targets(self) -> list[str]:
        raw = str(self.cfg.get("targets") or "")
        return [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]

    # --- life -------------------------------------------------------------
    async def async_start(self) -> None:
        saved = await self._store.async_load() or {}
        self.st.paused = bool(saved.get("paused"))
        self.st.cycles = [float(x) for x in saved.get("cycles", [])]
        self.st.log = list(saved.get("log", []))[:50]
        self._unsub = async_track_time_interval(
            self.hass,
            self._tick,
            timedelta(seconds=max(int(self.cfg["interval_s"]), 10)),
        )

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "paused": self.st.paused,
                "cycles": self.st.cycles,
                "log": self.st.log,
            },
            5,
        )

    def _changed(self) -> None:
        for cb in list(self.listeners):
            cb()

    def _log(self, text: str, kind: str = "info") -> None:
        self.st.log = [{"ts": time.time(), "kind": kind, "text": text}, *self.st.log][
            :50
        ]
        self._save()
        _LOGGER.info("%s: %s", self.title, text)

    async def set_paused(self, paused: bool) -> None:
        self.st.paused = paused
        self._log("автоперезапуск на паузе" if paused else "автоперезапуск включён")
        self._changed()

    async def reset(self) -> None:
        """Forget the cycle limit and a given-up ladder."""
        self.st.cycles = []
        if self.st.state == GAVE_UP:
            self.st.state = DOWN
            self.st.since = time.time()
        self._log("счётчик попыток сброшен")
        self._changed()

    # --- check ------------------------------------------------------------
    async def check(self) -> tuple[bool | None, str]:
        probe = self.cfg["probe"]
        try:
            if probe == "netwatch":
                return await self._check_netwatch()
            if probe == "ha":
                return await self._check_ha()
            return await self._check_router()
        except Exception as err:
            return None, f"проверка не выполнена: {err}"

    async def _check_router(self) -> tuple[bool | None, str]:
        client = self.runtime.site.client
        table = (self.cfg.get("routing_table") or "main").strip()
        tried = []
        for target in self.targets:
            ok = await client.ping(target, routing_table=table, count=2)
            tried.append(target)
            if ok:
                return True, f"{target} отвечает (с роутера, таблица {table})"
        return False, f"не отвечают {', '.join(tried)} (с роутера, таблица {table})"

    async def _check_netwatch(self) -> tuple[bool | None, str]:
        host = self.cfg.get("netwatch_host") or ""
        rows = await self.runtime.site.client.netwatch()
        for row in rows:
            if row.get("host") == host:
                status = str(row.get("status", ""))
                if status == "up":
                    return True, f"netwatch {host}: up"
                if status == "down":
                    return False, f"netwatch {host}: down"
                return None, f"netwatch {host}: {status or 'неизвестно'}"
        return None, f"netwatch {host} не найден на роутере"

    async def _check_ha(self) -> tuple[bool | None, str]:
        for target in self.targets:
            host, _, port = target.partition(":")
            try:
                _r, w = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port or 443)), 5
                )
                w.close()
                return True, f"{target} отвечает (из HA)"
            except (OSError, TimeoutError, ValueError):
                continue
        return False, f"не отвечают {', '.join(self.targets)} (из HA)"

    # --- the loop ---------------------------------------------------------
    async def _tick(self, _now=None) -> None:
        if self._busy:
            return
        self._busy = True
        before = self._sig()
        try:
            await self._step_machine()
        finally:
            self._busy = False
            if self._sig() != before:
                self._changed()

    def _sig(self) -> tuple:
        st = self.st
        return (
            st.state,
            st.step,
            st.paused,
            st.last_result,
            len(st.log),
            st.log[0]["ts"] if st.log else 0,
        )

    async def _step_machine(self) -> None:
        ok, detail = await self.check()
        now = time.time()
        st = self.st
        st.last_check, st.last_result, st.last_detail = now, ok, detail
        if ok is None:
            return
        if ok:
            if st.state != OK:
                took = _dur(now - st.since)
                text = f"интернет вернулся через {took}"
                if st.done:
                    text += " после: " + "; ".join(st.done)
                    st.cooldown_until = now + int(self.cfg["cooldown_min"]) * 60
                self._log(text, "ok")
                if st.state in (ACTING, GAVE_UP) or now - st.since >= self._grace:
                    await self._notify(text)
            st.state, st.since, st.step, st.done = OK, 0.0, 0, []
            self._told_paused = False
            return
        # no internet
        if st.state == OK:
            st.state, st.since = DOWN, now
            self._log(f"нет интернета: {detail}", "bad")
            return
        if st.state == GAVE_UP:
            return
        if st.state == DOWN:
            if now - st.since < self._grace:
                return
            await self._start_ladder(now)
            return
        if st.state == ACTING:
            steps = self.steps
            if now - st.step_at < steps[st.step][1] * 60:
                return
            if st.step + 1 >= len(steps):
                st.state = GAVE_UP
                text = (
                    f"интернета нет уже {_dur(now - st.since)}; все шаги выполнены "
                    f"({'; '.join(st.done)}), больше ничего не делаю"
                )
                self._log(text, "bad")
                await self._notify(text)
                return
            st.step += 1
            await self._run_step(now)

    @property
    def _grace(self) -> float:
        return int(self.cfg["down_after_min"]) * 60

    async def _start_ladder(self, now: float) -> None:
        st = self.st
        steps = self.steps
        if st.paused or not steps:
            if not getattr(self, "_told_paused", False):
                self._told_paused = True
                why = "автоперезапуск на паузе" if st.paused else "шаги не заданы"
                await self._notify(f"нет интернета {_dur(now - st.since)} ({why})")
            return
        self._told_paused = False
        if now < st.cooldown_until:
            return
        window = int(self.cfg["window_h"]) * 3600
        st.cycles = [t for t in st.cycles if now - t < window]
        if len(st.cycles) >= int(self.cfg["max_cycles"]):
            st.state = GAVE_UP
            text = (
                f"нет интернета {_dur(now - st.since)}; лимит "
                f"{self.cfg['max_cycles']} попыток за {self.cfg['window_h']} ч "
                "исчерпан, ничего не делаю"
            )
            self._log(text, "bad")
            await self._notify(text)
            return
        st.cycles.append(now)
        st.state, st.step, st.done = ACTING, 0, []
        self._save()
        await self._run_step(now)

    async def _run_step(self, now: float) -> None:
        st = self.st
        action, wait = self.steps[st.step]
        label = step_label(action, self.runtime)
        st.step_at = now
        if self.cfg.get("notify_only"):
            text = (
                f"нет интернета {_dur(now - st.since)}. Шаг {st.step + 1}: {label} — "
                "НЕ выполнен (режим «только уведомлять»)"
            )
            st.done.append(f"{label} (не выполнен)")
            self._log(text, "warn")
            await self._notify(text)
            return
        try:
            await self.run_action(action)
        except Exception as err:
            text = f"шаг {st.step + 1} «{label}» не удался: {err}"
            st.done.append(f"{label} (ошибка)")
            self._log(text, "bad")
            await self._notify(text)
            return
        st.done.append(label)
        text = (
            f"нет интернета {_dur(now - st.since)}. Шаг {st.step + 1}: {label}; "
            f"жду {wait} мин"
        )
        self._log(text, "warn")
        await self._notify(text)

    async def run_action(self, action: str) -> None:
        parts = action.split(":")
        kind = parts[0]
        rt = self.runtime
        if kind == "modem":
            modem = rt.modems[parts[1]]
            if parts[2] == "reconnect":
                await modem.async_reconnect()
            elif parts[2] == "reboot":
                await modem.async_reboot()
            elif parts[2] == "sim":
                info = modem.data
                slots = (info.sim_slots if info else None) or 2
                cur = (info.active_sim if info else None) or 1
                await modem.async_select_sim(cur % slots + 1)
            return
        if kind == "router":
            coord = rt.site if parts[1] == "core" else rt.devices[parts[1]]
            if parts[2] == "reboot":
                await coord.client.reboot()
            elif parts[2] == "iface":
                await coord.client.bounce_interface(parts[3])
            return
        if kind == "switch":
            sw = rt.switches[parts[1]]
            port = int(parts[3])
            await sw.async_run(lambda c: c.restart_port(port))
            return
        if kind == "power":
            entity = self.cfg.get("power_entity") or ""
            if not entity:
                raise ValueError("не указана розетка или реле для питания")
            await self.hass.services.async_call(
                "homeassistant", "turn_off", {"entity_id": entity}, blocking=True
            )
            await asyncio.sleep(max(int(self.cfg["power_off_s"]), 3))
            await self.hass.services.async_call(
                "homeassistant", "turn_on", {"entity_id": entity}, blocking=True
            )
            return
        raise ValueError(f"неизвестный шаг {action}")

    # --- notify -----------------------------------------------------------
    async def _notify(self, text: str) -> None:
        from homeassistant.components import persistent_notification

        title = f"{self.runtime.site.device_name}: {self.title}"
        persistent_notification.async_create(
            self.hass, text, title=title, notification_id=f"{DOMAIN}_wd_{self.sub_id}"
        )
        services = set(self.cfg.get("notify_services") or [])
        if self.cfg.get("notify_admins", True):
            services |= set(await admin_phones(self.hass))
        for svc in sorted(services):
            domain, _, name = svc.partition(".")
            if not name:
                domain, name = "notify", svc
            try:
                await self.hass.services.async_call(
                    domain, name, {"title": title, "message": text}, blocking=False
                )
            except Exception as err:
                _LOGGER.warning("notify %s failed: %s", svc, err)

    def view(self) -> dict[str, Any]:
        st = self.st
        steps = [
            {"action": a, "label": step_label(a, self.runtime), "wait_min": w}
            for a, w in self.steps
        ]
        return {
            "id": self.sub_id,
            "title": self.title,
            "state": st.state,
            "paused": st.paused,
            "notify_only": bool(self.cfg.get("notify_only")),
            "probe": self.cfg["probe"],
            "since": st.since or None,
            "step": st.step if st.state == ACTING else None,
            "steps": steps,
            "last_check": st.last_check or None,
            "last_result": st.last_result,
            "last_detail": st.last_detail,
            "cycles_used": len(
                [
                    t
                    for t in st.cycles
                    if time.time() - t < int(self.cfg["window_h"]) * 3600
                ]
            ),
            "max_cycles": int(self.cfg["max_cycles"]),
            "window_h": int(self.cfg["window_h"]),
            "down_after_min": int(self.cfg["down_after_min"]),
            "log": st.log[:15],
        }


async def admin_phones(hass: HomeAssistant) -> list[str]:
    """notify.mobile_app_* services of the phones of admin users."""
    users = {
        u.id for u in await hass.auth.async_get_users() if u.is_admin and u.is_active
    }
    out = []
    for entry in hass.config_entries.async_entries("mobile_app"):
        if entry.data.get("user_id") not in users:
            continue
        from homeassistant.util import slugify

        name = entry.data.get("device_name") or entry.title
        svc = f"mobile_app_{slugify(name)}"
        if hass.services.has_service("notify", svc):
            out.append(f"notify.{svc}")
    return out


def _dur(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s} с"
    if s < 5400:
        return f"{round(s / 60)} мин"
    return f"{s // 3600} ч {s % 3600 // 60} мин"


@callback
def watchdog_state_text(state: str) -> str:
    return {
        OK: "интернет есть",
        DOWN: "нет интернета",
        ACTING: "выполняю шаги",
        GAVE_UP: "сдался",
        "paused": "на паузе",
    }.get(state, state)
