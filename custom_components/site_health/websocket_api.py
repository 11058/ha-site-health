"""Websocket commands for the sidebar panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import to_dict


def _device_view(coord) -> dict[str, Any]:
    data = coord.data
    return {
        "key": coord.subentry_id or coord.config_entry.entry_id,
        "subentry_id": coord.subentry_id,
        "name": coord.device_name,
        "host": coord.host,
        "online": coord.last_update_success,
        "error": coord.last_error,
        "last_update": coord.last_update_success_time.isoformat()
        if getattr(coord, "last_update_success_time", None)
        else None,
        "system": to_dict(data.system) if data else None,
        "interfaces": [to_dict(i) for i in data.interfaces.values()] if data else [],
        "radio_links": [to_dict(r) for r in data.radio_links.values()] if data else [],
    }


def site_overview(hass: HomeAssistant) -> list[dict[str, Any]]:
    from . import loaded_runtimes

    sites = []
    for entry, runtime in loaded_runtimes(hass):
        coord = runtime.site
        site = coord.site
        sites.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "family_vlan": coord.family_vlan,
                "core": _device_view(coord),
                "devices": [_device_view(c) for c in runtime.devices.values()],
                "wans": [to_dict(w) for w in site.wans.values()] if site else [],
                "probes": [to_dict(p) for p in site.probes.values()] if site else [],
                "tunnels": [to_dict(t) for t in site.tunnels.values()] if site else [],
                "segments": [to_dict(s) for s in site.segments.values()]
                if site
                else [],
                "clients": [to_dict(c) for c in site.clients.values()] if site else [],
                "neighbors": [to_dict(n) for n in site.neighbors.values()]
                if site
                else [],
            }
        )
    return sites


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    connection.send_result(msg["id"], {"sites": site_overview(hass)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict) -> None:
    """Push a bare 'changed' event whenever any coordinator finishes a poll."""
    from . import loaded_runtimes

    unsubs = []

    @callback
    def _changed() -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"changed": True})
        )

    for _entry, runtime in loaded_runtimes(hass):
        for coord in runtime.all():
            unsubs.append(coord.async_add_listener(_changed))

    @callback
    def _unsub() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = _unsub
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/family_add",
        vol.Required("mac"): str,
        vol.Optional("name", default=""): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_family_add(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import async_add_family

    try:
        result = await async_add_family(
            hass, msg["mac"], msg.get("name", ""), msg.get("entry_id")
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/family_remove",
        vol.Required("mac"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_family_remove(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import async_remove_family

    try:
        result = await async_remove_family(hass, msg["mac"], msg.get("entry_id"))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (ws_overview, ws_subscribe, ws_family_add, ws_family_remove):
        websocket_api.async_register_command(hass, handler)
