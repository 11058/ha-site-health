"""Admin VPN: one-click WireGuard peers on the core router's admin interface.

The key pair is made only when the admin presses "Create". The private key
goes back once, inside the client config shown in the panel, and is stored
nowhere: not in Home Assistant, not on the router. The router gets only the
public key, a /32 inside the admin VLAN and a route to it.

Pure helpers first (tested without a router), router calls at the end.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import re
from collections.abc import Iterable
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .parsers import as_bool

TAG = "site-health: admin VPN"
KEEPALIVE = 25

Row = dict[str, Any]


def gen_keypair() -> tuple[str, str]:
    """(private, public), base64 like `wg genkey | wg pubkey`."""
    key = X25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        [
            *"abvgdee",
            "zh",
            *"zi",
            "y",
            *"klmnoprstufh",
            "ts",
            "ch",
            "sh",
            "sch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ],
        strict=True,
    )
)


def ascii_name(name: str) -> str:
    """RouterOS keeps names and comments in ASCII only: transliterate."""
    out = []
    for ch in name.strip():
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch != low else t)
        else:
            out.append(ch)
    text = re.sub(r"[^A-Za-z0-9 ._-]+", "", "".join(out))
    return re.sub(r"\s+", " ", text).strip()[:40]


def peer_name(name: str, ip: str = "") -> str:
    """ASCII peer name, unique on the router thanks to the address's last byte."""
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name(name).lower()).strip("-")
    tail = f"-{ip.rsplit('.', 1)[-1]}" if ip else ""
    return f"adm-{(slug or 'device')[:30]}{tail}"


def _networks(value: str) -> list[ipaddress.IPv4Network]:
    nets = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return [n for n in nets if n.version == 4]


def find_admin_interface(
    wg_interfaces: Iterable[Row], peers: Iterable[Row], admin_net: str
) -> Row | None:
    """The WireGuard interface admins dial in to.

    By name first ("wg-Admin", "wg-admin"), then the one whose peers sit inside
    the admin VLAN. Disabled interfaces are skipped.
    """
    ifaces = [r for r in wg_interfaces if not as_bool(r.get("disabled"))]
    for row in ifaces:
        if "admin" in str(row.get("name", "")).lower():
            return row
    try:
        net = ipaddress.ip_network(admin_net)
    except ValueError:
        return None
    hits: dict[str, int] = {}
    for p in peers:
        if any(n.subnet_of(net) for n in _networks(p.get("allowed-address", ""))):
            hits[str(p.get("interface", ""))] = (
                hits.get(str(p.get("interface", "")), 0) + 1
            )
    for row in sorted(ifaces, key=lambda r: -hits.get(str(r.get("name")), 0)):
        if hits.get(str(row.get("name"))):
            return row
    return None


def _pool_ranges(pools: Iterable[Row]) -> list[tuple[int, int]]:
    out = []
    for row in pools:
        for part in str(row.get("ranges", "")).split(","):
            part = part.strip()
            if not part:
                continue
            a, _, b = part.partition("-")
            try:
                lo = int(ipaddress.ip_address(a.strip()))
                hi = int(ipaddress.ip_address((b or a).strip()))
            except ValueError:
                continue
            out.append((lo, hi))
    return out


def free_address(
    admin_net: str,
    *,
    peers: Iterable[Row],
    leases: Iterable[Row] = (),
    addresses: Iterable[Row] = (),
    pools: Iterable[Row] = (),
) -> str | None:
    """A free host in the admin VLAN, outside every DHCP pool.

    The sites keep VPN clients at the top of the admin /24 (.250 laptop,
    .251 phone), so look there first and then walk down.
    """
    net = ipaddress.ip_network(admin_net)
    used: set[int] = set()
    for p in peers:
        # Only addresses inside the admin network count: a tunnel peer allowed
        # 10.0.0.0/8 or 0.0.0.0/0 does not take every host of it.
        for n in _networks(p.get("allowed-address", "")):
            if n.subnet_of(net):
                used.update(int(a) for a in n)
    for row in leases:
        with contextlib.suppress(ValueError):
            used.add(int(ipaddress.ip_address(str(row.get("address", "")))))
    for row in addresses:
        with contextlib.suppress(ValueError):
            used.add(int(ipaddress.ip_interface(str(row.get("address", ""))).ip))
    ranges = _pool_ranges(pools)
    hosts = [int(h) for h in net.hosts()]
    if not hosts:
        return None
    top = hosts[-1]
    preferred = [h for h in hosts if h >= top - 4]  # .250-.254 in a /24
    rest = sorted((h for h in hosts if h < top - 4), reverse=True)
    for h in preferred + rest:
        if h in used or any(lo <= h <= hi for lo, hi in ranges):
            continue
        return str(ipaddress.ip_address(h))
    return None


def site_allowed_ips(networks: Iterable[str], admin_net: str) -> str:
    """AllowedIPs for the client: the site's /16 when everything fits in it.

    Never 0.0.0.0/0: a full tunnel takes the client's own LAN and DNS.
    """
    nets = {n for v in [*networks, admin_net] for n in _networks(v)}
    nets = {n for n in nets if n.prefixlen > 0}
    supers = {n.supernet(new_prefix=16) if n.prefixlen > 16 else n for n in nets}
    if len(supers) == 1:
        return str(next(iter(supers)))
    return ", ".join(str(n) for n in ipaddress.collapse_addresses(sorted(nets)))


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return addr.is_global and addr not in cgnat


def guess_endpoint(cloud: Row, uplink_addresses: Iterable[str]) -> tuple[str, str]:
    """(host, why). Empty host when the site has no address reachable from outside."""
    if as_bool(cloud.get("ddns-enabled")) and cloud.get("dns-name"):
        return str(cloud["dns-name"]), "имя MikroTik Cloud (DDNS)"
    for ip in uplink_addresses:
        if _public(ip):
            return ip.split("/")[0], "внешний адрес канала"
    return "", "у объекта нет внешнего адреса (за NAT провайдера или модема)"


def input_open(rules: Iterable[Row], port: int) -> bool | None:
    """Does the input chain let the WireGuard port in? None = no rule seen."""
    for r in rules:
        if r.get("chain") != "input" or as_bool(r.get("dynamic")):
            continue
        if str(r.get("protocol", "")) != "udp":
            continue
        ports = str(r.get("dst-port", ""))
        if str(port) not in re.split(r"[,\s]+", ports):
            continue
        if r.get("action") == "accept":
            return not as_bool(r.get("disabled"))
    return None


def client_config(
    *,
    private_key: str,
    address: str,
    dns: str,
    server_public_key: str,
    endpoint: str,
    port: int,
    allowed_ips: str,
    name: str = "",
) -> str:
    lines = []
    if name:
        lines.append(f"# {name}")
    lines += [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}/32",
    ]
    if dns:
        lines.append(f"DNS = {dns}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {server_public_key}",
        f"AllowedIPs = {allowed_ips}",
    ]
    if endpoint:
        host = f"[{endpoint}]" if ":" in endpoint else endpoint
        lines.append(f"Endpoint = {host}:{port}")
    lines.append(f"PersistentKeepalive = {KEEPALIVE}")
    return "\n".join(lines) + "\n"


def qr_svg(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_inline(
        scale=5, border=2, dark="#000", light="#fff"
    )


def peer_view(row: Row, ours_tag: str = TAG) -> dict[str, Any]:
    comment = str(row.get("comment", ""))
    return {
        "id": row.get(".id"),
        "name": str(row.get("name", "")),
        "comment": comment,
        "address": str(row.get("allowed-address", "")),
        "ours": comment.startswith(ours_tag),
        "disabled": as_bool(row.get("disabled")),
        "last_handshake": str(row.get("last-handshake", "")),
    }


# --- router side ----------------------------------------------------------


def derive_admin_net(addresses: Iterable[Row], peers: Iterable[Row]) -> str:
    """The router's admin network when no VLAN number says so (a hub).

    It is the address whose network holds the admin interface's peers; failing
    that, an address on an interface named like the admin VLAN.
    """
    nets: list[tuple[ipaddress.IPv4Network, str]] = []
    for a in addresses:
        if as_bool(a.get("disabled")):
            continue
        try:
            net = ipaddress.ip_interface(str(a.get("address", ""))).network
        except ValueError:
            continue
        if net.version == 4 and net.prefixlen <= 29:
            nets.append((net, str(a.get("interface", "")).lower()))
    best, hits = "", 0
    for net, _iface in nets:
        n = sum(
            1
            for p in peers
            for pn in _networks(p.get("allowed-address", ""))
            if pn.prefixlen == 32 and pn.subnet_of(net)
        )
        if n > hits:
            best, hits = str(net), n
    if best:
        return best
    for net, iface in nets:
        if "admin" in iface or "99" in iface:
            return str(net)
    return ""


def _scope(site: str) -> str:
    return f"{TAG} [{ascii_name(site)}]" if site else TAG


def _mine(row: Row, site: str) -> bool:
    return str(row.get("comment", "")).startswith(_scope(site))


async def _read(client, admin_net: str | None):
    ifaces = await client._list("interface/wireguard", optional=True)
    peers = await client.wireguard_peers()
    addresses = await client.addresses()
    if not admin_net:
        admin_peers = [
            p
            for p in peers
            if any(
                "admin" in str(i.get("name", "")).lower()
                and i.get("name") == p.get("interface")
                for i in ifaces
            )
        ]
        admin_net = derive_admin_net(addresses, admin_peers)
    iface = find_admin_interface(ifaces, peers, admin_net) if admin_net else None
    return ifaces, peers, addresses, admin_net, iface


def _public_addresses(addresses: Iterable[Row]) -> list[str]:
    return [
        str(a.get("address", "")).split("/")[0]
        for a in addresses
        if not as_bool(a.get("disabled")) and _public(str(a.get("address", "")))
    ]


async def async_state(
    client,
    admin_net: str | None,
    uplink_addresses: list[str] | None = None,
    *,
    site: str = "",
    endpoint: str = "",
) -> dict[str, Any]:
    """What the panel shows before anything is created.

    `site` is set when this router is a gateway serving another site: then only
    the peers made for that site are listed, never the gateway's own ones.
    """
    _ifaces, peers, addresses, admin_net, iface = await _read(client, admin_net)
    if iface is None:
        return {"available": False, "admin_net": admin_net or "",
                "reason": "на роутере нет админского WireGuard-интерфейса"}  # fmt: skip
    port = int(str(iface.get("listen-port", "0")) or 0)
    why = "задан в настройках шлюза"
    if not endpoint:
        cloud = {}
        try:
            cloud = await client._dict("ip/cloud")
        except Exception:  # ip/cloud is optional on CHR/x86
            cloud = {}
        if uplink_addresses is None:
            uplink_addresses = _public_addresses(addresses)
        endpoint, why = guess_endpoint(cloud, uplink_addresses)
    rules = await client._list("ip/firewall/filter", optional=True)
    name = str(iface.get("name"))
    shown = [
        peer_view(p)
        for p in peers
        if p.get("interface") == name and (not site or _mine(p, site))
    ]
    return {
        "available": True,
        "admin_net": admin_net,
        "interface": name,
        "port": port,
        "endpoint": endpoint,
        "endpoint_why": why,
        "input_open": input_open(rules, port),
        "peers": shown,
    }


async def async_create(
    client,
    *,
    name: str,
    admin_net: str | None,
    endpoint: str,
    allowed_ips: str,
    site: str = "",
) -> dict[str, Any]:
    """Add a peer for a fresh key pair; return the client config once.

    With `site` the router is a gateway: the client gets an address in the
    gateway's admin network, reaches the site through the gateway's tunnel and
    also the gateway's own address for DNS.
    """
    _ifaces, peers, addresses, admin_net, iface = await _read(client, admin_net)
    if iface is None or not admin_net:
        raise ValueError("на роутере нет админского WireGuard-интерфейса")
    leases = await client.leases()
    pools = await client._list("ip/pool", optional=True)
    ip = free_address(
        admin_net, peers=peers, leases=leases, addresses=addresses, pools=pools
    )
    if ip is None:
        raise ValueError(f"в {admin_net} не осталось свободных адресов")
    net = ipaddress.ip_network(admin_net)
    dns = next(
        (
            str(ipaddress.ip_interface(a["address"]).ip)
            for a in addresses
            if a.get("address")
            and ipaddress.ip_interface(a["address"]).network == net
            and not as_bool(a.get("disabled"))
        ),
        "",
    )
    if site and dns and dns not in allowed_ips:
        allowed_ips = f"{allowed_ips}, {dns}/32"
    label = ascii_name(name) or ip
    comment = f"{_scope(site)} {label} ({ip})"
    iface_name = str(iface["name"])
    pname = peer_name(name or "device", ip)
    private, public = gen_keypair()
    peer = await client._request(
        "PUT",
        "interface/wireguard/peers",
        {
            "interface": iface_name,
            "name": pname,
            "public-key": public,
            "allowed-address": f"{ip}/32",
            "comment": comment,
        },
    )
    # The admin VLAN's address lives on the VLAN, not on wg: route the /32.
    try:
        await client._request(
            "PUT",
            "ip/route",
            {"dst-address": f"{ip}/32", "gateway": iface_name, "comment": comment},
        )
    except Exception:
        if isinstance(peer, dict) and peer.get(".id"):
            await client._request("DELETE", f"interface/wireguard/peers/{peer['.id']}")
        raise
    port = int(str(iface.get("listen-port", "0")) or 0)
    config = client_config(
        private_key=private,
        address=ip,
        dns=dns,
        server_public_key=str(iface.get("public-key", "")),
        endpoint=endpoint,
        port=port,
        allowed_ips=allowed_ips,
        name=name,
    )
    return {
        "address": ip,
        "peer": pname,
        "config": config,
        "qr_svg": qr_svg(config),
        "filename": f"{pname}.conf",
    }


async def async_remove(client, peer_id: str, *, site: str = "") -> dict[str, Any]:
    peers = await client.wireguard_peers()
    row = next((p for p in peers if p.get(".id") == peer_id), None)
    if row is None:
        raise ValueError("такого подключения уже нет")
    if not _mine(row, site):
        raise ValueError(
            "это подключение создано не Site Health — удалите его на роутере"
        )
    await client._request("DELETE", f"interface/wireguard/peers/{peer_id}")
    removed_routes = 0
    for n in _networks(row.get("allowed-address", "")):
        for r in await client.host_route(str(n.network_address)):
            if str(r.get("comment", "")).startswith(TAG) and r.get(".id"):
                await client._request("DELETE", f"ip/route/{r['.id']}")
                removed_routes += 1
    return {"removed": True, "routes": removed_routes}
