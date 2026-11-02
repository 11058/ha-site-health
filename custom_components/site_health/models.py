"""Plain data model. No Home Assistant imports, so tests can use it directly."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SystemInfo:
    identity: str = ""
    model: str = ""
    board: str = ""
    serial: str = ""
    version: str = ""
    firmware: str = ""
    uptime_s: int | None = None
    cpu_load: int | None = None
    memory_used_pct: float | None = None
    hdd_used_pct: float | None = None
    temperature: float | None = None
    voltage: float | None = None


@dataclass
class InterfaceInfo:
    name: str
    type: str
    running: bool
    disabled: bool
    comment: str = ""
    mac: str = ""
    rx_byte: int = 0
    tx_byte: int = 0
    link_downs: int = 0
    rx_bps: float | None = None
    tx_bps: float | None = None


@dataclass
class WanLink:
    """An uplink: a DHCP client on a WAN port (for now)."""

    interface: str
    status: str
    bound: bool
    kind: str = "dhcp"  # dhcp | pppoe | lte
    address: str = ""
    gateway: str = ""
    comment: str = ""
    disabled: bool = False
    running: bool = False
    rx_bps: float | None = None
    tx_bps: float | None = None


@dataclass
class Probe:
    """A netwatch entry: reachability of a host as seen by the router."""

    key: str
    name: str
    host: str
    up: bool | None
    since: str = ""
    rtt_ms: float | None = None
    loss_pct: float | None = None
    disabled: bool = False


@dataclass
class Tunnel:
    key: str
    name: str
    interface: str
    endpoint: str
    handshake_age_s: int | None
    connected: bool
    # Dial-in peer (no endpoint configured): a VPN client such as the admin's
    # phone. Its absence is normal, so it is shown but never alarmed on.
    dial_in: bool = False
    rx_byte: int = 0
    tx_byte: int = 0


@dataclass
class RadioLink:
    """One wireless association seen from this device's side."""

    key: str
    interface: str
    mac: str
    ssid: str = ""
    signal_dbm: int | None = None
    tx_rate_mbps: float | None = None
    rx_rate_mbps: float | None = None
    uptime_s: int | None = None
    band: str = ""
    peer_name: str = ""
    rx_bps: float | None = None
    tx_bps: float | None = None


@dataclass
class Segment:
    key: str
    kind: str
    name: str
    interface: str
    network: str
    vlan_id: int | None = None


@dataclass
class Client:
    mac: str
    ip: str = ""
    hostname: str = ""
    comment: str = ""
    segment: str = ""
    segment_kind: str = ""
    interface: str = ""
    wifi_interface: str = ""
    ssid: str = ""
    signal_dbm: int | None = None
    online: bool = False
    last_seen_s: int | None = None
    static_lease: bool = False
    family: bool = False
    admin: bool = False
    family_entry_id: str = ""
    access_vlan: int | None = None
    wireless: bool = False
    wifi_uptime_s: int | None = None
    # Locally administered MAC: a phone's per-network "private address".
    random_mac: bool = False


@dataclass
class Neighbor:
    mac: str
    identity: str
    address: str
    interface: str
    platform: str = ""
    board: str = ""
    version: str = ""


@dataclass
class ModemInfo:
    """A cellular router in front of the core router (Tandem-4GT and alike)."""

    reachable: bool = False
    error: str = ""
    connected: bool | None = None
    connecting: bool = False
    uptime_s: int | None = None
    active_sim: int | None = None
    sim_slots: int | None = None
    sim_status: str = ""
    operator: str = ""
    iccid: str = ""
    registration: str = ""
    registered: bool | None = None
    roaming: bool = False
    tech: str = ""
    band: str = ""
    bandwidth_mhz: float | None = None
    rsrp: float | None = None
    rsrq: float | None = None
    rssi: float | None = None
    sinr: float | None = None
    signal_pct: int | None = None
    cell_id: str = ""
    temperature: float | None = None
    # What each slot looked like the last time it was the active one.
    last_by_slot: dict[str, dict] = field(default_factory=dict)


@dataclass
class DeviceSnapshot:
    """Everything polled from one RouterOS device."""

    system: SystemInfo = field(default_factory=SystemInfo)
    interfaces: dict[str, InterfaceInfo] = field(default_factory=dict)
    radio_links: dict[str, RadioLink] = field(default_factory=dict)


@dataclass
class SiteSnapshot(DeviceSnapshot):
    """The core router adds the site-wide view."""

    wans: dict[str, WanLink] = field(default_factory=dict)
    probes: dict[str, Probe] = field(default_factory=dict)
    tunnels: dict[str, Tunnel] = field(default_factory=dict)
    segments: dict[str, Segment] = field(default_factory=dict)
    clients: dict[str, Client] = field(default_factory=dict)
    neighbors: dict[str, Neighbor] = field(default_factory=dict)
    caps: list[dict[str, Any]] = field(default_factory=list)


def to_dict(obj: Any) -> Any:
    return asdict(obj)
