"""Uplinks of any kind and how traffic chooses between them.

Sites differ a lot: PPPoE only (Yug), PPPoE plus an LTE box on a static /30
behind recursive default routes and policy routing (BitBarn), a USB LTE modem
plus a spare DHCP port (Palniki), DHCP behind a cellular router (Beregovaya).
This module turns the router's tables into one picture:

* every uplink, whatever its kind (pppoe, dhcp, lte, static WAN port);
* for each, its default routes per routing table, with recursive gateways
  ("0.0.0.0/0 via 77.88.8.8", where 77.88.8.8/32 goes out ISP1) resolved;
* its role in the main table: active, backup, used only by policy routing;
* default routes that are not uplinks at all (DPI containers, tunnels);
* the policy-routing rules (mangle mark-routing) and whether they are on.

Pure functions; no Home Assistant imports.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

Row = dict[str, Any]

MAIN = "main"


def _b(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def num(value: Any) -> float | None:
    """'-97dBm', '-12.5', '15dB' -> number."""
    if value is None:
        return None
    m = _NUM.search(str(value))
    return float(m.group(0)) if m else None


@dataclass
class UplinkRoute:
    table: str
    distance: int
    active: bool
    gateway: str
    check_gateway: str = ""
    via: str = ""  # the recursive hop, e.g. "77.88.8.8"
    comment: str = ""


@dataclass
class Uplink:
    interface: str
    kind: str  # pppoe | dhcp | lte | static
    status: str
    bound: bool
    address: str = ""
    gateway: str = ""
    comment: str = ""
    disabled: bool = False
    running: bool = False
    role: str = "no-route"  # active | backup | pbr | no-route
    routes: list[UplinkRoute] = field(default_factory=list)
    lte: dict[str, Any] | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None


@dataclass
class ServiceRoute:
    """A default route that is not an uplink: DPI container, tunnel, ..."""

    table: str
    gateway: str
    active: bool
    comment: str = ""


@dataclass
class RouteRule:
    """A mangle rule that sends some traffic to a routing table."""

    table: str
    match: str
    disabled: bool
    comment: str = ""


def wan_lists(lists: Iterable[str]) -> set[str]:
    return {n for n in lists if "wan" in n.lower()}


def _addr_of(addresses: Iterable[Row]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in addresses:
        if _b(row.get("disabled")) or _b(row.get("invalid")):
            continue
        out.setdefault(str(row.get("interface", "")), str(row.get("address", "")))
    return out


def build_uplinks(
    *,
    dhcp_clients: Iterable[Row],
    pppoe_clients: Iterable[Row],
    lte: Iterable[Row],
    addresses: Iterable[Row],
    running: dict[str, bool],
    comments: dict[str, str],
    wan_members: Iterable[Row] = (),
) -> dict[str, Uplink]:
    addr = _addr_of(addresses)
    ups: dict[str, Uplink] = {}

    def put(iface: str, **kw: Any) -> Uplink:
        up = ups.get(iface)
        if up is None:
            up = Uplink(
                interface=iface,
                kind=kw.pop("kind"),
                status=kw.pop("status"),
                bound=kw.pop("bound"),
            )
            ups[iface] = up
        else:
            kw.pop("kind", None)  # the more specific kind (lte, pppoe) wins
            if not kw.get("bound"):
                kw.pop("status", None)
                kw.pop("bound", None)
        for key, value in kw.items():
            if value not in ("", None):
                setattr(up, key, value)
        up.running = running.get(iface, up.running)
        up.comment = up.comment or comments.get(iface, "")
        return up

    for row in lte:
        name = str(row.get("name", ""))
        if not name:
            continue
        dis = _b(row.get("disabled"))
        run = _b(row.get("running"))
        put(
            name,
            kind="lte",
            status="disabled" if dis else "connected" if run else "down",
            bound=run and not dis,
            disabled=dis,
            address=addr.get(name, "").split("/")[0],
            comment=str(row.get("comment", "")),
        )
    for row in pppoe_clients:
        name = str(row.get("name", ""))
        if not name:
            continue
        dis = _b(row.get("disabled"))
        run = _b(row.get("running"))
        put(
            name,
            kind="pppoe",
            status="disabled" if dis else "connected" if run else "down",
            bound=run and not dis,
            disabled=dis,
            address=addr.get(name, "").split("/")[0],
            gateway=str(row.get("interface", "")),
            comment=str(row.get("comment", "")),
        )
    for row in dhcp_clients:
        iface = str(row.get("interface", ""))
        if not iface:
            continue
        status = str(row.get("status", "")) or "unknown"
        put(
            iface,
            kind="dhcp",
            status=status,
            bound=status == "bound",
            disabled=_b(row.get("disabled")),
            address=str(row.get("address", "")).split("/")[0],
            gateway=str(row.get("gateway", "")),
            comment=str(row.get("comment", "")),
        )
    # A WAN-list port with a plain address: BitBarn's static /30 to its LTE box.
    for row in wan_members:
        iface = str(row.get("interface", ""))
        if not iface or iface in ups or _b(row.get("disabled")):
            continue
        if iface not in addr:
            continue
        run = running.get(iface, False)
        put(
            iface,
            kind="static",
            status="up" if run else "down",
            bound=run,
            address=addr[iface].split("/")[0],
        )
    return ups


def _iface_of_gateway(gw: str, ups: dict[str, Uplink]) -> str | None:
    """'ISP1' / '10.100.250.2%sfp-sfpplus11' / an address in an uplink's subnet."""
    if "%" in gw:
        return gw.split("%", 1)[1]
    if gw in ups:
        return gw
    try:
        ip = ipaddress.ip_address(gw)
    except ValueError:
        return None
    for name, up in ups.items():
        if up.gateway == gw:
            return name
        if up.address:
            try:
                net = ipaddress.ip_interface(
                    up.address if "/" in up.address else f"{up.address}/30"
                ).network
            except ValueError:
                continue
            if ip in net and up.kind == "static":
                return name
    return None


def attach_routes(
    ups: dict[str, Uplink],
    default_routes: Iterable[Row],
    host_routes: Iterable[Row],
    static_addresses: dict[str, str] | None = None,
) -> list[ServiceRoute]:
    """Give every uplink its default routes and role; return the rest."""
    hosts: dict[str, list[Row]] = {}
    for row in host_routes:
        dst = str(row.get("dst-address", "")).split("/")[0]
        hosts.setdefault(dst, []).append(row)
    if static_addresses:
        for name, cidr in static_addresses.items():
            if name in ups and "/" in cidr:
                ups[name].address = cidr  # keep the prefix for subnet matching

    service: list[ServiceRoute] = []
    for row in default_routes:
        if _b(row.get("disabled")):
            continue
        table = str(row.get("routing-table") or MAIN)
        gw = str(row.get("immediate-gw") or row.get("gateway") or "")
        via = ""
        iface = _iface_of_gateway(gw, ups)
        if iface is None:
            gw_plain = str(row.get("gateway", ""))
            for _ in range(3):  # recursive gateway: follow the /32 host route
                hop = hosts.get(gw_plain)
                if not hop:
                    break
                same = [h for h in hop if str(h.get("routing-table") or MAIN) == table]
                chosen = (same or hop)[0]
                via = via or gw_plain
                nxt = str(chosen.get("immediate-gw") or chosen.get("gateway") or "")
                iface = _iface_of_gateway(nxt, ups)
                if iface:
                    break
                gw_plain = nxt
        active = _b(row.get("active"))
        route = UplinkRoute(
            table=table,
            distance=int(num(row.get("distance")) or 1),
            active=active,
            gateway=str(row.get("gateway", "")),
            check_gateway=str(row.get("check-gateway", "")),
            via=via,
            comment=str(row.get("comment", "")),
        )
        if iface and iface in ups:
            ups[iface].routes.append(route)
        else:
            service.append(
                ServiceRoute(
                    table=table,
                    gateway=route.gateway,
                    active=active,
                    comment=route.comment,
                )
            )
    for up in ups.values():
        if "/" in up.address:
            up.address = up.address.split("/")[0]
        up.routes.sort(key=lambda r: (r.table != MAIN, r.table, r.distance))
        main = [r for r in up.routes if r.table == MAIN]
        if any(r.active for r in main):
            up.role = "active"
        elif main:
            up.role = "backup"
        elif up.routes:
            up.role = "pbr"
        else:
            up.role = "no-route"
    return service


def parse_route_rules(mangle: Iterable[Row]) -> list[RouteRule]:
    rules = []
    for row in mangle:
        if str(row.get("action")) != "mark-routing":
            continue
        parts = []
        for key in (
            "in-interface",
            "in-interface-list",
            "src-address",
            "src-address-list",
            "dst-address-list",
            "connection-mark",
        ):
            if row.get(key):
                parts.append(f"{key}={row[key]}")
        rules.append(
            RouteRule(
                table=str(row.get("new-routing-mark", "")),
                match=", ".join(parts) or "all",
                disabled=_b(row.get("disabled")),
                comment=str(row.get("comment", "")),
            )
        )
    return rules


def parse_lte_monitor(row: Row) -> dict[str, Any]:
    """RouterOS /interface/lte/monitor once -> plain signal facts."""
    rsrp, sinr = num(row.get("rsrp")), num(row.get("sinr"))
    if rsrp is None:
        quality = "unknown"
    elif rsrp >= -90 and (sinr is None or sinr >= 10):
        quality = "good"
    elif rsrp >= -105 and (sinr is None or sinr >= 0):
        quality = "fair"
    else:
        quality = "poor"
    return {
        "registration": str(row.get("registration-status", "")),
        "operator": str(row.get("current-operator") or row.get("operator") or ""),
        "tech": str(row.get("access-technology", "")),
        "band": str(row.get("primary-band", "")).split("@")[0].strip(),
        "rsrp": rsrp,
        "rsrq": num(row.get("rsrq")),
        "sinr": sinr,
        "rssi": num(row.get("rssi")),
        "cell": str(row.get("current-cellid", "")),
        "quality": quality,
    }
