"""Config flow: the core router, then each further MikroTik as a subentry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
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
    SUBENTRY_DEVICE,
)
from .mikrotik import MikrotikAuthError, MikrotikError
from .parsers import mikrotik_candidates

MANUAL = "__manual__"


def _addr(data: Mapping[str, Any]) -> str:
    """Identity of a device for duplicate checks: host plus explicit port."""
    port = data.get(CONF_PORT) or ""
    return f"{data.get(CONF_HOST)}:{port}"


def _conn_schema(defaults: Mapping[str, Any], *, with_host: bool = True) -> dict:
    schema: dict = {}
    if with_host:
        schema[vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, ""))] = str
    schema.update(
        {
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "ha-infra")
            ): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_TLS, default=defaults.get(CONF_TLS, True)): bool,
            vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, 0)): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=65535)
            ),
            vol.Optional(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, False)
            ): bool,
        }
    )
    return schema


async def _probe(hass: HomeAssistant, data: dict) -> tuple[str | None, dict]:
    """Return (error_key, identity row)."""
    from . import make_client

    client = make_client(hass, data)
    try:
        identity = await client.identity()
        resource = await client.resource()
    except MikrotikAuthError:
        return "invalid_auth", {}
    except MikrotikError:
        return "cannot_connect", {}
    if not identity.get("name"):
        return "not_routeros", {}
    return None, {**identity, "board": resource.get("board-name", "")}


class SiteHealthConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SiteHealthOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_DEVICE: DeviceSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            err, ident = await _probe(self.hass, user_input)
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                title = user_input.get(CONF_SITE_NAME) or ident["name"]
                return self.async_create_entry(title=title, data=user_input)
        defaults = user_input or {}
        schema = {
            vol.Required(CONF_SITE_NAME, default=defaults.get(CONF_SITE_NAME, "")): str,
            **_conn_schema(defaults),
        }
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            err, _ident = await _probe(self.hass, data)
            if not err:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = err
        schema = {
            vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            err, _ident = await _probe(self.hass, data)
            if not err:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = err
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_conn_schema(entry.data)),
            errors=errors,
        )


class SiteHealthOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = {
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=600)),
            vol.Optional(
                CONF_FAMILY_VLAN, default=o.get(CONF_FAMILY_VLAN, DEFAULT_FAMILY_VLAN)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=4094)),
            vol.Optional(
                CONF_WG_STALE, default=o.get(CONF_WG_STALE, DEFAULT_WG_STALE)
            ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add one more MikroTik (radio, AP, switch) with its own login."""

    def __init__(self) -> None:
        self._host: str | None = None
        self._title: str = ""

    def _known(self) -> set[str]:
        """host:port of the core router and every added device."""
        entry = self._get_entry()
        datas = [entry.data, *(s.data for s in entry.subentries.values())]
        return {_addr(d) for d in datas if d.get(CONF_HOST)}

    def _candidates(self) -> dict[str, str]:
        entry = self._get_entry()
        runtime = getattr(entry, "runtime_data", None)
        site = getattr(getattr(runtime, "site", None), "site", None)
        if site is None:
            return {}
        known = {k.split(":")[0] for k in self._known() if k.endswith(":")}
        found: dict[str, str] = {}
        for nb in mikrotik_candidates(site.neighbors):
            if nb.address in known:
                continue
            label = f"{nb.identity or nb.mac} — {nb.address}"
            if nb.board:
                label += f" ({nb.board})"
            found[nb.address] = label
        return found

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        candidates = self._candidates()
        if user_input is not None:
            choice = user_input["device"]
            if choice != MANUAL:
                self._host = choice
                self._title = candidates.get(choice, choice).split(" — ")[0]
            return await self.async_step_credentials()
        options = [
            selector.SelectOptionDict(value=host, label=label)
            for host, label in candidates.items()
        ]
        options.append(selector.SelectOptionDict(value=MANUAL, label="Ввести адрес"))
        schema = vol.Schema(
            {
                vol.Required("device"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={"count": str(len(candidates))},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            if self._host:
                data[CONF_HOST] = self._host
            if _addr(data) in self._known():
                return self.async_abort(reason="already_configured")
            err, ident = await _probe(self.hass, data)
            if err:
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title=ident.get("name") or self._title or data[CONF_HOST],
                    data=data,
                    unique_id=_addr(data),
                )
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                _conn_schema(user_input or {}, with_host=self._host is None)
            ),
            errors=errors,
            description_placeholders={"host": self._host or "", "name": self._title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**sub.data, **user_input}
            err, _ident = await _probe(self.hass, data)
            if not err:
                return self.async_update_and_abort(self._get_entry(), sub, data=data)
            errors["base"] = err
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_conn_schema(sub.data, with_host=False)),
            errors=errors,
            description_placeholders={"host": sub.data.get(CONF_HOST, "")},
        )
