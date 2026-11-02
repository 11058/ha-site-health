"""Site Health: MikroTik network health and family Wi-Fi list for one site."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_MAC,
    ATTR_NAME,
    CONF_FAMILY_VLAN,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SITE_NAME,
    CONF_TLS,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_WG_STALE,
    DEFAULT_FAMILY_VLAN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WG_STALE,
    DOMAIN,
    SERVICE_ADD_FAMILY,
    SERVICE_REFRESH,
    SERVICE_REMOVE_FAMILY,
    SUBENTRY_DEVICE,
)
from .coordinator import DeviceCoordinator, SiteCoordinator
from .mikrotik import MikrotikClient, MikrotikError
from .parsers import as_int, family_comment, norm_mac

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@dataclass
class SiteRuntime:
    site: SiteCoordinator
    devices: dict[str, DeviceCoordinator] = field(default_factory=dict)

    def all(self) -> list[DeviceCoordinator]:
        return [self.site, *self.devices.values()]


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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    from .panel import async_register_panel
    from .websocket_api import async_register_websocket

    async_register_websocket(hass)
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
        wg_stale_s=opts.get(CONF_WG_STALE, DEFAULT_WG_STALE),
    )
    await site.async_config_entry_first_refresh()
    runtime = SiteRuntime(site=site)

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
            peer_names=site.peer_names,
        )
        # A radio being down must not keep the whole site from loading.
        await coord.async_refresh()
        runtime.devices[sub_id] = coord
        if coord.data:
            site.infra_macs |= {i.mac for i in coord.data.interfaces.values() if i.mac}

    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


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


async def async_add_family(
    hass: HomeAssistant, mac: str, name: str = "", entry_id: str | None = None
) -> dict:
    """Map a MAC to the family VLAN on the core router's Wi-Fi access list."""
    mac = _check_mac(mac)
    runtime = _pick_runtime(hass, entry_id)
    site = runtime.site
    vlan = site.family_vlan
    try:
        rows = await site.client.wifi_access_list()
        mine = [r for r in rows if norm_mac(r.get("mac-address")) == mac]
        if any(as_int(r.get("vlan-id")) == vlan for r in mine):
            result = {"mac": mac, "status": "already_family"}
        elif mine:
            # An admin (VLAN 99) or other mapping is never overwritten from HA.
            other = as_int(mine[0].get("vlan-id"))
            raise ServiceValidationError(
                f"{mac} is already mapped to VLAN {other}; change it on the router"
            )
        else:
            await site.client.add_wifi_access(mac, vlan, family_comment(name, vlan))
            kicked = await site.client.kick_wifi_client(mac)
            result = {"mac": mac, "status": "added", "reconnected": kicked}
    except MikrotikError as err:
        raise HomeAssistantError(f"Router refused the change: {err}") from err
    await site.async_refresh()  # the panel re-reads right after this
    return result


async def async_remove_family(
    hass: HomeAssistant, mac: str, entry_id: str | None = None
) -> dict:
    mac = _check_mac(mac)
    runtime = _pick_runtime(hass, entry_id)
    site = runtime.site
    removed = 0
    try:
        for row in await site.client.wifi_access_list():
            if norm_mac(row.get("mac-address")) != mac:
                continue
            if as_int(row.get("vlan-id")) != site.family_vlan or not row.get(".id"):
                continue
            await site.client.remove_wifi_access(row[".id"])
            removed += 1
        kicked = await site.client.kick_wifi_client(mac) if removed else 0
    except MikrotikError as err:
        raise HomeAssistantError(f"Router refused the change: {err}") from err
    await site.async_refresh()  # the panel re-reads right after this
    return {"mac": mac, "status": "removed" if removed else "not_family",
            "reconnected": kicked}  # fmt: skip


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
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)
