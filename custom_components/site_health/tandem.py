"""Tandem-4GT (Microdrive NetOS = OpenWrt/LuCI) cellular router.

Everything goes through the same LuCI endpoints its own web UI uses, so the
router needs no changes:

    POST /cgi-bin/luci  luci_username, luci_password  -> cookie sysauth
    GET  /cgi-bin/luci/admin/status/modem/action_status -> {"code":0,"out":{...}}
    GET  /cgi-bin/luci/admin/status/modem/action_slot/<1|2>  (switch SIM)

USSD is not functional on this firmware, so no balance. The inactive SIM can
only be seen as "a slot exists"; its state is remembered from the last time
it was active.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .models import ModemInfo

_LOGGER = logging.getLogger(__name__)

STATUS = "admin/status/modem/action_status"
SLOT = "admin/status/modem/action_slot/{slot}"


class TandemError(Exception):
    """Router unreachable or answered with an error."""


class TandemAuthError(TandemError):
    """Login rejected."""


class TandemClient:
    def __init__(
        self, session: aiohttp.ClientSession, host: str, username: str, password: str
    ) -> None:
        self._session = session
        self._host = host
        self._user = username
        self._password = password
        self._base = f"http://{host}/cgi-bin/luci"
        self._cookie: str | None = None
        self._timeout = ClientTimeout(total=20)

    @property
    def host(self) -> str:
        return self._host

    async def login(self) -> None:
        try:
            async with self._session.post(
                self._base,
                data={"luci_username": self._user, "luci_password": self._password},
                allow_redirects=False,
                timeout=self._timeout,
            ) as resp:
                cookie = resp.cookies.get("sysauth")
                if resp.status not in (200, 302) or cookie is None:
                    raise TandemAuthError(f"LuCI login rejected ({resp.status})")
                self._cookie = cookie.value
        except (ClientError, TimeoutError, OSError) as err:
            raise TandemError(f"{self._host}: {err}") from err

    async def _get_json(self, path: str, *, retry: bool = True) -> dict[str, Any]:
        if self._cookie is None:
            await self.login()
        try:
            async with self._session.get(
                f"{self._base}/{path}",
                cookies={"sysauth": self._cookie or ""},
                allow_redirects=False,
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (ClientError, TimeoutError, OSError) as err:
            raise TandemError(f"{self._host}: {err}") from err
        # An expired session answers with the login page (200 HTML or 403).
        if status in (302, 403) or text.lstrip().startswith("<"):
            if not retry:
                raise TandemAuthError("session rejected after a fresh login")
            self._cookie = None
            return await self._get_json(path, retry=False)
        try:
            data = json.loads(text)
        except ValueError as err:
            raise TandemError(f"unexpected answer: {text[:120]}") from err
        if not isinstance(data, dict):
            raise TandemError("unexpected answer")
        return data

    async def status(self) -> dict[str, Any]:
        data = await self._get_json(STATUS)
        if data.get("code") != 0:
            raise TandemError(f"modem status code {data.get('code')}")
        return data.get("out") or {}

    async def select_sim(self, slot: int) -> None:
        data = await self._get_json(SLOT.format(slot=int(slot)))
        if data.get("code") != 0:
            raise TandemError(f"SIM switch refused (code {data.get('code')})")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def parse_status(out: dict[str, Any], previous: ModemInfo | None = None) -> ModemInfo:
    sim = out.get("sim") or {}
    system = out.get("system") or {}
    connect = out.get("connect") or {}
    regs = str(system.get("regs", ""))
    up = connect.get("up")
    info = ModemInfo(
        reachable=True,
        connected=up == 1 if up is not None else None,
        connecting=up == 2,
        uptime_s=int(connect["uptime"]) if connect.get("uptime") is not None else None,
        active_sim=int(sim["cur"]) if sim.get("cur") is not None else None,
        sim_slots=int(sim["num"]) if sim.get("num") is not None else None,
        sim_status=str(sim.get("status", "")),
        operator=str(sim.get("spn") or system.get("spn") or ""),
        iccid=str(sim.get("iccid", "")),
        registration=regs,
        registered=regs.upper().startswith("REGISTERED") if regs else None,
        roaming="ROAM" in regs.upper(),
        tech=str(system.get("tech", "")),
        band=str(system.get("band", "")),
        bandwidth_mhz=_num(system.get("dlbandw")),
        rsrp=_num(system.get("rsrp")),
        rsrq=_num(system.get("rsrq")),
        rssi=_num(system.get("rssi")),
        sinr=_num(system.get("sinr")),
        signal_pct=_int(system.get("rssi_lv")),
        cell_id=str(system.get("cid", "")),
        temperature=_num((out.get("ts") or {}).get("temp")),
        last_by_slot=dict(previous.last_by_slot) if previous else {},
    )
    if info.active_sim is not None:
        info.last_by_slot[str(info.active_sim)] = {
            "operator": info.operator,
            "sim_status": info.sim_status,
            "registration": info.registration,
            "connected": info.connected,
            "rsrp": info.rsrp,
            "sinr": info.sinr,
            "tech": info.tech,
        }
    return info


def signal_quality(info: ModemInfo) -> str:
    """Rough LTE verdict from RSRP/SINR, for a colour in the panel."""
    if info.rsrp is None:
        return "unknown"
    if info.rsrp >= -90 and (info.sinr is None or info.sinr >= 10):
        return "good"
    if info.rsrp >= -105 and (info.sinr is None or info.sinr >= 0):
        return "fair"
    return "poor"
