"""Web-smart PoE switches PS308GV3 / PS208GV3 (SSC "SwitchManagement" firmware).

The firmware's own UI talks JSON to a tiny lwIP server: every request is
``POST /<cmd>`` with ``{"data": {"callcmd": <cmd>, "calldata": {...}}}``.

    123  login {password}           -> calldata.login == "success" (+ cookie)
    200  everything: detail, vlan, limits, ...
    114  MAC table: MAC[] with a port bitmap each
    109  802.1Q VLAN: read, or write the whole table
    103  port operation: opcode = value<<9 | port<<4 | op
         op 0 speed, 2 PoE on/off, 3 restart port (PoE power cycle), 5 forced PoE
    201  configuration export (JSON)

VLAN model of this firmware: a normal port is an access port of exactly one
VLAN (bit in that VLAN's ``vlanport`` mask; in none of them = VLAN 1); a port
whose bit is set in ``porttype`` is a trunk carrying every VLAN tagged.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import ClientError, ClientTimeout

_LOGGER = logging.getLogger(__name__)

CMD_LOGIN = 123
CMD_ALL = 200
CMD_MACS = 114
CMD_VLAN = 109
CMD_PORT = 103
CMD_EXPORT = 201

OP_POE = 2
OP_RESTART = 3

EXTRA_PORT_NAMES = ["LAN1", "LAN2", "SFP"]
SPEED = {0: None, 1: "10M half", 2: "10M", 3: "100M half", 4: "100M", 5: "1G"}
DEFAULT_VLAN = 1
MAX_VLANS = 15


class SwitchError(Exception):
    """Switch unreachable or refused."""


class SwitchAuthError(SwitchError):
    """Password rejected."""


@dataclass
class SwitchPort:
    index: int
    name: str
    up: bool
    speed: str | None
    admin_up: bool = True
    poe_capable: bool = False
    poe_enabled: bool = False
    poe_w: float | None = None
    uplink: bool = False
    trunk: bool = False
    vlan: int | None = DEFAULT_VLAN  # None on a trunk
    macs: list[str] = field(default_factory=list)
    tx: int | None = None
    rx: int | None = None


@dataclass
class SwitchInfo:
    model: str = ""
    serial: str = ""
    firmware: str = ""
    ip: str = ""
    mac: str = ""
    temperature: float | None = None
    poe_voltage: float | None = None
    poe_total_w: float | None = None
    vlans: list[int] = field(default_factory=list)
    ports: list[SwitchPort] = field(default_factory=list)


def port_name(index: int) -> str:
    if index < 8:
        return str(index + 1)
    extra = index - 8
    return EXTRA_PORT_NAMES[extra] if extra < len(EXTRA_PORT_NAMES) else str(index + 1)


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mac(raw: str) -> str:
    raw = raw.upper()
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2))


def parse_switch(all_data: dict[str, Any], macs: dict[str, Any] | None) -> SwitchInfo:
    detail = all_data.get("detail") or {}
    vlan = all_data.get("vlan") or {}
    serial = str(detail.get("sn", ""))
    links = list(detail.get("link") or [])
    count = len(links)
    pw = list(detail.get("pw") or [])
    poec = list(detail.get("poec") or [])
    admin = list(detail.get("AdminState") or [1] * count)
    uplink = int(detail.get("uplink") or 0)
    trunk = int(vlan.get("porttype") or 0)
    vlan_of: dict[int, int] = {}
    for vid, mask in zip(
        vlan.get("vlanid") or [], vlan.get("vlanport") or [], strict=False
    ):
        for i in range(count):
            if int(mask) >> i & 1:
                vlan_of[i] = int(vid)
    tx, rx = list(detail.get("tx") or []), list(detail.get("rx") or [])
    ports = []
    for i in range(count):
        is_trunk = bool(trunk >> i & 1)
        ports.append(
            SwitchPort(
                index=i,
                name=port_name(i),
                up=links[i] != 0,
                speed=SPEED.get(links[i], "1G" if links[i] else None),
                admin_up=admin[i] != 0 if i < len(admin) else True,
                poe_capable=i < len(pw),
                poe_enabled=bool(poec[i]) if i < len(poec) else False,
                poe_w=_f(pw[i]) if i < len(pw) else None,
                uplink=bool(uplink >> i & 1),
                trunk=is_trunk,
                vlan=None if is_trunk else vlan_of.get(i, DEFAULT_VLAN),
                tx=int(_f(tx[i]) or 0) if i < len(tx) else None,
                rx=int(_f(rx[i]) or 0) if i < len(rx) else None,
            )
        )
    if macs:
        for raw, bitmap in zip(
            macs.get("MAC") or [], macs.get("bitmap") or [], strict=False
        ):
            for port in ports:
                if int(bitmap) >> port.index & 1:
                    port.macs.append(_mac(raw))
    total = _f(detail.get("tp"))
    return SwitchInfo(
        model=serial[:8] if serial.startswith("PS") else "",
        serial=serial,
        firmware=str(detail.get("V", "")),
        ip=str(detail.get("ip", "")),
        mac=_mac(str(detail.get("mac", ""))) if detail.get("mac") else "",
        temperature=_f(detail.get("T")),
        poe_voltage=_f(detail.get("vol")),
        poe_total_w=round(total / 1000, 1) if total is not None else None,
        vlans=[int(v) for v in vlan.get("vlanid") or []],
        ports=ports,
    )


def vlan_table_with(
    vlan: dict[str, Any], port_index: int, target: int | str, port_count: int
) -> dict[str, Any]:
    """The whole 802.1Q table after moving one port.

    target: a VLAN id (1 = default/untagged) or "trunk".
    """
    new = deepcopy(vlan)
    new["vlan"] = 1
    new["vlantype"] = 1
    ids: list[int] = [int(v) for v in new.get("vlanid") or []]
    masks: list[int] = [int(m) for m in new.get("vlanport") or []]
    bit = 1 << port_index
    masks = [m & ~bit for m in masks]
    porttype = int(new.get("porttype") or 0)
    if target == "trunk":
        porttype |= bit
    else:
        porttype &= ~bit
        vid = int(target)
        if not 1 <= vid <= 4094:
            raise SwitchError(f"VLAN {vid} out of range")
        if vid != DEFAULT_VLAN:
            if vid not in ids:
                if len(ids) >= MAX_VLANS:
                    raise SwitchError("the switch holds at most 15 VLANs")
                ids.append(vid)
                masks.append(0)
            masks[ids.index(vid)] |= bit
    if porttype & ((1 << port_count) - 1) == 0:
        raise SwitchError("at least one port must stay a trunk")
    new["vlanid"], new["vlanport"], new["porttype"] = ids, masks, porttype
    return new


class SwitchBusyError(SwitchError):
    """Another web session is open: the firmware allows only one at a time."""


CMD_LOGOUT = 126


class SscSwitchClient:
    """One short session per poll or action: login, work, logout.

    The firmware keeps a single web session; holding one would lock the
    owner out of the switch's own web page, so nothing is kept between calls.
    """

    def __init__(
        self, session: aiohttp.ClientSession, host: str, password: str
    ) -> None:
        self._session = session
        self._host = host
        self._password = password
        self._base = f"http://{host}"
        self._timeout = ClientTimeout(total=15)

    @property
    def host(self) -> str:
        return self._host

    async def _post(self, cmd: int, calldata: dict | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"data": {"callcmd": cmd}}
        if calldata is not None:
            body["data"]["calldata"] = calldata
        try:
            async with self._session.post(
                f"{self._base}/{cmd}",
                data=json.dumps(body),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
        except aiohttp.ServerDisconnectedError as err:
            if cmd == CMD_LOGIN:
                raise SwitchBusyError(
                    f"{self._host}: another web session is open on the switch"
                ) from err
            raise SwitchError(f"{self._host}: {err}") from err
        except (ClientError, TimeoutError, OSError) as err:
            raise SwitchError(f"{self._host}: {err}") from err
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {}

    async def _login(self) -> None:
        data = await self._post(CMD_LOGIN, {"password": self._password})
        calldata = (data.get("data") or {}).get("calldata") or {}
        if calldata.get("login") != "success":
            raise SwitchAuthError(f"{self._host}: password rejected")

    async def _logout(self) -> None:
        try:
            await self._post(CMD_LOGOUT)
        except SwitchError as err:  # the session times out on its own anyway
            _LOGGER.debug("logout from %s failed: %s", self._host, err)

    async def _cmd(self, cmd: int, calldata: dict | None = None) -> dict[str, Any]:
        data = await self._post(cmd, calldata)
        if not data:
            raise SwitchError(f"{self._host}: no answer to command {cmd}")
        if data.get("errcode") not in (0, None):
            raise SwitchError(
                f"{self._host}: command {cmd} error {data.get('errcode')}"
            )
        return (data.get("data") or {}).get("calldata") or {}

    async def _session_do(self, work):
        await self._login()
        try:
            return await work()
        finally:
            await self._logout()

    async def check(self) -> None:
        await self._session_do(lambda: self._cmd(CMD_VLAN))

    async def read(self) -> SwitchInfo:
        async def work() -> SwitchInfo:
            all_data = await self._cmd(CMD_ALL)
            macs = await self._cmd(CMD_MACS)
            return parse_switch(all_data, macs)

        return await self._session_do(work)

    async def export_config(self) -> dict[str, Any]:
        return await self._session_do(lambda: self._cmd(CMD_EXPORT))

    async def set_port_vlan(self, port_index: int, target: int | str) -> None:
        async def work() -> None:
            all_data = await self._cmd(CMD_ALL)
            info = parse_switch(all_data, None)
            if not 0 <= port_index < len(info.ports):
                raise SwitchError(f"no port {port_index + 1}")
            if info.ports[port_index].uplink:
                raise SwitchError(
                    f"port {port_name(port_index)} is the uplink to the router; "
                    "changing it would cut the switch off"
                )
            table = vlan_table_with(
                all_data.get("vlan") or {}, port_index, target, len(info.ports)
            )
            result = await self._cmd(CMD_VLAN, table)
            if result.get("vlan") != "ok":
                raise SwitchError(f"{self._host}: VLAN change refused")

        await self._session_do(work)

    async def _port_op(self, port_index: int, op: int, value: int) -> None:
        async def work() -> None:
            result = await self._cmd(
                CMD_PORT, {"opcode": value << 9 | port_index << 4 | op}
            )
            if result.get("config") != "ok":
                raise SwitchError(f"{self._host}: port operation refused")

        await self._session_do(work)

    async def restart_port(self, port_index: int) -> None:
        """Power-cycle a PoE device / renegotiate the link."""
        await self._port_op(port_index, OP_RESTART, 1)

    async def set_poe(self, port_index: int, enabled: bool) -> None:
        await self._port_op(port_index, OP_POE, 1 if enabled else 0)
