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
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .models import ModemInfo

_LOGGER = logging.getLogger(__name__)

STATUS = "admin/status/modem/action_status"
SLOT = "admin/status/modem/action_slot/{slot}"
SMS_LIST = "admin/status/modem/action_read_sms/all?block=1"
SMS_SEND = "admin/status/modem/action_send_sms/{kind}"
DEVICE_FORM = "admin/network/modem/device"

# Free balance requests by SMS (USSD does not work on this firmware).
# Operator name as the SIM reports it -> (number, text).
# Only numbers known to be free; add others once verified.
BALANCE_SMS: dict[str, tuple[str, str]] = {
    "megafon": ("000100", "Баланс"),
}


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

    async def read_sms(self) -> list[dict[str, Any]]:
        data = await self._get_json(SMS_LIST)
        if data.get("code") != 0:
            raise TandemError(f"SMS list code {data.get('code')}")
        return decode_sms((data.get("out") or {}).get("sms") or [])

    async def send_sms(self, phone: str, text: str) -> None:
        if all(ord(ch) < 128 for ch in text):
            kind, payload = "gsm", text
        else:
            kind = "ucs2"
            payload = "".join(f"{ord(ch):04X}" for ch in text)
        query = urlencode({"text": payload, "phone": phone})
        data = await self._get_json(f"{SMS_SEND.format(kind=kind)}?{query}")
        if data.get("code") != 0:
            raise TandemError(f"SMS not sent (code {data.get('code')})")

    async def failover_mode(self) -> dict[str, str]:
        """Read Ping Check settings from the settings page (never submitted)."""
        if self._cookie is None:
            await self.login()
        try:
            async with self._session.get(
                f"{self._base}/{DEVICE_FORM}",
                cookies={"sysauth": self._cookie or ""},
                allow_redirects=False,
                timeout=self._timeout,
            ) as resp:
                html = await resp.text()
        except (ClientError, TimeoutError, OSError) as err:
            raise TandemError(f"{self._host}: {err}") from err
        return parse_failover(html)

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


def _hex_text(data: str, dcs: Any) -> str:
    try:
        if dcs == 2:
            return "".join(
                chr(int(data[i : i + 4], 16)) for i in range(0, len(data), 4)
            )
        if dcs == 1:
            raw = bytes(int(data[i : i + 2], 16) for i in range(0, len(data), 2))
            return raw.decode("utf-8", errors="replace")
    except ValueError:
        return data
    return data


def decode_sms(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decode text and glue multi-part messages, newest first."""
    parts: dict[tuple, list[dict]] = {}
    single: list[dict] = []
    for sms in raw:
        if sms.get("fail"):
            continue
        item = dict(sms, text=_hex_text(str(sms.get("data", "")), sms.get("dcs")))
        if sms.get("c_ref"):
            parts.setdefault((sms.get("sender"), sms.get("c_ref")), []).append(item)
        else:
            single.append(item)
    for chunks in parts.values():
        chunks.sort(key=lambda x: x.get("c_seq", 0))
        first = dict(chunks[0])
        first["text"] = "".join(c["text"] for c in chunks)
        single.append(first)
    single.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return [
        {
            "sender": str(m.get("sender", "")),
            "text": m["text"],
            "ts": m.get("ts"),
            "when": f"{m.get('date', '')} {m.get('time', '')}".strip(),
        }
        for m in single
    ]


_MONEY = re.compile(
    r"(?:баланс|balance|на сч[её]те|остаток)[^\d-]{0,20}(-?\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
_RUB = re.compile(r"(-?\d+(?:[.,]\d{1,2})?)\s*(?:р\b|р\.|руб|₽|rub)", re.IGNORECASE)


def parse_balance(messages: list[dict[str, Any]], sender: str | None = None):
    """Newest balance-looking SMS -> (amount, message) or (None, None)."""
    for msg in messages:
        if sender and sender not in msg["sender"] and not msg["sender"].isalpha():
            continue
        text = msg["text"]
        match = _MONEY.search(text) or _RUB.search(text)
        if match:
            return float(match.group(1).replace(",", ".")), msg
    return None, None


def balance_request_for(operator: str) -> tuple[str, str] | None:
    key = re.sub(r"[^a-z0-9]", "", operator.lower())
    for name, req in BALANCE_SMS.items():
        if name in key:
            return req
    return None


def parse_failover(html: str) -> dict[str, str]:
    """Selected values of sim{1,2}_control/_host1/_ping/_timeout on the form."""
    out: dict[str, str] = {}
    for slot in ("sim1", "sim2"):
        sel = re.search(
            rf'name="cbid\.cm\.modem0\.{slot}_control".*?</select>', html, re.S
        )
        if sel:
            chosen = re.search(
                r'<option[^>]*selected[^>]*value="([^"]*)"', sel.group(0)
            )
            chosen = chosen or re.search(
                r'<option[^>]*value="([^"]*)"[^>]*selected', sel.group(0)
            )
            out[f"{slot}_control"] = chosen.group(1) if chosen else ""
        for field in ("ping", "timeout", "host1", "host2", "timelimit"):
            m = re.search(
                rf'name="cbid\.cm\.modem0\.{slot}_{field}"[^>]*value="([^"]*)"', html
            )
            if m:
                out[f"{slot}_{field}"] = m.group(1)
    return out
