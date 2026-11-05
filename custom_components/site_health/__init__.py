"""Site Health: MikroTik network health and family Wi-Fi list for one site."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_MAC,
    ATTR_NAME,
    CONF_ADMIN_VLAN,
    CONF_AUTO_CONFIGURE,
    CONF_FAMILY_VLAN,
    CONF_HA_IP,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SITE_NAME,
    CONF_TLS,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_WG_STALE,
    DEFAULT_ADMIN_VLAN,
    DEFAULT_AUTO_CONFIGURE,
    DEFAULT_FAMILY_VLAN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WG_STALE,
    DOMAIN,
    PLACE_ADMIN,
    PLACE_FAMILY,
    PLACE_GUEST,
    SERVICE_ADD_FAMILY,
    SERVICE_MODEM_SIM,
    SERVICE_REFRESH,
    SERVICE_REMOVE_FAMILY,
    SERVICE_SET_SEGMENT,
    SUBENTRY_DEVICE,
    SUBENTRY_GATEWAY,
    SUBENTRY_MODEM,
    SUBENTRY_SWITCH,
)
from .coordinator import (
    DeviceCoordinator,
    ModemCoordinator,
    SiteCoordinator,
    SwitchCoordinator,
)
from .mikrotik import MikrotikClient, MikrotikError
from .parsers import as_int, norm_mac, place_comment
from .ssc_switch import SscSwitchClient
from .tandem import TandemClient, TandemError

_LOGGER = logging.getLogger(__name__)

PROVISION_INTERVAL = timedelta(minutes=10)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@dataclass
class SiteRuntime:
    site: SiteCoordinator
    devices: dict[str, DeviceCoordinator] = field(default_factory=dict)
    modems: dict[str, ModemCoordinator] = field(default_factory=dict)
    switches: dict[str, SwitchCoordinator] = field(default_factory=dict)
    # Human names for clients, by MAC. Kept in HA: Cyrillic-safe and
    # independent of what the router can store in its comments.
    names: dict[str, str] = field(default_factory=dict)
    names_store: Any = None
    # Last provisioning pass on the core router: what is missing or broken.
    provision: dict[str, Any] = field(default_factory=dict)
    # Admin VPN through hubs: wanted public keys and each hub's last report.
    gateways: dict[str, Any] = field(default_factory=dict)
    vpn_store: Any = None

    def save_vpn(self) -> None:
        self.vpn_store.async_delay_save(
            lambda: {k: g.as_dict() for k, g in self.gateways.items()}, 2
        )

    def all(self) -> list[DeviceCoordinator]:
        return [self.site, *self.devices.values()]

    def peer_names(self) -> dict[str, str]:
        """MAC -> name for radio link peers: neighbours plus our own devices.

        The neighbour table only knows the MAC a device shows the core
        router; the far end of a radio link sees the device's wifi MAC,
        which the device itself reports among its interfaces.
        """
        names = self.site.peer_names()
        for coord in self.all():
            if coord.data is None:
                continue
            for iface in coord.data.interfaces.values():
                if iface.mac:
                    names.setdefault(iface.mac, coord.device_name)
        return names


type SiteHealthConfigEntry = ConfigEntry[SiteRuntime]


def make_client(hass: HomeAssistant, data: dict) -> MikrotikClient:
    return MikrotikClient(
        async_get_clientsession(hass, verify_ssl=False),
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        port=data.get(CONF_PORT) or None,
        tls=data.get(CONF_TLS, True),
        verify_ssl=data.get(CONF_VERIFY_SSL, False),
    )


def make_modem_client(hass: HomeAssistant, data: dict) -> TandemClient:
    # A separate cookie jar: LuCI's sysauth cookie must not leak across hosts
    # or get mixed with Home Assistant's shared session.
    return TandemClient(
        async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar()),
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )


def make_switch_client(hass: HomeAssistant, data: dict) -> SscSwitchClient:
    # Own cookie jar for the switch's session cookie; every request carries
    # "Connection: close" because the lwIP server serves one per connection.
    return SscSwitchClient(
        async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar(unsafe=True)),
        data[CONF_HOST],
        data[CONF_PASSWORD],
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    from .panel import async_register_panel
    from .websocket_api import async_register_websocket

    async_register_websocket(hass)
    from .hub_view import HubView

    hass.http.register_view(HubView())
    _register_services(hass)
    await async_register_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SiteHealthConfigEntry) -> bool:
    opts = entry.options
    interval = opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    site = SiteCoordinator(
        hass,
        entry,
        make_client(hass, entry.data),
        name=entry.data.get(CONF_SITE_NAME) or entry.title,
        interval_s=interval,
        family_vlan=opts.get(CONF_FAMILY_VLAN, DEFAULT_FAMILY_VLAN),
        admin_vlan=opts.get(CONF_ADMIN_VLAN, DEFAULT_ADMIN_VLAN),
        wg_stale_s=opts.get(CONF_WG_STALE, DEFAULT_WG_STALE),
    )
    await site.async_config_entry_first_refresh()
    runtime = SiteRuntime(site=site)
    runtime.names_store = Store(hass, 1, f"{DOMAIN}.names.{entry.entry_id}")
    saved = await runtime.names_store.async_load() or {}
    if "names" not in saved:  # 0.5.0 stored a flat MAC -> name map
        saved = {"names": saved, "written": {}}
    runtime.names = saved["names"]
    site.names = runtime.names
    from .hub_vpn import GatewayState

    runtime.vpn_store = Store(hass, 1, f"{DOMAIN}.vpn.{entry.entry_id}")
    vpn_saved = await runtime.vpn_store.async_load() or {}
    runtime.gateways = {
        sid: GatewayState.from_dict(vpn_saved.get(sid, {}))
        for sid, sub in entry.subentries.items()
        if sub.subentry_type == SUBENTRY_GATEWAY
    }
    site.names_written = saved["written"]
    site.save_names = lambda: runtime.names_store.async_delay_save(
        lambda: {"names": runtime.names, "written": site.names_written}, 5
    )

    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type != SUBENTRY_DEVICE:
            continue
        coord = DeviceCoordinator(
            hass,
            entry,
            make_client(hass, sub.data),
            name=sub.title,
            interval_s=interval,
            subentry_id=sub_id,
            peer_names=lambda: runtime.peer_names(),
        )
        runtime.devices[sub_id] = coord

    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type != SUBENTRY_MODEM:
            continue
        modem = ModemCoordinator(
            hass,
            entry,
            make_modem_client(hass, sub.data),
            name=sub.title,
            interval_s=interval,
            subentry_id=sub_id,
        )
        await modem.async_restore()
        runtime.modems[sub_id] = modem

    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type != SUBENTRY_SWITCH:
            continue
        sw = SwitchCoordinator(
            hass,
            entry,
            make_switch_client(hass, sub.data),
            name=sub.title,
            interval_s=interval,
            subentry_id=sub_id,
        )
        runtime.switches[sub_id] = sw

    # First polls side by side: a device behind a closed firewall times out,
    # and one after another they would hold HA's startup for minutes.
    # A device being down must not keep the whole site from loading.
    await asyncio.gather(
        *(c.async_refresh() for c in runtime.devices.values()),
        *(m.async_refresh() for m in runtime.modems.values()),
        *(w.async_refresh() for w in runtime.switches.values()),
    )
    for coord in runtime.devices.values():
        if coord.data:
            site.infra_macs |= {i.mac for i in coord.data.interfaces.values() if i.mac}

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if opts.get(CONF_AUTO_CONFIGURE, DEFAULT_AUTO_CONFIGURE):
        entry.async_create_background_task(
            hass, _async_provision(hass, entry, runtime), f"{DOMAIN} provision"
        )

        # Again every few minutes: the owner may switch the firewall off and
        # on, re-import it or reorder it; HA must not be locked out for good.
        async def _again(_now) -> None:
            await _async_provision(hass, entry, runtime)

        entry.async_on_unload(
            async_track_time_interval(hass, _again, PROVISION_INTERVAL)
        )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_provision(
    hass: HomeAssistant, entry: SiteHealthConfigEntry, runtime: SiteRuntime
) -> None:
    """Set up the rest on the core router (forward rule, probes), once per load."""
    from homeassistant.components import persistent_notification

    from .provision import async_provision_core

    site = runtime.site.site
    ha_ip = entry.data.get(CONF_HA_IP)
    if not site or not ha_ip:
        return
    result = await async_provision_core(
        runtime.site.client,
        ha_ip=ha_ip,
        neighbors=site.neighbors.values(),
        segments=site.segments,
    )
    runtime.provision = {
        "done": result.done,
        "already": result.already,
        "failed": result.failed,
    }
    if not result.done and not result.failed:
        persistent_notification.async_dismiss(
            hass, f"{DOMAIN}_{entry.entry_id}_provision"
        )
        return
    lines = [f"- added: {x}" for x in result.done]
    lines += [f"- **failed**: {x}" for x in result.failed]
    persistent_notification.async_create(
        hass,
        "Site Health configured the core router "
        f"{entry.data.get('host')}:\n\n" + "\n".join(lines),
        title=f"Site Health: {entry.title}",
        notification_id=f"{DOMAIN}_{entry.entry_id}_provision",
    )
    if result.done:
        for coord in [*runtime.all(), *runtime.switches.values()]:
            await coord.async_request_refresh()


async def _async_reload(hass: HomeAssistant, entry: SiteHealthConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SiteHealthConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# --- family list ----------------------------------------------------------------


def loaded_runtimes(hass: HomeAssistant) -> list[tuple[ConfigEntry, SiteRuntime]]:
    return [
        (e, e.runtime_data)
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if isinstance(getattr(e, "runtime_data", None), SiteRuntime)
    ]


def _pick_runtime(hass: HomeAssistant, entry_id: str | None) -> SiteRuntime:
    runtimes = loaded_runtimes(hass)
    if entry_id:
        for entry, runtime in runtimes:
            if entry.entry_id == entry_id:
                return runtime
        raise ServiceValidationError(f"Site {entry_id} is not loaded")
    if len(runtimes) != 1:
        raise ServiceValidationError("Several sites are loaded: pass entry_id")
    return runtimes[0][1]


def _check_mac(mac: str) -> str:
    mac = norm_mac(mac).replace("-", ":")
    if not _MAC_RE.match(mac):
        raise ServiceValidationError(f"Not a MAC address: {mac}")
    return mac


def _vlan_for(site: SiteCoordinator, place: str) -> int | None:
    return {PLACE_FAMILY: site.family_vlan, PLACE_ADMIN: site.admin_vlan}.get(place)


async def async_set_segment(
    hass: HomeAssistant,
    mac: str,
    place: str,
    name: str = "",
    entry_id: str | None = None,
) -> dict:
    """Put a Wi-Fi client in the family or admin VLAN, or back to guests.

    Only entries for the VLANs managed here (family, admin) are touched; a MAC
    mapped by hand to any other VLAN is left alone and reported.
    """
    mac = _check_mac(mac)
    if place not in (PLACE_GUEST, PLACE_FAMILY, PLACE_ADMIN):
        raise ServiceValidationError(f"Unknown segment: {place}")
    runtime = _pick_runtime(hass, entry_id)
    site = runtime.site
    managed = {site.family_vlan, site.admin_vlan}
    target = _vlan_for(site, place)
    legacy = bool(site.site and site.site.wifi_backend == "wireless")
    cl = site.client
    acl_list = cl.wireless_access_list if legacy else cl.wifi_access_list
    acl_add = cl.add_wireless_access if legacy else cl.add_wifi_access
    acl_set = cl.set_wireless_access if legacy else cl.set_wifi_access
    acl_del = cl.remove_wireless_access if legacy else cl.remove_wifi_access
    kick = cl.kick_wireless_client if legacy else cl.kick_wifi_client
    try:
        rows = [
            r
            for r in await acl_list()
            if norm_mac(r.get("mac-address")) == mac and r.get(".id")
        ]
        foreign = [r for r in rows if as_int(r.get("vlan-id")) not in managed]
        if foreign:
            other = as_int(foreign[0].get("vlan-id"))
            raise ServiceValidationError(
                f"{mac} is mapped to VLAN {other} by hand; change it on the router"
            )
        current = [r for r in rows if as_int(r.get("vlan-id")) in managed]
        if target is None:
            for row in current:
                await acl_del(row[".id"])
            status = "guest" if current else "already_guest"
        elif current and as_int(current[0].get("vlan-id")) == target:
            status = f"already_{place}"
        elif current:
            await acl_set(
                current[0][".id"],
                {"vlan-id": str(target), "comment": place_comment(place, name, target)},
            )
            for extra in current[1:]:
                await acl_del(extra[".id"])
            status = place
        else:
            await acl_add(mac, target, place_comment(place, name, target))
            status = place
        kicked = await kick(mac) if not status.startswith("already") else 0
    except MikrotikError as err:
        raise HomeAssistantError(f"Router refused the change: {err}") from err
    await site.async_refresh()  # the panel re-reads right after this
    return {"mac": mac, "status": status, "reconnected": kicked}


async def async_add_family(
    hass: HomeAssistant, mac: str, name: str = "", entry_id: str | None = None
) -> dict:
    return await async_set_segment(hass, mac, PLACE_FAMILY, name, entry_id)


async def async_remove_family(
    hass: HomeAssistant, mac: str, entry_id: str | None = None
) -> dict:
    return await async_set_segment(hass, mac, PLACE_GUEST, "", entry_id)


async def async_set_name(
    hass: HomeAssistant, mac: str, name: str, entry_id: str | None = None
) -> dict:
    mac = _check_mac(mac)
    runtime = _pick_runtime(hass, entry_id)
    name = name.strip()[:64]
    if name:
        runtime.names[mac] = name
    else:
        runtime.names.pop(mac, None)
    runtime.site.save_names()
    # The next poll writes the name to the router's lease comment.
    await runtime.site.async_refresh()
    return {"mac": mac, "name": name}


def find_switch(hass: HomeAssistant, subentry_id: str) -> SwitchCoordinator:
    for _entry, runtime in loaded_runtimes(hass):
        if subentry_id in runtime.switches:
            return runtime.switches[subentry_id]
    raise ServiceValidationError(f"Switch {subentry_id} is not loaded")


def _find_modem(hass: HomeAssistant, subentry_id: str) -> ModemCoordinator:
    for _entry, runtime in loaded_runtimes(hass):
        if subentry_id in runtime.modems:
            return runtime.modems[subentry_id]
    raise ServiceValidationError(f"Modem {subentry_id} is not loaded")


def _register_services(hass: HomeAssistant) -> None:
    base = {vol.Optional("entry_id"): cv.string}

    async def add(call: ServiceCall) -> dict:
        return await async_add_family(
            hass, call.data[ATTR_MAC], call.data.get(ATTR_NAME, ""),
            call.data.get("entry_id"),
        )  # fmt: skip

    async def remove(call: ServiceCall) -> dict:
        return await async_remove_family(
            hass, call.data[ATTR_MAC], call.data.get("entry_id")
        )

    async def set_segment(call: ServiceCall) -> dict:
        return await async_set_segment(
            hass, call.data[ATTR_MAC], call.data["segment"],
            call.data.get(ATTR_NAME, ""), call.data.get("entry_id"),
        )  # fmt: skip

    async def modem_sim(call: ServiceCall) -> None:
        coord = _find_modem(hass, call.data["modem"])
        try:
            await coord.async_select_sim(call.data["slot"])
        except TandemError as err:
            raise HomeAssistantError(str(err)) from err

    async def refresh(call: ServiceCall) -> None:
        for _entry, runtime in loaded_runtimes(hass):
            for coord in runtime.all():
                await coord.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_FAMILY,
        add,
        schema=vol.Schema(
            {
                vol.Required(ATTR_MAC): cv.string,
                vol.Optional(ATTR_NAME): cv.string,
                **base,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_FAMILY,
        remove,
        schema=vol.Schema({vol.Required(ATTR_MAC): cv.string, **base}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SEGMENT,
        set_segment,
        schema=vol.Schema(
            {
                vol.Required(ATTR_MAC): cv.string,
                vol.Required("segment"): vol.In(
                    [PLACE_GUEST, PLACE_FAMILY, PLACE_ADMIN]
                ),
                vol.Optional(ATTR_NAME): cv.string,
                **base,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MODEM_SIM,
        modem_sim,
        schema=vol.Schema(
            {
                vol.Required("modem"): cv.string,
                vol.Required("slot"): vol.All(vol.Coerce(int), vol.In([1, 2])),
            }
        ),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)
