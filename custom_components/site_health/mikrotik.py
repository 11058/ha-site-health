"""Minimal async RouterOS v7 REST client.

Derived from ha-asic-fleet. RouterOS REST verbs:
    GET    /rest/<path>            list / read
    PUT    /rest/<path>            add
    PATCH  /rest/<path>/<id>       set
    DELETE /rest/<path>/<id>       remove
    POST   /rest/<path>/<command>  run a command (print with a query, ping, ...)

Every value RouterOS returns is a string; parsing lives in parsers.py so it can
be tested without a router.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
from aiohttp import (
    BasicAuth,
    ClientConnectorError,
    ClientError,
    ClientResponseError,
    ClientTimeout,
    ServerDisconnectedError,
)

from .const import HTTP_TIMEOUT

_LOGGER = logging.getLogger(__name__)


def net_error_text(err: BaseException) -> str:
    """A readable reason; aiohttp timeouts stringify to ''."""
    if isinstance(err, TimeoutError):
        return (
            "нет ответа (таймаут): устройство выключено или firewall "
            "не пропускает Home Assistant"
        )
    if isinstance(err, ClientConnectorError):
        os_err = getattr(err, "os_error", None)
        reason = getattr(os_err, "strerror", None) or str(os_err or err)
        return f"не удалось подключиться: {reason}"
    if isinstance(err, ServerDisconnectedError):
        return "устройство закрыло соединение"
    text = str(err).strip()
    return text or type(err).__name__


class MikrotikError(Exception):
    """Router did not answer or answered with an error."""


class MikrotikAuthError(MikrotikError):
    """Credentials rejected."""


class MikrotikClient:
    """Talks to one RouterOS box over its REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        port: int | None = None,
        tls: bool = True,
        verify_ssl: bool = False,
    ) -> None:
        self._session = session
        self._host = host
        self._auth = BasicAuth(username, password)
        scheme = "https" if tls else "http"
        netloc = f"{host}:{port}" if port else host
        self._base = f"{scheme}://{netloc}/rest"
        # A self-signed certificate is the norm on RouterOS; verification can
        # be switched back on for a box with a real certificate.
        self._ssl: Any = None if not tls else verify_ssl
        self._timeout = ClientTimeout(total=HTTP_TIMEOUT)

    @property
    def host(self) -> str:
        return self._host

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            async with self._session.request(
                method,
                url,
                auth=self._auth,
                json=payload,
                timeout=self._timeout,
                ssl=self._ssl,
            ) as resp:
                if resp.status in (401, 403):
                    raise MikrotikAuthError(
                        f"RouterOS rejected credentials for {self._host} "
                        f"({resp.status})"
                    )
                if resp.status >= 400:
                    detail = await resp.text()
                    raise MikrotikError(
                        f"{method} {path} failed: {resp.status} {detail[:200]}"
                    )
                text = await resp.text()
                if not text.strip():
                    return None
                try:
                    return json.loads(text)
                except ValueError:
                    return None
        except MikrotikError:
            raise
        except ClientResponseError as err:
            raise MikrotikError(
                f"{method} {path} failed: {err.status} {err.message}"
            ) from err
        except (ClientError, TimeoutError, OSError) as err:
            raise MikrotikError(f"{method} {path}: {net_error_text(err)}") from err

    async def _list(self, path: str, *, optional: bool = False) -> list[dict]:
        """GET a table. Optional tables (package not installed) yield []."""
        try:
            data = await self._request("GET", path)
        except MikrotikAuthError:
            raise
        except MikrotikError as err:
            if not optional:
                raise
            _LOGGER.debug("%s unavailable on %s: %s", path, self._host, err)
            return []
        return data if isinstance(data, list) else []

    async def _dict(self, path: str) -> dict[str, Any]:
        data = await self._request("GET", path)
        return data if isinstance(data, dict) else {}

    # --- system --------------------------------------------------------------

    async def identity(self) -> dict[str, Any]:
        return await self._dict("system/identity")

    async def resource(self) -> dict[str, Any]:
        return await self._dict("system/resource")

    async def routerboard(self) -> dict[str, Any]:
        try:
            return await self._dict("system/routerboard")
        except MikrotikAuthError:
            raise
        except MikrotikError:  # CHR / x86 have no routerboard menu
            return {}

    async def health(self) -> list[dict]:
        return await self._list("system/health", optional=True)

    # --- interfaces and links ------------------------------------------------

    async def interfaces(self) -> list[dict]:
        return await self._list("interface")

    async def vlans(self) -> list[dict]:
        return await self._list("interface/vlan", optional=True)

    async def addresses(self) -> list[dict]:
        return await self._list("ip/address")

    async def dhcp_clients(self) -> list[dict]:
        return await self._list("ip/dhcp-client", optional=True)

    async def pppoe_clients(self) -> list[dict]:
        return await self._list("interface/pppoe-client", optional=True)

    async def lte(self) -> list[dict]:
        return await self._list("interface/lte", optional=True)

    async def interface_list_members(self) -> list[dict]:
        return await self._list("interface/list/member", optional=True)

    async def default_routes(self) -> list[dict]:
        return await self._list("ip/route?dst-address=0.0.0.0/0", optional=True)

    async def host_route(self, ip: str) -> list[dict]:
        return await self._list(f"ip/route?dst-address={ip}/32", optional=True)

    async def routing_mangle(self) -> list[dict]:
        return await self._list("ip/firewall/mangle?action=mark-routing", optional=True)

    async def lte_monitor(self, name: str) -> dict:
        try:
            data = await self._request(
                "POST", "interface/lte/monitor", {"numbers": name, "once": ""}
            )
        except MikrotikAuthError:
            raise
        except MikrotikError:
            return {}
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    # legacy wireless package (RB951 and other pre-wifiwave2 boards)
    async def wireless_interfaces(self) -> list[dict]:
        return await self._list("interface/wireless", optional=True)

    async def wireless_registrations(self) -> list[dict]:
        return await self._list("interface/wireless/registration-table", optional=True)

    async def wireless_access_list(self) -> list[dict]:
        return await self._list("interface/wireless/access-list", optional=True)

    async def netwatch(self) -> list[dict]:
        return await self._list("tool/netwatch", optional=True)

    async def wireguard_peers(self) -> list[dict]:
        return await self._list("interface/wireguard/peers", optional=True)

    async def wifi_interfaces(self) -> list[dict]:
        return await self._list("interface/wifi", optional=True)

    async def wifi_registrations(self) -> list[dict]:
        return await self._list("interface/wifi/registration-table", optional=True)

    async def capsman_caps(self) -> list[dict]:
        return await self._list("interface/wifi/capsman/remote-cap", optional=True)

    async def neighbors(self) -> list[dict]:
        return await self._list("ip/neighbor", optional=True)

    # --- clients -------------------------------------------------------------

    async def leases(self) -> list[dict]:
        return await self._list("ip/dhcp-server/lease", optional=True)

    async def arp(self) -> list[dict]:
        return await self._list("ip/arp", optional=True)

    async def bridge_hosts(self) -> list[dict]:
        return await self._list("interface/bridge/host", optional=True)

    async def wifi_access_list(self) -> list[dict]:
        return await self._list("interface/wifi/access-list", optional=True)

    # --- writes --------------------------------------------------------------

    async def add_wifi_access(
        self, mac: str, vlan_id: int, comment: str
    ) -> dict[str, Any]:
        payload = {
            "action": "accept",
            "mac-address": mac,
            "vlan-id": str(vlan_id),
            "comment": comment,
        }
        data = await self._request("PUT", "interface/wifi/access-list", payload)
        return data if isinstance(data, dict) else {}

    async def set_wifi_access(self, entry_id: str, fields: dict[str, str]) -> None:
        await self._request("PATCH", f"interface/wifi/access-list/{entry_id}", fields)

    async def remove_wifi_access(self, entry_id: str) -> None:
        await self._request("DELETE", f"interface/wifi/access-list/{entry_id}")

    async def set_lease_comment(self, lease_id: str, comment: str) -> None:
        await self._request(
            "PATCH", f"ip/dhcp-server/lease/{lease_id}", {"comment": comment}
        )

    async def add_wireless_access(self, mac: str, vlan_id: int, comment: str) -> None:
        """Legacy wireless: per-MAC VLAN is vlan-mode=use-tag on the access list."""
        await self._request(
            "PUT",
            "interface/wireless/access-list",
            {"mac-address": mac, "vlan-mode": "use-tag", "vlan-id": str(vlan_id),
             "comment": comment},
        )  # fmt: skip

    async def set_wireless_access(self, entry_id: str, fields: dict[str, str]) -> None:
        await self._request(
            "PATCH", f"interface/wireless/access-list/{entry_id}", fields
        )

    async def remove_wireless_access(self, entry_id: str) -> None:
        await self._request("DELETE", f"interface/wireless/access-list/{entry_id}")

    async def kick_wireless_client(self, mac: str) -> int:
        kicked = 0
        for reg in await self.wireless_registrations():
            if reg.get("mac-address", "").upper() != mac.upper() or not reg.get(".id"):
                continue
            try:
                await self._request(
                    "DELETE", f"interface/wireless/registration-table/{reg['.id']}"
                )
                kicked += 1
            except MikrotikError as err:
                _LOGGER.debug("kick %s failed: %s", mac, err)
        return kicked

    async def kick_wifi_client(self, mac: str) -> int:
        """Drop a client's association so it reconnects and gets the new VLAN."""
        kicked = 0
        for reg in await self.wifi_registrations():
            if reg.get("mac-address", "").upper() != mac.upper() or not reg.get(".id"):
                continue
            try:
                await self._request(
                    "DELETE", f"interface/wifi/registration-table/{reg['.id']}"
                )
                kicked += 1
            except MikrotikError as err:  # it may have roamed meanwhile
                _LOGGER.debug("kick %s failed: %s", mac, err)
        return kicked
