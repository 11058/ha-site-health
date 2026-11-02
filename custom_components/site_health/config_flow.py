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
    DEFAULT_AUTO_CONFIGURE,
    DEFAULT_FAMILY_VLAN,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WG_STALE,
    DOMAIN,
    SUBENTRY_DEVICE,
)
from .mikrotik import MikrotikAuthError, MikrotikError
from .parsers import mikrotik_candidates
from .provision import USERNAME, new_password, onboarding_script

MANUAL = "__manual__"
METHOD = "method"
METHOD_SCRIPT = "script"
METHOD_MANUAL = "manual"


def _method_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[METHOD_SCRIPT, METHOD_MANUAL],
            translation_key="method",
            mode=selector.SelectSelectorMode.LIST,
        )
    )


async def _ha_ip(hass: HomeAssistant, target: str) -> str:
    """The address the router will see HA connect from."""
    from homeassistant.components import network

    try:
        return await network.async_get_source_ip(hass, target_ip=target)
    except Exception:
        return ""


async def _suggest_gateway(hass: HomeAssistant) -> str:
    """HA's own segment gateway (x.y.z.1): the router address to stay in-segment.

    Talking to the router on the address of HA's own VLAN keeps the traffic
    there; the admin VLAN address would work too but means nothing to HA.
    """
    from homeassistant.components import network

    try:
        ip = await network.async_get_source_ip(hass)
    except Exception:
        return ""
    parts = ip.split(".")
    if len(parts) != 4 or ip.startswith("127."):
        return ""
    return ".".join([*parts[:3], "1"])


def _generated(host: str, ha_ip: str) -> dict[str, Any]:
    return {
        CONF_HOST: host,
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: new_password(),
        CONF_TLS: True,
        CONF_PORT: 0,
        CONF_VERIFY_SSL: False,
        CONF_HA_IP: ha_ip,
    }


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

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
            ha_ip = await _ha_ip(self.hass, host)
            self._data = {
                CONF_SITE_NAME: user_input[CONF_SITE_NAME],
                **_generated(host, ha_ip),
            }
            if user_input[METHOD] == METHOD_SCRIPT:
                return await self.async_step_onboard()
            return await self.async_step_manual()
        schema = {
            vol.Required(CONF_SITE_NAME): str,
            vol.Required(CONF_HOST, default=await _suggest_gateway(self.hass)): str,
            vol.Required(METHOD, default=METHOD_SCRIPT): _method_selector(),
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema))

    async def _finish(self) -> ConfigFlowResult:
        err, ident = await _probe(self.hass, self._data)
        if err:
            return err  # type: ignore[return-value]
        title = self._data.get(CONF_SITE_NAME) or ident["name"]
        return self.async_create_entry(title=title, data=self._data)

    async def async_step_onboard(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._finish()
            if not isinstance(result, str):
                return result
            errors["base"] = result
        return self.async_show_form(
            step_id="onboard",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "host": self._data[CONF_HOST],
                "ha_ip": self._data[CONF_HA_IP] or "?",
                "script": onboarding_script(
                    ha_ip=self._data[CONF_HA_IP] or "HA_IP",
                    password=self._data[CONF_PASSWORD],
                    core=True,
                ),
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            result = await self._finish()
            if not isinstance(result, str):
                return result
            errors["base"] = result
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(_conn_schema(user_input or {}, with_host=False)),
            errors=errors,
            description_placeholders={"host": self._data[CONF_HOST]},
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
            vol.Optional(
                CONF_AUTO_CONFIGURE,
                default=o.get(CONF_AUTO_CONFIGURE, DEFAULT_AUTO_CONFIGURE),
            ): bool,
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Add one more MikroTik (radio, AP, switch) with its own login."""

    def __init__(self) -> None:
        self._title: str = ""
        self._method: str = METHOD_SCRIPT
        self._data: dict[str, Any] = {}

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
            self._method = user_input[METHOD]
            choice = user_input["device"]
            if choice == MANUAL:
                return await self.async_step_address()
            self._title = candidates.get(choice, choice).split(" — ")[0]
            return await self._route(choice)
        options = [
            selector.SelectOptionDict(value=host, label=label)
            for host, label in candidates.items()
        ]
        options.append(selector.SelectOptionDict(value=MANUAL, label="Ввести адрес"))
        schema = vol.Schema(
            {
                vol.Required("device"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                ),
                vol.Required(METHOD, default=METHOD_SCRIPT): _method_selector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={"count": str(len(candidates))},
        )

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return await self._route(user_input[CONF_HOST].strip())
        return self.async_show_form(
            step_id="address",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
        )

    async def _route(self, host: str) -> SubentryFlowResult:
        core = self._get_entry().data
        ha_ip = core.get(CONF_HA_IP) or await _ha_ip(self.hass, host)
        self._data = _generated(host, ha_ip)
        if _addr(self._data) in self._known():
            return self.async_abort(reason="already_configured")
        if self._method == METHOD_SCRIPT:
            return await self.async_step_onboard()
        return await self.async_step_credentials()

    async def _finish(self) -> SubentryFlowResult | str:
        err, ident = await _probe(self.hass, self._data)
        if err:
            return err
        return self.async_create_entry(
            title=ident.get("name") or self._title or self._data[CONF_HOST],
            data=self._data,
            unique_id=_addr(self._data),
        )

    async def async_step_onboard(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._finish()
            if not isinstance(result, str):
                return result
            errors["base"] = result
        return self.async_show_form(
            step_id="onboard",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "host": self._data[CONF_HOST],
                "name": self._title or self._data[CONF_HOST],
                "script": onboarding_script(
                    ha_ip=self._data[CONF_HA_IP] or "HA_IP",
                    password=self._data[CONF_PASSWORD],
                    core=False,
                ),
            },
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            result = await self._finish()
            if not isinstance(result, str):
                return result
            errors["base"] = result
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(_conn_schema(user_input or {}, with_host=False)),
            errors=errors,
            description_placeholders={
                "host": self._data[CONF_HOST],
                "name": self._title or self._data[CONF_HOST],
            },
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
