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
                "provision": runtime.provision,
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/probe_remove",
        vol.Required("entry_id"): str,
        vol.Required("host"): str,
    }
)
@websocket_api.async_response
async def ws_probe_remove(hass: HomeAssistant, connection, msg: dict) -> None:
    """Delete the core router's netwatch check of a device that is gone."""
    from . import loaded_runtimes
    from .mikrotik import MikrotikError

    runtime = next(
        (r for e, r in loaded_runtimes(hass) if e.entry_id == msg["entry_id"]), None
    )
    if runtime is None:
        connection.send_error(msg["id"], "not_found", "site not loaded")
        return
    client = runtime.site.client
    try:
        rows = [r for r in await client.netwatch() if r.get("host") == msg["host"]]
        for row in rows:
            await client._request("DELETE", f"tool/netwatch/{row['.id']}")
    except MikrotikError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    await runtime.site.async_request_refresh()
    connection.send_result(msg["id"], {"removed": len(rows)})


def _vpn_site(hass: HomeAssistant, entry_id: str):
    from . import loaded_runtimes

    found = next(
        ((e, r) for e, r in loaded_runtimes(hass) if e.entry_id == entry_id), None
    )
    if found is None or found[1].site.site is None:
        raise HomeAssistantError("объект не загружен")
    entry, runtime = found
    coord = runtime.site
    site = coord.site
    admin = next(
        (g.network for g in site.segments.values() if g.vlan_id == coord.admin_vlan),
        "",
    )
    nets = [g.network for g in site.segments.values()]
    uplinks = [w.address for w in site.wans.values() if getattr(w, "address", "")]
    return entry, coord, admin, nets, uplinks


def _gateways(entry) -> dict[str, Any]:
    from .const import SUBENTRY_GATEWAY

    return {
        sid: sub
        for sid, sub in entry.subentries.items()
        if sub.subentry_type == SUBENTRY_GATEWAY
    }


def _gateway_client(hass: HomeAssistant, sub):
    from . import make_client

    return make_client(hass, dict(sub.data))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/vpn_state", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_vpn_state(hass: HomeAssistant, connection, msg: dict) -> None:
    """Every place an admin VPN can be made: the site itself and its gateways."""
    from . import admin_vpn
    from .const import CONF_ENDPOINT
    from .mikrotik import MikrotikError

    try:
        entry, coord, admin, nets, uplinks = _vpn_site(hass, msg["entry_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    allowed = admin_vpn.site_allowed_ips(nets, admin) if admin else ""
    options = []
    direct: dict[str, Any] = {"id": "", "name": coord.device_name, "direct": True}
    if not admin:
        reason = f"не найдена админская сеть VLAN {coord.admin_vlan}"
        direct |= {"available": False, "reason": reason}
    else:
        try:
            direct |= await admin_vpn.async_state(coord.client, admin, uplinks)
        except MikrotikError as err:
            direct |= {"available": False, "reason": str(err)}
    options.append(direct)
    for sid, sub in _gateways(entry).items():
        gw: dict[str, Any] = {"id": sid, "name": sub.title, "direct": False}
        try:
            gw |= await admin_vpn.async_state(
                _gateway_client(hass, sub),
                None,
                site=entry.title,
                endpoint=sub.data.get(CONF_ENDPOINT, ""),
            )
        except MikrotikError as err:
            gw |= {"available": False, "reason": str(err)}
        options.append(gw)
    for o in options:
        o["allowed_ips"] = allowed
    # Prefer a place clients can actually reach from the internet.
    best = next(
        (o["id"] for o in options if o.get("available") and o.get("endpoint")),
        next((o["id"] for o in options if o.get("available")), ""),
    )
    connection.send_result(msg["id"], {"options": options, "suggested": best})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/vpn_create",
        vol.Required("entry_id"): str,
        vol.Optional("gateway", default=""): str,
        vol.Optional("name", default=""): vol.All(str, vol.Length(max=60)),
        vol.Optional("endpoint", default=""): vol.All(
            str, vol.Match(r"^[A-Za-z0-9.:\-]*$")
        ),
    }
)
@websocket_api.async_response
async def ws_vpn_create(hass: HomeAssistant, connection, msg: dict) -> None:
    """Make a key pair now, add the peer, hand the config back exactly once."""
    from . import admin_vpn
    from .mikrotik import MikrotikError

    try:
        entry, coord, admin, nets, _uplinks = _vpn_site(hass, msg["entry_id"])
        if not admin:
            raise HomeAssistantError("не найдена админская сеть")
        allowed = admin_vpn.site_allowed_ips(nets, admin)
        if msg["gateway"]:
            sub = _gateways(entry).get(msg["gateway"])
            if sub is None:
                raise HomeAssistantError("шлюз не найден")
            client, net, site = _gateway_client(hass, sub), None, entry.title
        else:
            client, net, site = coord.client, admin, ""
        result = await admin_vpn.async_create(
            client,
            name=msg["name"].strip(),
            admin_net=net,
            endpoint=msg["endpoint"].strip(),
            allowed_ips=allowed,
            site=site,
        )
    except (HomeAssistantError, MikrotikError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    hass.async_create_task(coord.async_request_refresh())
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/vpn_remove",
        vol.Required("entry_id"): str,
        vol.Optional("gateway", default=""): str,
        vol.Required("peer_id"): str,
    }
)
@websocket_api.async_response
async def ws_vpn_remove(hass: HomeAssistant, connection, msg: dict) -> None:
    from . import admin_vpn
    from .mikrotik import MikrotikError

    try:
        entry, coord, _admin, _nets, _uplinks = _vpn_site(hass, msg["entry_id"])
        if msg["gateway"]:
            sub = _gateways(entry).get(msg["gateway"])
            if sub is None:
                raise HomeAssistantError("шлюз не найден")
            result = await admin_vpn.async_remove(
                _gateway_client(hass, sub), msg["peer_id"], site=entry.title
            )
        else:
            result = await admin_vpn.async_remove(coord.client, msg["peer_id"])
    except (HomeAssistantError, MikrotikError, ValueError) as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    hass.async_create_task(coord.async_request_refresh())
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
        ws_probe_remove,
        ws_vpn_state,
        ws_vpn_create,
        ws_vpn_remove,
    ):
        websocket_api.async_register_command(hass, handler)
