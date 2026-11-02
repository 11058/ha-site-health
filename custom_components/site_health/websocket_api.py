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
        "lte": data.lte if data else {},
    }


def _modem_view(coord) -> dict[str, Any]:
    from .tandem import signal_quality

    data = coord.data
    info = to_dict(data) if data else None
    if info:
        info.pop("iccid", None)
        info["quality"] = signal_quality(data)
    return {
        "subentry_id": coord.subentry_id,
        "name": coord.device_name,
        "host": coord.host,
        "online": coord.last_update_success,
        "error": coord.last_error,
        "info": info,
    }


def _switch_view(coord) -> dict[str, Any]:
    return {
        "subentry_id": coord.subentry_id,
        "name": coord.device_name,
        "host": coord.host,
        "online": coord.last_update_success,
        "error": coord.last_error,
        "info": to_dict(coord.data) if coord.data else None,
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
                "clients": [
                    {**to_dict(c), "alias": runtime.names.get(c.mac, "")}
                    for c in site.clients.values()
                ]
                if site
                else [],
                "admin_vlan": coord.admin_vlan,
                "service_routes": [to_dict(r) for r in site.service_routes]
                if site
                else [],
                "route_rules": [to_dict(r) for r in site.route_rules] if site else [],
                "caps": site.caps if site else [],
                "wifi_backend": site.wifi_backend if site else "",
                "modems": [_modem_view(m) for m in runtime.modems.values()],
                "switches": [_switch_view(w) for w in runtime.switches.values()],
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
        for coord in [
            *runtime.all(),
            *runtime.modems.values(),
            *runtime.switches.values(),
        ]:
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_segment",
        vol.Required("mac"): str,
        vol.Required("segment"): vol.In(["guest", "family", "admin"]),
        vol.Optional("name", default=""): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_set_segment(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import async_set_segment

    try:
        result = await async_set_segment(
            hass, msg["mac"], msg["segment"], msg.get("name", ""), msg.get("entry_id")
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/modem_sim",
        vol.Required("modem"): str,
        vol.Required("slot"): vol.In([1, 2]),
    }
)
@websocket_api.async_response
async def ws_modem_sim(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import _find_modem
    from .tandem import TandemError

    try:
        await _find_modem(hass, msg["modem"]).async_select_sim(msg["slot"])
    except (HomeAssistantError, TandemError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"slot": msg["slot"]})


async def _switch_action(hass, connection, msg, action) -> None:
    from . import find_switch
    from .ssc_switch import SwitchError

    try:
        await find_switch(hass, msg["switch"]).async_run(action)
    except (HomeAssistantError, SwitchError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/switch_vlan",
        vol.Required("switch"): str,
        vol.Required("port"): int,
        vol.Required("vlan"): vol.Any(
            vol.All(int, vol.Range(min=1, max=4094)), "trunk"
        ),
    }
)
@websocket_api.async_response
async def ws_switch_vlan(hass: HomeAssistant, connection, msg: dict) -> None:
    await _switch_action(
        hass, connection, msg, lambda c: c.set_port_vlan(msg["port"], msg["vlan"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/switch_port",
        vol.Required("switch"): str,
        vol.Required("port"): int,
        vol.Required("action"): vol.In(["restart", "poe_on", "poe_off"]),
    }
)
@websocket_api.async_response
async def ws_switch_port(hass: HomeAssistant, connection, msg: dict) -> None:
    port, act = msg["port"], msg["action"]
    if act == "restart":
        action = lambda c: c.restart_port(port)  # noqa: E731
    else:
        action = lambda c: c.set_poe(port, act == "poe_on")  # noqa: E731
    await _switch_action(hass, connection, msg, action)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/modem_balance", vol.Required("modem"): str}
)
@websocket_api.async_response
async def ws_modem_balance(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import _find_modem
    from .tandem import TandemError

    try:
        await _find_modem(hass, msg["modem"]).async_request_balance()
    except (HomeAssistantError, TandemError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"requested": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_name",
        vol.Required("mac"): str,
        vol.Required("name"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_set_name(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import async_set_name

    try:
        result = await async_set_name(
            hass, msg["mac"], msg["name"], msg.get("entry_id")
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], result)


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    for handler in (
        ws_overview,
        ws_subscribe,
        ws_family_add,
        ws_family_remove,
        ws_set_segment,
        ws_modem_sim,
        ws_switch_vlan,
        ws_switch_port,
        ws_modem_balance,
        ws_set_name,
    ):
        websocket_api.async_register_command(hass, handler)
