"""Onboarding: the one paste per device, and what the integration sets up itself.

Home Assistant cannot reach a stock RouterOS box until something on it lets HA
in, so each device needs one paste into its terminal. The config flow
generates that paste with HA's own address and a random password filled in.
Everything after that (forward rule to the radios' subnet, internet probes) is
done over REST by the integration, idempotently, tagged with COMMENT_TAG.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field

from .mikrotik import MikrotikClient, MikrotikError
from .models import Neighbor, Segment
from .parsers import as_bool

_LOGGER = logging.getLogger(__name__)

USERNAME = "ha-infra"
GROUP = "ha-infra"
CERT = "site-health-rest"
COMMENT_TAG = "site-health"

_DENY = "!local,!telnet,!ssh,!ftp,!reboot,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon"
POLICY_CORE = f"read,write,api,rest-api,{_DENY}"
POLICY_DEVICE = f"read,api,rest-api,!write,{_DENY}"

INTERNET_PROBES = [("8.8.8.8", "Internet 8.8.8.8"), ("1.1.1.1", "Internet 1.1.1.1")]


def new_password() -> str:
    # URL-safe alphabet: nothing RouterOS treats specially inside "...".
    return secrets.token_urlsafe(18)


def onboarding_script(*, ha_ip: str, password: str, core: bool) -> str:
    """RouterOS commands that let HA in. Safe to paste twice."""
    ha = f"{ha_ip}/32"
    policy = POLICY_CORE if core else POLICY_DEVICE
    lines = [
        f"# Site Health: access for Home Assistant {ha_ip}",
        f":if ([:len [/certificate find name={CERT}]] = 0) do={{ /certificate add name={CERT} common-name={CERT} days-valid=3650 key-usage=digital-signature,key-encipherment,tls-server; /certificate sign {CERT} }}",
        # Signing runs in the background on small boards: wait for the key.
        f":local n 0; :while ([:len [/certificate find where name={CERT} and private-key]] = 0 && $n < 60) do={{ :delay 1s; :set n ($n + 1) }}",
        f':if ([/ip service get www-ssl certificate] = "none") do={{ /ip service set www-ssl certificate={CERT} }}',
        # Keep any addresses already allowed (e.g. the admin's), only add HA.
        f':if ([/ip service get www-ssl disabled]) do={{ /ip service set www-ssl address={ha} disabled=no }} else={{ :local a [/ip service get www-ssl address]; :if ([:len $a] > 0 && [:typeof [:find $a {ha}]] = "nil") do={{ /ip service set www-ssl address=($a,"{ha}") }} }}',
        f':if ([:len [/user group find name={GROUP}]] = 0) do={{ /user group add name={GROUP} policy={policy} comment="Site Health (Home Assistant)" }} else={{ /user group set {GROUP} policy={policy} }}',
        f':if ([:len [/user find name={USERNAME}]] = 0) do={{ /user add name={USERNAME} group={GROUP} address={ha} password="{password}" }} else={{ /user set {USERNAME} group={GROUP} address={ha} password="{password}" }}',
    ]
    if core:
        lines.append(
            f':if ([:len [/ip firewall filter find comment="{COMMENT_TAG}: HA REST"]] = 0) do={{ :local f [/ip firewall filter find chain=input]; :if ([:len $f] > 0) do={{ /ip firewall filter add chain=input action=accept protocol=tcp dst-port=443 src-address={ha_ip} comment="{COMMENT_TAG}: HA REST" place-before=[:pick $f 0] }} else={{ /ip firewall filter add chain=input action=accept protocol=tcp dst-port=443 src-address={ha_ip} comment="{COMMENT_TAG}: HA REST" }} }}'
        )
    return "\n".join(lines)


@dataclass
class ProvisionResult:
    done: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def device_networks(
    neighbors: Iterable[Neighbor], segments: dict[str, Segment], ha_ip: str
) -> list[str]:
    """Subnets holding MikroTik neighbours that HA is not part of itself."""
    ha = ipaddress.ip_address(ha_ip)
    nets: list[str] = []
    for nb in neighbors:
        if "mikrotik" not in nb.platform.lower() or not nb.address:
            continue
        try:
            addr = ipaddress.ip_address(nb.address)
        except ValueError:
            continue
        for seg in segments.values():
            net = ipaddress.ip_network(seg.network)
            if addr in net and ha not in net and seg.network not in nets:
                nets.append(seg.network)
    return nets


def first_forward_drop(rules: list[dict]) -> str | None:
    """Insert point: before the first forward drop/reject that is not 'invalid'."""
    for rule in rules:
        if rule.get("chain") != "forward" or as_bool(rule.get("disabled")):
            continue
        if rule.get("action") not in ("drop", "reject"):
            continue
        if "invalid" in str(rule.get("connection-state", "")):
            continue
        return rule.get(".id")
    return None


async def async_provision_core(
    client: MikrotikClient,
    *,
    ha_ip: str,
    neighbors: Iterable[Neighbor],
    segments: dict[str, Segment],
) -> ProvisionResult:
    """Idempotent setup on the core router after HA has its login."""
    result = ProvisionResult()

    # 1. HA -> REST of radios/APs in other subnets.
    try:
        rules = await client._list("ip/firewall/filter")
        before = first_forward_drop(rules)
        for net in device_networks(neighbors, segments, ha_ip):
            comment = f"{COMMENT_TAG}: HA REST -> {net}"
            what = f"forward HA -> {net}:443"
            if any(r.get("comment") == comment for r in rules):
                result.already.append(what)
                continue
            payload = {
                "chain": "forward",
                "action": "accept",
                "protocol": "tcp",
                "dst-port": "443",
                "src-address": ha_ip,
                "dst-address": net,
                "comment": comment,
            }
            if before:
                payload["place-before"] = before
            await client._request("PUT", "ip/firewall/filter", payload)
            result.done.append(what)
    except MikrotikError as err:
        _LOGGER.warning("Could not add the forward rule: %s", err)
        result.failed.append(f"forward rule: {err}")

    # 2. Internet reachability probes.
    try:
        existing = await client.netwatch()
        hosts = {r.get("host") for r in existing}
        for host, title in INTERNET_PROBES:
            what = f"netwatch {host}"
            if host in hosts:
                result.already.append(what)
                continue
            await client._request(
                "PUT",
                "tool/netwatch",
                {
                    "host": host,
                    "type": "icmp",
                    "interval": "30s",
                    "name": f"inet-{host}",
                    "comment": f"{COMMENT_TAG}: {title}",
                },
            )
            result.done.append(what)
    except MikrotikError as err:
        _LOGGER.warning("Could not add netwatch probes: %s", err)
        result.failed.append(f"netwatch: {err}")
    return result
