"""Admin VPN: key pair, free address, client config, router calls."""

from __future__ import annotations

import asyncio
import base64

from conftest import load_module
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

vpn = load_module("admin_vpn")

GW_IFACES = [
    {
        "name": "wg-Dom",
        "listen-port": "13231",
        "public-key": "DOM=",
        "disabled": "false",
    },
    {
        "name": "wg-Admin",
        "listen-port": "13234",
        "public-key": "SRV=",
        "disabled": "false",
    },
]
GW_PEERS = [
    {
        ".id": "*1",
        "interface": "wg-Dom",
        "name": "dom-hub",
        "allowed-address": "10.0.0.0/16,10.255.0.0/24",
    },
    {
        ".id": "*2",
        "interface": "wg-Admin",
        "name": "admin-laptop",
        "allowed-address": "10.5.99.250/32",
    },
    {
        ".id": "*3",
        "interface": "wg-Admin",
        "name": "admin-phone",
        "allowed-address": "10.5.99.251/32",
    },
]


def test_keypair_is_wireguard_shaped():
    priv, pub = vpn.gen_keypair()
    raw = base64.b64decode(priv)
    assert len(raw) == 32 and len(base64.b64decode(pub)) == 32
    again = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    assert base64.b64encode(again).decode() == pub
    assert vpn.gen_keypair()[0] != priv  # fresh every time


def test_admin_interface_by_name_then_by_peers():
    assert (
        vpn.find_admin_interface(GW_IFACES, GW_PEERS, "10.5.99.0/24")["name"]
        == "wg-Admin"
    )
    ifaces = [
        {"name": "wg1", "disabled": "false"},
        {"name": "wg2", "disabled": "false"},
    ]
    peers = [{"interface": "wg2", "allowed-address": "10.7.99.250/32"}]
    assert vpn.find_admin_interface(ifaces, peers, "10.7.99.0/24")["name"] == "wg2"
    assert vpn.find_admin_interface(ifaces, [], "10.7.99.0/24") is None


def test_free_address_top_first_skips_used_and_pool():
    ip = vpn.free_address("10.5.99.0/24", peers=GW_PEERS)
    assert ip == "10.5.99.252"
    ip = vpn.free_address(
        "10.5.99.0/24",
        peers=GW_PEERS,
        leases=[{"address": "10.5.99.252"}],
        addresses=[{"address": "10.5.99.253/24"}],
        pools=[{"ranges": "10.5.99.254-10.5.99.254,10.5.99.100-10.5.99.249"}],
    )
    assert ip == "10.5.99.99"


def test_allowed_ips_site_slash16_never_default():
    nets = ["10.5.10.0/24", "10.5.20.0/24", "10.5.99.0/24"]
    assert vpn.site_allowed_ips(nets, "10.5.99.0/24") == "10.5.0.0/16"
    mixed = vpn.site_allowed_ips(["192.168.1.0/24", "10.9.0.0/24"], "10.9.0.0/24")
    assert "0.0.0.0/0" not in mixed and "192.168.1.0/24" in mixed


def test_endpoint_guess():
    assert (
        vpn.guess_endpoint(
            {"ddns-enabled": "true", "dns-name": "x.sn.mynetname.net"}, []
        )[0]
        == "x.sn.mynetname.net"
    )
    assert (
        vpn.guess_endpoint({}, ["192.168.8.100", "77.236.65.70"])[0] == "77.236.65.70"
    )
    host, why = vpn.guess_endpoint({}, ["192.168.8.100", "100.125.3.4"])
    assert host == "" and "NAT" in why


def test_input_open():
    rules = [
        {
            "chain": "input",
            "action": "accept",
            "protocol": "udp",
            "dst-port": "13234",
            "disabled": "true",
        }
    ]
    assert vpn.input_open(rules, 13234) is False
    rules[0]["disabled"] = "false"
    assert vpn.input_open(rules, 13234) is True
    assert vpn.input_open([], 13234) is None


def test_names_are_ascii():
    assert vpn.ascii_name("Телефон Алексея") == "Telefon Alekseya"
    assert vpn.peer_name("Ноут Маши!") == "adm-nout-mashi"
    assert vpn.peer_name("") == "adm-device"


def test_config_text():
    cfg = vpn.client_config(
        private_key="PRIV=", address="10.5.99.252", dns="10.5.99.1",
        server_public_key="SRV=", endpoint="1.2.3.4", port=13234,
        allowed_ips="10.5.0.0/16", name="Phone",
    )  # fmt: skip
    assert "Address = 10.5.99.252/32" in cfg and "Endpoint = 1.2.3.4:13234" in cfg
    assert "AllowedIPs = 10.5.0.0/16" in cfg and "PersistentKeepalive = 25" in cfg
    assert vpn.qr_svg(cfg).startswith("<svg")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.peers = [dict(p) for p in GW_PEERS]

    async def _list(self, path, optional=False):
        return {
            "interface/wireguard": GW_IFACES,
            "ip/pool": [{"ranges": "10.5.99.100-10.5.99.200"}],
        }.get(path, [])

    async def _dict(self, path):
        return {}

    async def wireguard_peers(self):
        return self.peers

    async def leases(self):
        return []

    async def addresses(self):
        return [{"address": "10.5.99.1/24", "interface": "vlan99-Admin"}]

    async def host_route(self, ip):
        return [{".id": "*R", "comment": vpn.TAG + " x"}] if ip == "10.5.99.252" else []

    async def _request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "PUT" and path == "interface/wireguard/peers":
            row = {".id": "*9", **payload}
            self.peers.append(row)
            return row
        return {}


def test_create_and_remove():
    c = FakeClient()
    res = asyncio.run(
        vpn.async_create(
            c,
            name="Телефон",
            admin_net="10.5.99.0/24",
            endpoint="",
            allowed_ips="10.5.0.0/16",
        )
    )
    put_peer = c.calls[0]
    assert put_peer[2]["interface"] == "wg-Admin"
    assert put_peer[2]["allowed-address"] == "10.5.99.252/32"
    assert "private" not in str(put_peer[2]).lower()
    priv = res["config"].split("PrivateKey = ")[1].split("\n")[0]
    assert priv not in str(c.calls)  # the private key never reaches the router
    assert c.calls[1][1] == "ip/route" and c.calls[1][2]["gateway"] == "wg-Admin"
    assert "DNS = 10.5.99.1" in res["config"] and "Endpoint" not in res["config"]
    assert res["filename"] == "adm-telefon.conf"

    out = asyncio.run(vpn.async_remove(c, "*9"))
    assert out == {"removed": True, "routes": 1}
    try:
        asyncio.run(vpn.async_remove(c, "*2"))  # admin-laptop: made by hand
    except ValueError as err:
        assert "не Site Health" in str(err)
    else:
        raise AssertionError("must refuse foreign peers")
