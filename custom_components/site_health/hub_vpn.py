"""Admin VPN through a provider's hub, without giving this HA any rights there.

The hub (a MikroTik with a public address whose tunnel reaches the site) runs
a small script every minute. It fetches from this HA the list of public keys
and addresses wanted for this site, adds and removes WireGuard peers on its
client interface, and posts back what it has (its public key, port, last
handshakes). The script only ever touches peers tagged for this site and only
addresses inside the site's own client network; the hub's firewall lets that
network reach the site's subnets and nothing else. A compromised HA can at
most add keys that reach its own site.

HA keeps public keys only. Private keys exist once, in the config handed to
the admin.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

HUB_IFACE = "WG-Sites"
HUB_PORT = 13235
HUB_ADDRESS = "10.250.0.1/16"
VIEW_URL = "/api/site_health/hub/{token}"
_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def site_tag(site: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", site.lower()).strip("-")
    return f"site-vpn:{slug or 'site'}"


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


@dataclass
class GatewayState:
    """Wanted peers and the hub's last report, for one gateway."""

    peers: list[dict[str, Any]] = field(default_factory=list)  # k, a, n, t
    seen: float | None = None
    hub_key: str = ""
    hub_port: int = 0
    handshakes: dict[str, str] = field(default_factory=dict)  # key -> age

    def as_dict(self) -> dict[str, Any]:
        return {
            "peers": self.peers,
            "seen": self.seen,
            "hub_key": self.hub_key,
            "hub_port": self.hub_port,
            "handshakes": self.handshakes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GatewayState:
        return cls(
            peers=list(d.get("peers", [])),
            seen=d.get("seen"),
            hub_key=d.get("hub_key", ""),
            hub_port=int(d.get("hub_port") or 0),
            handshakes=dict(d.get("handshakes", {})),
        )

    def wanted(self) -> dict[str, Any]:
        return {"peers": [{"k": p["k"], "a": p["a"], "n": p["n"]} for p in self.peers]}

    def report(self, body: dict[str, Any], now: float | None = None) -> None:
        key = str(body.get("pub", ""))
        if valid_key(key):
            self.hub_key = key
        with contextlib.suppress(TypeError, ValueError):
            self.hub_port = int(body.get("port") or self.hub_port)
        hs = {}
        for row in body.get("peers") or []:
            if isinstance(row, dict) and valid_key(str(row.get("k", ""))):
                hs[str(row["k"])] = str(row.get("hs") or "")
        self.handshakes = hs
        self.seen = now if now is not None else time.time()

    def free_address(self, client_net: str) -> str | None:
        net = ipaddress.ip_network(client_net)
        used = {p["a"] for p in self.peers}
        for host in reversed(list(net.hosts())):
            if str(host) not in used:
                return str(host)
        return None

    def add(self, *, key: str, address: str, name: str) -> None:
        self.peers.append({"k": key, "a": address, "n": name, "t": int(time.time())})

    def remove(self, key: str) -> bool:
        before = len(self.peers)
        self.peers = [p for p in self.peers if p["k"] != key]
        return len(self.peers) != before

    def view(self) -> list[dict[str, Any]]:
        out = []
        for p in self.peers:
            out.append(
                {
                    "id": p["k"],
                    "name": p["n"],
                    "comment": "",
                    "address": f"{p['a']}/32",
                    "ours": True,
                    "applied": p["k"] in self.handshakes,
                    "last_handshake": self.handshakes.get(p["k"], ""),
                }
            )
        return out


def _q(value: str) -> str:
    """A RouterOS string literal (one line: newlines become \\n)."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return '"' + esc.replace("\n", "\\n") + '"'


def hub_script(
    *,
    site: str,
    ha_url: str,
    token: str,
    client_net: str,
    site_nets: list[str],
    src_address: str = "",
    iface: str = HUB_IFACE,
    port: int = HUB_PORT,
    hub_address: str = HUB_ADDRESS,
) -> str:
    """RouterOS commands for the hub. Safe to paste again."""
    tag = site_tag(site)
    name = tag.replace(":", "-")
    url = ha_url.rstrip("/") + VIEW_URL.format(token=token)
    src = f" src-address={src_address}" if src_address else ""
    # The reconcile script itself, as RouterOS source.
    body = "\n".join(
        [
            f":local url {_q(url)}",
            f":local iface {_q(iface)}",
            f":local tag {_q(tag)}",
            f":local net {client_net}",
            ":local r",
            f':do {{ :set r ([/tool fetch url=$url{src} output=user as-value]->"data") }} on-error={{ :log warning "$tag: no answer from Home Assistant"; :error "fetch" }}',
            ':local want ([:deserialize from=json value=$r]->"peers")',
            ':local keep [:toarray ""]',
            ':foreach p in=$want do={ :local k ($p->"k"); :local a [:toip ($p->"a")]; :local n ($p->"n"); :if ([:typeof $a] = "ip" && ($a in $net) && [:len $k] = 44) do={ :local id [/interface wireguard peers find interface=$iface public-key=$k]; :if ([:len $id] = 0) do={ :if ([:len [/interface wireguard peers find interface=$iface allowed-address="$a/32"]] = 0) do={ /interface wireguard peers add interface=$iface public-key=$k allowed-address="$a/32" comment="$tag $n" } } else={ :if ([/interface wireguard peers get $id comment] ~ "^$tag ") do={ /interface wireguard peers set $id allowed-address="$a/32" comment="$tag $n" } }; :set ($keep->$k) $a } }',
            ':foreach id in=[/interface wireguard peers find interface=$iface comment~"^$tag "] do={ :local k [/interface wireguard peers get $id public-key]; :if ([:typeof ($keep->$k)] = "nothing") do={ /interface wireguard peers remove $id } }',
            ':local rep [:toarray ""]',
            ':foreach id in=[/interface wireguard peers find interface=$iface comment~"^$tag "] do={ :local h [/interface wireguard peers get $id last-handshake]; :set rep ($rep, {{"k"=[/interface wireguard peers get $id public-key]; "hs"=[:tostr $h]}}) }',
            ':local out {"pub"=[/interface wireguard get $iface public-key]; "port"=[/interface wireguard get $iface listen-port]; "peers"=$rep}',
            f':do {{ /tool fetch url=$url{src} http-method=post http-header-field="Content-Type: application/json" http-data=[:serialize to=json value=$out] output=none }} on-error={{ :log warning "$tag: report failed" }}',
        ]
    )
    lines = [
        f"# Site Health: admin VPN for {site} through this hub",
        f':if ([:len [/interface wireguard find name={iface}]] = 0) do={{ /interface wireguard add name={iface} listen-port={port} mtu=1420 comment="VPN clients of sites, one /24 per site" }}',
        f':if ([:len [/ip address find interface={iface}]] = 0) do={{ /ip address add address={hub_address} interface={iface} comment="site VPN clients" }}',
        # Firewall: placed above everything else, so no broad rule can widen it.
        f':if ([:len [/ip firewall filter find comment="site-vpn: wg port"]] = 0) do={{ /ip firewall filter add chain=input action=accept protocol=udp dst-port={port} comment="site-vpn: wg port" place-before=[:pick [/ip firewall filter find chain=input dynamic=no] 0] }}',
        f':if ([:len [/ip firewall filter find comment="site-vpn: clients never to the router"]] = 0) do={{ /ip firewall filter add chain=input action=drop in-interface={iface} comment="site-vpn: clients never to the router" place-before=[:pick [/ip firewall filter find chain=input dynamic=no] 0] }}',
        f':if ([:len [/ip firewall filter find comment="site-vpn: drop everything else from clients"]] = 0) do={{ /ip firewall filter add chain=forward action=drop in-interface={iface} comment="site-vpn: drop everything else from clients" place-before=[:pick [/ip firewall filter find chain=forward dynamic=no] 0] }}',
        f':if ([:len [/ip firewall filter find comment="site-vpn: nobody starts connections to clients"]] = 0) do={{ /ip firewall filter add chain=forward action=drop out-interface={iface} connection-state=new comment="site-vpn: nobody starts connections to clients" place-before=[:pick [/ip firewall filter find chain=forward dynamic=no] 0] }}',
        f':if ([:len [/ip firewall filter find comment="{tag}: own site only"]] = 0) do={{ /ip firewall filter add chain=forward action=accept in-interface={iface} src-address={client_net} dst-address-list={name} comment="{tag}: own site only" place-before=[:pick [/ip firewall filter find chain=forward dynamic=no] 0] }}',
        f"/ip firewall address-list remove [find list={name}]",
        *[
            f'/ip firewall address-list add list={name} address={n} comment="{tag}"'
            for n in site_nets
        ],
        f"/system script remove [find name={name}]",
        f"/system script add name={name} policy=read,write,test dont-require-permissions=no source={_q(body)}",
        f':if ([:len [/system scheduler find name={name}]] = 0) do={{ /system scheduler add name={name} interval=1m on-event={name} policy=read,write,test comment="{tag}: sync peers with Home Assistant" }}',
        f"/system script run {name}",
    ]
    return "\n".join(lines)
