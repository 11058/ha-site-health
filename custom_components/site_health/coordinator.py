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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SEG_SWITCHES
from .mikrotik import MikrotikAuthError, MikrotikClient, MikrotikError
from .models import DeviceSnapshot, InterfaceInfo, ModemInfo, SiteSnapshot
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
from .ssc_switch import SscSwitchClient, SwitchBusyError, SwitchError, SwitchInfo
from .tandem import (
    TandemClient,
    TandemError,
    balance_request_for,
    parse_balance,
    parse_status,
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

    def __init__(
        self,
        *args: Any,
        family_vlan: int,
        wg_stale_s: int,
        admin_vlan: int = 99,
        **kw: Any,
    ):
        super().__init__(*args, **kw)
        self.family_vlan = family_vlan
        self.admin_vlan = admin_vlan
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
            pppoe,
            lte,
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
            c.pppoe_clients(),
            c.lte(),
        )
        wans = parse_wans(
            dhcp_clients,
            base.interfaces,
            pppoe_clients=pppoe,
            lte=lte,
            addresses=addresses,
        )
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
            admin_vlan=self.admin_vlan,
            exclude_macs=infra,
        )
        # Switch management VLAN holds infrastructure, shown with the devices.
        clients = {m: c for m, c in clients.items() if c.segment_kind != SEG_SWITCHES}
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


class ModemCoordinator(DataUpdateCoordinator[ModemInfo]):
    """Polls a cellular router (Tandem-4GT) through its LuCI endpoints.

    Besides the radio status it keeps: the balance (asked by a free SMS to the
    operator, at most once a day unless asked from the panel; the answer is
    read from the inbox) and the Ping Check / failover settings (read-only).
    """

    SLOW_EVERY_S = 600  # SMS inbox and settings page
    REPLY_WAIT_S = 600  # how long to look out for the operator's answer

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TandemClient,
        *,
        name: str,
        interval_s: int,
        subentry_id: str,
        balance_hours: int = 24,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=timedelta(seconds=max(interval_s, 30)),
        )
        self.client = client
        self.device_name = name
        self.subentry_id = subentry_id
        self.balance_hours = balance_hours
        self.last_error: str | None = None
        self._store: Store = Store(hass, 1, f"{DOMAIN}.modem.{subentry_id}")
        self._saved: dict[str, Any] = {}
        self._slow_at = 0.0

    @property
    def host(self) -> str:
        return self.client.host

    async def async_restore(self) -> None:
        self._saved = await self._store.async_load() or {}

    def _save(self) -> None:
        self._store.async_delay_save(lambda: dict(self._saved), 30)

    async def _async_update_data(self) -> ModemInfo:
        try:
            out = await self.client.status()
        except TandemError as err:
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err
        previous = self.data or ModemInfo(
            last_by_slot=self._saved.get("last_by_slot", {})
        )
        info = parse_status(out, previous)
        if info.last_by_slot != self._saved.get("last_by_slot"):
            self._saved["last_by_slot"] = info.last_by_slot
            self._save()

        request = balance_request_for(info.operator)
        info.balance_supported = request is not None
        now = time.time()
        asked = self._saved.get("balance_requested")
        waiting = asked is not None and now - asked < self.REPLY_WAIT_S
        if (
            request
            and self.balance_hours
            and (asked is None or now - asked > self.balance_hours * 3600)
        ):
            await self._request_balance(request)
            waiting = True
        if waiting or now - self._slow_at > self.SLOW_EVERY_S:
            self._slow_at = now
            await self._read_slow(request)
        info.balance = self._saved.get("balance")
        info.balance_text = self._saved.get("balance_text", "")
        info.balance_when = self._saved.get("balance_when", "")
        info.balance_requested = self._saved.get("balance_requested")
        info.failover = self._saved.get("failover", {})
        self.last_error = None
        return info

    async def _read_slow(self, request: tuple[str, str] | None) -> None:
        try:
            messages = await self.client.read_sms()
            amount, msg = parse_balance(messages, request[0] if request else None)
            if msg is not None and msg.get("ts") != self._saved.get("balance_ts"):
                self._saved.update(
                    balance=amount,
                    balance_text=msg["text"][:300],
                    balance_when=msg["when"],
                    balance_ts=msg.get("ts"),
                )
                self._save()
        except TandemError as err:
            _LOGGER.debug("SMS inbox not read: %s", err)
        try:
            failover = await self.client.failover_mode()
            if failover and failover != self._saved.get("failover"):
                self._saved["failover"] = failover
                self._save()
        except TandemError as err:
            _LOGGER.debug("failover settings not read: %s", err)

    async def _request_balance(self, request: tuple[str, str]) -> None:
        number, text = request
        try:
            await self.client.send_sms(number, text)
        except TandemError as err:
            _LOGGER.warning("Balance request SMS not sent: %s", err)
            return
        self._saved["balance_requested"] = time.time()
        self._save()

    async def async_request_balance(self) -> None:
        info = self.data
        request = balance_request_for(info.operator) if info else None
        if request is None:
            raise TandemError("no balance request known for this operator")
        await self._request_balance(request)
        await self.async_request_refresh()

    async def async_select_sim(self, slot: int) -> None:
        await self.client.select_sim(slot)
        await self.async_request_refresh()


class SwitchCoordinator(DataUpdateCoordinator[SwitchInfo]):
    """Polls an SSC web-smart switch (PS308GV3 / PS208GV3)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SscSwitchClient,
        *,
        name: str,
        interval_s: int,
        subentry_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            # Each poll is a full login/logout on a tiny web server: keep it calm.
            update_interval=timedelta(seconds=max(interval_s * 2, 60)),
        )
        self.client = client
        self.device_name = name
        self.subentry_id = subentry_id
        self.last_error: str | None = None

    @property
    def host(self) -> str:
        return self.client.host

    async def _async_update_data(self) -> SwitchInfo:
        try:
            info = await self.client.read()
        except SwitchBusyError as err:
            # Someone is in the switch's web page: keep the last data.
            self.last_error = str(err)
            if self.data is not None:
                return self.data
            raise UpdateFailed(str(err)) from err
        except SwitchError as err:
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err
        self.last_error = None
        return info

    async def async_run(self, action) -> None:
        await action(self.client)
        await self.async_refresh()
