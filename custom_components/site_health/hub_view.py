"""HTTP endpoint the hub's script talks to (see hub_vpn.py).

No HA login: the hub authenticates with the gateway's random token in the URL
and, when the gateway names one, must come from the hub's own address. It can
only read the wanted public keys of its site and report back.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import CONF_HUB_IP, SUBENTRY_GATEWAY
from .hub_vpn import VIEW_URL

_LOGGER = logging.getLogger(__name__)


def _find(hass, token: str) -> tuple[Any, str, Any] | None:
    from . import loaded_runtimes

    for entry, runtime in loaded_runtimes(hass):
        for sid, sub in entry.subentries.items():
            if sub.subentry_type != SUBENTRY_GATEWAY:
                continue
            if secrets.compare_digest(str(sub.data.get("token", "")), token):
                return runtime, sid, sub
    return None


def _allowed(request: web.Request, sub) -> bool:
    want = str(sub.data.get(CONF_HUB_IP, "") or "").strip()
    if not want:
        return True
    try:
        return ipaddress.ip_address(request.remote or "") == ipaddress.ip_address(want)
    except ValueError:
        return False


class HubView(HomeAssistantView):
    url = VIEW_URL
    name = "api:site_health:hub"
    requires_auth = False

    async def _resolve(self, request: web.Request, token: str):
        found = _find(request.app["hass"], token)
        if found is None:
            return None
        runtime, sid, sub = found
        if not _allowed(request, sub):
            _LOGGER.warning(
                "site_health hub %s: request from %s refused", sub.title, request.remote
            )
            return None
        state = runtime.gateways.get(sid)
        if state is None:
            return None
        return runtime, state

    async def get(self, request: web.Request, token: str) -> web.Response:
        found = await self._resolve(request, token)
        if found is None:
            return web.Response(status=404)
        _runtime, state = found
        return self.json(state.wanted())

    async def post(self, request: web.Request, token: str) -> web.Response:
        found = await self._resolve(request, token)
        if found is None:
            return web.Response(status=404)
        runtime, state = found
        try:
            body = await request.json()
        except ValueError:
            return web.Response(status=400)
        if not isinstance(body, dict):
            return web.Response(status=400)
        state.report(body)
        runtime.save_vpn()
        return self.json({"ok": True})
