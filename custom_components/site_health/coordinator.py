"""Polling coordinators: one for the core router, one per extra device."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .mikrotik import MikrotikAuthError, MikrotikClient, MikrotikError
from .models import DeviceSnapshot, InterfaceInfo, SiteSnapshot
from .parsers import (
    apply_rates,
    build_clients,
    norm_mac,
    parse_interfaces,
    parse_neighbors,
    parse_probes,
    parse_radio_links,
    parse_segments,
    parse_system,
    parse_tunnels,
    parse_wans,
)

_LOGGER = logging.getLogger(__name__)


class DeviceCoordinator(DataUpdateCoordinator[DeviceSnapshot]):
    """Polls one RouterOS device: system, interfaces and its radio links."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MikrotikClient,
        *,
        name: str,
        interval_s: int,
        subentry_id: str | None = None,
        peer_names: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=timedelta(seconds=interval_s),
        )
        self.client = client
        self.device_name = name
        self.subentry_id = subentry_id
        self._peer_names = peer_names or dict
        self._prev_ifaces: dict[str, InterfaceInfo] | None = None
        self._prev_time: float | None = None
        self.last_error: str | None = None

    @property
    def host(self) -> str:
        return self.client.host

    async def _async_update_data(self) -> DeviceSnapshot:
        try:
            return await self._fetch()
        except MikrotikAuthError as err:
            # Only the main entry can be reauthenticated; a subentry device
            # just goes unavailable until its credentials are fixed.
            if self.subentry_id is None:
                raise ConfigEntryAuthFailed(str(err)) from err
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err
        except MikrotikError as err:
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err

    async def _common(self) -> tuple[DeviceSnapshot, dict[str, Any]]:
        c = self.client
        (
            identity,
            resource,
            routerboard,
            health,
            ifaces_raw,
            registrations,
        ) = await asyncio.gather(
            c.identity(),
            c.resource(),
            c.routerboard(),
            c.health(),
            c.interfaces(),
            c.wifi_registrations(),
        )
        now = time.monotonic()
        ifaces = parse_interfaces(ifaces_raw)
        elapsed = now - self._prev_time if self._prev_time else None
        apply_rates(ifaces, self._prev_ifaces, elapsed)
        self._prev_ifaces, self._prev_time = ifaces, now
        snap = DeviceSnapshot(
            system=parse_system(identity, resource, routerboard, health),
            interfaces=ifaces,
        )
        self.last_error = None
        return snap, {"registrations": registrations}

    async def _fetch(self) -> DeviceSnapshot:
        snap, raw = await self._common()
        snap.radio_links = parse_radio_links(raw["registrations"], self._peer_names())
        return snap


class SiteCoordinator(DeviceCoordinator):
    """The core router: everything a device has, plus the site-wide view."""

    def __init__(self, *args: Any, family_vlan: int, wg_stale_s: int, **kw: Any):
        super().__init__(*args, **kw)
        self.family_vlan = family_vlan
        self.wg_stale_s = wg_stale_s
        # MACs of the infrastructure itself, hidden from the client list.
        self.infra_macs: set[str] = set()

    @property
    def site(self) -> SiteSnapshot | None:
        return self.data if isinstance(self.data, SiteSnapshot) else None

    def peer_names(self) -> dict[str, str]:
        site = self.site
        if not site:
            return {}
        return {mac: nb.identity for mac, nb in site.neighbors.items() if nb.identity}

    async def _fetch(self) -> SiteSnapshot:
        base, raw = await self._common()
        c = self.client
        (
            vlans,
            addresses,
            dhcp_clients,
            netwatch,
            wg_peers,
            neighbors_raw,
            leases,
            arp,
            access_list,
            caps,
        ) = await asyncio.gather(
            c.vlans(),
            c.addresses(),
            c.dhcp_clients(),
            c.netwatch(),
            c.wireguard_peers(),
            c.neighbors(),
            c.leases(),
            c.arp(),
            c.wifi_access_list(),
            c.capsman_caps(),
        )
        wans = parse_wans(dhcp_clients, base.interfaces)
        neighbors = parse_neighbors(neighbors_raw)
        segments = parse_segments(
            addresses, vlans, base.interfaces, exclude_interfaces=set(wans)
        )
        infra = {i.mac for i in base.interfaces.values() if i.mac} | set(neighbors)
        infra |= {norm_mac(m) for m in self.infra_macs}
        clients = build_clients(
            leases=leases,
            arp=arp,
            registrations=raw["registrations"],
            access_list=access_list,
            segments=segments,
            family_vlan=self.family_vlan,
            exclude_macs=infra,
        )
        return SiteSnapshot(
            system=base.system,
            interfaces=base.interfaces,
            radio_links={},  # GW's own registrations are clients, not links
            wans=wans,
            probes=parse_probes(netwatch),
            tunnels=parse_tunnels(wg_peers, self.wg_stale_s),
            segments=segments,
            clients=clients,
            neighbors=neighbors,
            caps=caps,
        )
