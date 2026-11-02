"""Turn raw RouterOS REST rows into the data model.

RouterOS REST returns every value as a string ("true", "-61", "1w2d3h4m5s",
"866.6Mbps-80MHz/2S/SGI"). All the string handling is here, free of Home
Assistant, so it can be exercised with fixtures captured from real devices.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Any

from .const import (
    CLIENT_ONLINE_SECONDS,
    SEG_OTHER,
    SEGMENT_BY_NAME_HINT,
    SEGMENT_BY_VLAN,
)
from .models import (
    Client,
    InterfaceInfo,
    Neighbor,
    Probe,
    RadioLink,
    Segment,
    SystemInfo,
    Tunnel,
    WanLink,
)

Row = dict[str, Any]

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(w|d|h|ms|us|ns|m|s)")
_CLOCK = re.compile(r"^(?:(\d+)d)?\s*(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?$")
_UNIT_SECONDS = {
    "w": 604800,
    "d": 86400,
    "h": 3600,
    "m": 60,
    "s": 1,
    "ms": 0.001,
    "us": 0.000001,
    "ns": 0.000000001,
}
_RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kMG]?)bps", re.IGNORECASE)


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).split("@")[0].strip()))
    except ValueError:
        return None


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def parse_duration(value: Any) -> float | None:
    """'1w2d3h4m5s', '4m12s', '12ms345us', '1d02:03:04', '00:00:30' -> seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("never", "none"):
        return None
    clock = _CLOCK.match(text)
    if clock:
        days, hours, minutes, seconds = clock.groups()
        return (
            int(days or 0) * 86400
            + int(hours) * 3600
            + int(minutes) * 60
            + int(seconds)
        )
    total = 0.0
    matched = False
    for number, unit in _DURATION_PART.findall(text):
        total += float(number) * _UNIT_SECONDS[unit]
        matched = True
    return total if matched else None


def parse_duration_int(value: Any) -> int | None:
    seconds = parse_duration(value)
    return int(seconds) if seconds is not None else None


def parse_rtt_ms(value: Any) -> float | None:
    seconds = parse_duration(value)
    return round(seconds * 1000, 2) if seconds is not None else None


def parse_rate_mbps(value: Any) -> float | None:
    """'866.6Mbps-80MHz/2S/SGI' -> 866.6; '1.2Gbps' -> 1200.0."""
    if not value:
        return None
    match = _RATE.match(str(value))
    if not match:
        return None
    number, prefix = float(match.group(1)), match.group(2).upper()
    factor = {"": 0.000001, "K": 0.001, "M": 1.0, "G": 1000.0}[prefix]
    return round(number * factor, 1)


def norm_mac(mac: Any) -> str:
    return str(mac or "").strip().upper()


# --- system -------------------------------------------------------------------


def parse_system(
    identity: Row, resource: Row, routerboard: Row, health: Iterable[Row]
) -> SystemInfo:
    info = SystemInfo(
        identity=str(identity.get("name", "")),
        model=str(routerboard.get("model") or resource.get("board-name", "")),
        board=str(resource.get("board-name", "")),
        serial=str(routerboard.get("serial-number", "")),
        version=str(resource.get("version", "")).split(" ")[0],
        firmware=str(routerboard.get("current-firmware", "")),
        uptime_s=parse_duration_int(resource.get("uptime")),
        cpu_load=as_int(resource.get("cpu-load")),
    )
    total = as_int(resource.get("total-memory"))
    free = as_int(resource.get("free-memory"))
    if total and free is not None:
        info.memory_used_pct = round((total - free) * 100 / total, 1)
    total = as_int(resource.get("total-hdd-space"))
    free = as_int(resource.get("free-hdd-space"))
    if total and free is not None:
        info.hdd_used_pct = round((total - free) * 100 / total, 1)

    temps: dict[str, float] = {}
    for row in health:
        name = str(row.get("name", ""))
        value = as_float(row.get("value"))
        if value is None:
            continue
        if "temperature" in name:
            temps[name] = value
        elif name == "voltage":
            info.voltage = value
    # Prefer the CPU sensor; boards without one report board/sfp temperatures.
    for preferred in ("cpu-temperature", "temperature", "board-temperature1"):
        if preferred in temps:
            info.temperature = temps[preferred]
            break
    else:
        if temps:
            info.temperature = max(temps.values())
    return info


# --- interfaces ---------------------------------------------------------------


def parse_interfaces(rows: Iterable[Row]) -> dict[str, InterfaceInfo]:
    result: dict[str, InterfaceInfo] = {}
    for row in rows:
        name = str(row.get("name", ""))
        if not name:
            continue
        result[name] = InterfaceInfo(
            name=name,
            type=str(row.get("type", "")),
            running=as_bool(row.get("running")),
            disabled=as_bool(row.get("disabled")),
            comment=str(row.get("comment", "")),
            mac=norm_mac(row.get("mac-address")),
            rx_byte=as_int(row.get("rx-byte")) or 0,
            tx_byte=as_int(row.get("tx-byte")) or 0,
            link_downs=as_int(row.get("link-downs")) or 0,
        )
    return result


def apply_rates(
    current: dict[str, InterfaceInfo],
    previous: dict[str, InterfaceInfo] | None,
    elapsed_s: float | None,
) -> None:
    """Fill rx/tx bits per second from the byte counters of the last poll."""
    if not previous or not elapsed_s or elapsed_s <= 0:
        return
    for name, iface in current.items():
        old = previous.get(name)
        if old is None:
            continue
        drx = iface.rx_byte - old.rx_byte
        dtx = iface.tx_byte - old.tx_byte
        # Counter reset (reboot) — skip one sample rather than show a spike.
        if drx < 0 or dtx < 0:
            continue
        iface.rx_bps = round(drx * 8 / elapsed_s, 1)
        iface.tx_bps = round(dtx * 8 / elapsed_s, 1)


# --- uplinks, probes, tunnels -------------------------------------------------


def parse_wans(
    dhcp_clients: Iterable[Row], interfaces: dict[str, InterfaceInfo]
) -> dict[str, WanLink]:
    result: dict[str, WanLink] = {}
    for row in dhcp_clients:
        iface = str(row.get("interface", ""))
        if not iface:
            continue
        status = str(row.get("status", "")) or "unknown"
        info = interfaces.get(iface)
        result[iface] = WanLink(
            interface=iface,
            status=status,
            bound=status == "bound",
            address=str(row.get("address", "")).split("/")[0],
            gateway=str(row.get("gateway", "")),
            comment=str(row.get("comment", "") or (info.comment if info else "")),
            disabled=as_bool(row.get("disabled")),
            running=info.running if info else False,
            rx_bps=info.rx_bps if info else None,
            tx_bps=info.tx_bps if info else None,
        )
    return result


def parse_probes(rows: Iterable[Row]) -> dict[str, Probe]:
    result: dict[str, Probe] = {}
    for row in rows:
        host = str(row.get("host", ""))
        if not host:
            continue
        name = str(row.get("name") or row.get("comment") or host)
        key = slug(str(row.get("name") or host))
        status = str(row.get("status", "")).lower()
        result[key] = Probe(
            key=key,
            name=name,
            host=host,
            up=True if status == "up" else False if status == "down" else None,
            since=str(row.get("since", "")),
            rtt_ms=parse_rtt_ms(row.get("rtt-avg")),
            loss_pct=as_float(row.get("loss-percent")),
            disabled=as_bool(row.get("disabled")),
        )
    return result


def parse_tunnels(rows: Iterable[Row], stale_s: int) -> dict[str, Tunnel]:
    result: dict[str, Tunnel] = {}
    for row in rows:
        if as_bool(row.get("disabled")):
            continue
        name = str(row.get("name") or row.get("comment") or "")
        pubkey = str(row.get("public-key", ""))
        key = slug(name or pubkey[:8])
        age = parse_duration_int(row.get("last-handshake"))
        endpoint = str(
            row.get("current-endpoint-address") or row.get("endpoint-address") or ""
        )
        result[key] = Tunnel(
            key=key,
            name=name or pubkey[:8],
            interface=str(row.get("interface", "")),
            endpoint=endpoint,
            handshake_age_s=age,
            connected=age is not None and age <= stale_s,
            rx_byte=as_int(row.get("rx")) or 0,
            tx_byte=as_int(row.get("tx")) or 0,
        )
    return result


# --- radio --------------------------------------------------------------------


def parse_radio_links(
    registrations: Iterable[Row], peer_names: dict[str, str] | None = None
) -> dict[str, RadioLink]:
    peer_names = peer_names or {}
    result: dict[str, RadioLink] = {}
    for row in registrations:
        mac = norm_mac(row.get("mac-address"))
        iface = str(row.get("interface", ""))
        if not mac:
            continue
        key = slug(f"{iface}_{mac}")
        result[key] = RadioLink(
            key=key,
            interface=iface,
            mac=mac,
            ssid=str(row.get("ssid", "")),
            signal_dbm=as_int(row.get("signal") or row.get("signal-strength")),
            tx_rate_mbps=parse_rate_mbps(row.get("tx-rate")),
            rx_rate_mbps=parse_rate_mbps(row.get("rx-rate")),
            uptime_s=parse_duration_int(row.get("uptime")),
            band=str(row.get("band", "")),
            peer_name=peer_names.get(mac, ""),
        )
    return result


def parse_neighbors(rows: Iterable[Row]) -> dict[str, Neighbor]:
    result: dict[str, Neighbor] = {}
    for row in rows:
        mac = norm_mac(row.get("mac-address"))
        if not mac:
            continue
        result[mac] = Neighbor(
            mac=mac,
            identity=str(row.get("identity", "")),
            address=str(row.get("address", "")).split(",")[0].strip(),
            interface=str(row.get("interface", "")).split(",")[0],
            platform=str(row.get("platform", "")),
            board=str(row.get("board", "")),
            version=str(row.get("version", "")).split(" ")[0],
        )
    return result


def mikrotik_candidates(neighbors: dict[str, Neighbor]) -> list[Neighbor]:
    """Neighbors worth offering in the 'add device' flow, one per address."""
    seen: dict[str, Neighbor] = {}
    for nb in neighbors.values():
        if "mikrotik" not in nb.platform.lower() or not nb.address:
            continue
        try:
            ipaddress.ip_address(nb.address)
        except ValueError:
            continue
        seen.setdefault(nb.address, nb)
    return sorted(seen.values(), key=lambda n: ipaddress.ip_address(n.address))


# --- segments and clients -----------------------------------------------------


def segment_kind(vlan_id: int | None, *names: str) -> str:
    if vlan_id is not None and vlan_id in SEGMENT_BY_VLAN:
        return SEGMENT_BY_VLAN[vlan_id]
    lowered = " ".join(n.lower() for n in names if n)
    for hint, kind in SEGMENT_BY_NAME_HINT:
        if hint in lowered:
            return kind
    return SEG_OTHER


def parse_segments(
    addresses: Iterable[Row],
    vlans: Iterable[Row],
    interfaces: dict[str, InterfaceInfo],
    exclude_interfaces: set[str],
) -> dict[str, Segment]:
    vlan_ids = {
        str(v.get("name", "")): as_int(v.get("vlan-id")) for v in vlans if v.get("name")
    }
    result: dict[str, Segment] = {}
    for row in addresses:
        if as_bool(row.get("disabled")) or as_bool(row.get("invalid")):
            continue
        iface = str(row.get("interface", ""))
        if not iface or iface in exclude_interfaces:
            continue
        info = interfaces.get(iface)
        if info and info.type == "wg":
            continue
        try:
            network = ipaddress.ip_interface(str(row.get("address", ""))).network
        except ValueError:
            continue
        if network.prefixlen >= 31:  # point-to-point links are not segments
            continue
        vlan_id = vlan_ids.get(iface)
        if vlan_id is None and not info:
            continue
        if vlan_id is None and network.is_private and network.prefixlen == 24:
            # 10.N.<vlan>.0/24 convention: the bridge itself carries VLAN 1.
            third = int(str(network.network_address).split(".")[2])
            vlan_id = third if third in SEGMENT_BY_VLAN else None
        comment = info.comment if info else ""
        key = str(network)
        result[key] = Segment(
            key=key,
            kind=segment_kind(vlan_id, iface, comment),
            name=iface,
            interface=iface,
            network=key,
            vlan_id=vlan_id,
        )
    return result


def segment_for_ip(ip: str, segments: dict[str, Segment]) -> Segment | None:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for seg in segments.values():
        if addr in ipaddress.ip_network(seg.network):
            return seg
    return None


_ARP_ONLINE = {"reachable", "delay", "probe", "permanent"}


def build_clients(
    *,
    leases: Iterable[Row],
    arp: Iterable[Row],
    registrations: Iterable[Row],
    access_list: Iterable[Row],
    segments: dict[str, Segment],
    family_vlan: int,
    exclude_macs: set[str] | None = None,
) -> dict[str, Client]:
    exclude_macs = exclude_macs or set()
    clients: dict[str, Client] = {}

    def get(mac: str) -> Client:
        if mac not in clients:
            clients[mac] = Client(mac=mac)
        return clients[mac]

    for row in leases:
        mac = norm_mac(row.get("mac-address"))
        if not mac or as_bool(row.get("disabled")):
            continue
        client = get(mac)
        client.ip = str(row.get("active-address") or row.get("address") or "")
        client.hostname = str(row.get("host-name", ""))
        client.comment = str(row.get("comment", ""))
        client.static_lease = not as_bool(row.get("dynamic"))
        last_seen = parse_duration_int(row.get("last-seen"))
        client.last_seen_s = last_seen
        if str(row.get("status", "")) == "bound" and (
            last_seen is not None and last_seen <= CLIENT_ONLINE_SECONDS
        ):
            client.online = True

    for row in arp:
        mac = norm_mac(row.get("mac-address"))
        ip = str(row.get("address", ""))
        if not mac or not ip or as_bool(row.get("invalid")):
            continue
        status = str(row.get("status", "")).lower()
        complete = as_bool(row.get("complete")) or status in _ARP_ONLINE
        if not complete and mac not in clients:
            continue
        client = get(mac)
        if not client.ip:
            client.ip = ip
        client.interface = str(row.get("interface", ""))
        if status in _ARP_ONLINE:
            client.online = True

    for row in registrations:
        mac = norm_mac(row.get("mac-address"))
        if not mac:
            continue
        client = get(mac)
        client.wireless = True
        client.online = True
        client.wifi_interface = str(row.get("interface", ""))
        client.ssid = str(row.get("ssid", ""))
        client.signal_dbm = as_int(row.get("signal") or row.get("signal-strength"))
        client.last_seen_s = 0

    for row in access_list:
        mac = norm_mac(row.get("mac-address"))
        if not mac or as_bool(row.get("disabled")):
            continue
        if str(row.get("action", "accept")) != "accept":
            continue
        vlan = as_int(row.get("vlan-id"))
        client = get(mac)
        client.access_vlan = vlan
        if not client.comment:
            client.comment = str(row.get("comment", ""))
        if vlan == family_vlan:
            client.family = True
            client.family_entry_id = str(row.get(".id", ""))

    for mac in list(clients):
        if mac in exclude_macs:
            del clients[mac]
            continue
        client = clients[mac]
        seg = segment_for_ip(client.ip, segments) if client.ip else None
        if seg:
            client.segment = seg.key
            client.segment_kind = seg.kind
    return clients


# --- helpers ------------------------------------------------------------------

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: str) -> str:
    return _SLUG.sub("_", translit(text).lower()).strip("_") or "x"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip


def translit(text: str) -> str:
    """RouterOS drops Cyrillic from comments typed via CLI/REST: transliterate."""
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            rep = _TRANSLIT[low]
            out.append(rep.capitalize() if ch != low and rep else rep)
        else:
            out.append(ch)
    return "".join(out)


def family_comment(name: str, vlan_id: int) -> str:
    """Comment for a family access-list entry: ASCII, bounded, recognisable."""
    clean = re.sub(r"[^\x20-\x7e]", "", translit(name)).strip()
    clean = clean.replace('"', "'")[:48]
    label = f"Family: {clean}" if clean else "Family"
    return f"{label} -> VLAN {vlan_id} (HA)"
