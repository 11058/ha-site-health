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
    CONF_ADMIN_VLAN,
    CONF_AUTO_CONFIGURE,
    CONF_CLIENT_NET,
    CONF_ENDPOINT,
    CONF_FAMILY_VLAN,
    CONF_HA_IP,
    CONF_HOST,
    CONF_HUB_IP,
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
    SUBENTRY_DEVICE,
    SUBENTRY_GATEWAY,
    SUBENTRY_MODEM,
    SUBENTRY_SWITCH,
    SUBENTRY_WATCHDOG,
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
        return {
            SUBENTRY_DEVICE: DeviceSubentryFlow,
            SUBENTRY_MODEM: ModemSubentryFlow,
            SUBENTRY_SWITCH: SwitchSubentryFlow,
            SUBENTRY_GATEWAY: GatewaySubentryFlow,
            SUBENTRY_WATCHDOG: WatchdogSubentryFlow,
        }

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
                CONF_ADMIN_VLAN, default=o.get(CONF_ADMIN_VLAN, DEFAULT_ADMIN_VLAN)
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
            data_schema=vol.Schema(_conn_schema(sub.data)),
            errors=errors,
            description_placeholders={"host": sub.data.get(CONF_HOST, "")},
        )


class ModemSubentryFlow(ConfigSubentryFlow):
    """A cellular router in front of the core router (Tandem-4GT)."""

    def _gateway_guess(self) -> str:
        runtime = getattr(self._get_entry(), "runtime_data", None)
        site = getattr(getattr(runtime, "site", None), "site", None)
        if site is None:
            return ""
        for wan in site.wans.values():
            if wan.kind == "dhcp" and wan.bound and wan.gateway:
                return wan.gateway
        return ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        from homeassistant.helpers.aiohttp_client import async_create_clientsession

        from .tandem import TandemAuthError, TandemClient, TandemError

        errors: dict[str, str] = {}
        if user_input is not None:
            client = TandemClient(
                async_create_clientsession(self.hass),
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                out = await client.status()
            except TandemAuthError:
                errors["base"] = "invalid_auth"
            except TandemError:
                errors["base"] = "cannot_connect"
            else:
                if not out.get("sim") and not out.get("system"):
                    errors["base"] = "not_tandem"
                else:
                    return self.async_create_entry(
                        title=user_input["name"],
                        data={k: v for k, v in user_input.items() if k != "name"},
                        unique_id=f"modem:{user_input[CONF_HOST]}",
                    )
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required("name", default=defaults.get("name", "Tandem-4GT")): str,
                vol.Required(
                    CONF_HOST, default=defaults.get(CONF_HOST, self._gateway_guess())
                ): str,
                vol.Required(
                    CONF_USERNAME, default=defaults.get(CONF_USERNAME, "root")
                ): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """New address or login of the 4G router (e.g. after a password change)."""
        from homeassistant.helpers.aiohttp_client import async_create_clientsession

        from .tandem import TandemAuthError, TandemClient, TandemError

        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**sub.data, **user_input}
            client = TandemClient(
                async_create_clientsession(self.hass),
                data[CONF_HOST],
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
            )
            try:
                await client.login()
                await client.status()
            except TandemAuthError:
                errors["base"] = "invalid_auth"
            except TandemError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_and_abort(self._get_entry(), sub, data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=sub.data.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_USERNAME, default=sub.data.get(CONF_USERNAME, "root")
                ): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": sub.title},
        )


class SwitchSubentryFlow(ConfigSubentryFlow):
    """A web-smart PoE switch (PS308GV3 / PS208GV3) watched by the core router."""

    def __init__(self) -> None:
        self._host: str = ""
        self._name: str = ""

    def _candidates(self) -> dict[str, str]:
        """Switches the core router pings (netwatch) in its switch subnet."""
        import ipaddress

        entry = self._get_entry()
        site = getattr(
            getattr(getattr(entry, "runtime_data", None), "site", None), "site", None
        )
        if site is None:
            return {}
        nets = [
            ipaddress.ip_network(g.network)
            for g in site.segments.values()
            if g.kind == "switches"
        ]
        known = {s.data.get(CONF_HOST) for s in entry.subentries.values()}
        found: dict[str, str] = {}
        for probe in site.probes.values():
            try:
                addr = ipaddress.ip_address(probe.host)
            except ValueError:
                continue
            if probe.host in known or not any(addr in n for n in nets):
                continue
            found[probe.host] = f"{probe.name} — {probe.host}"
        return found

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        candidates = self._candidates()
        if user_input is not None:
            choice = user_input["device"]
            if choice != MANUAL:
                self._host = choice
                self._name = candidates.get(choice, choice).split(" — ")[0]
            return await self.async_step_credentials()
        options = [
            selector.SelectOptionDict(value=h, label=label)
            for h, label in candidates.items()
        ]
        options.append(selector.SelectOptionDict(value=MANUAL, label="Ввести адрес"))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
            description_placeholders={"count": str(len(candidates))},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        from . import make_switch_client
        from .ssc_switch import SwitchAuthError, SwitchBusyError, SwitchError

        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get(CONF_HOST) or self._host).strip()
            data = {CONF_HOST: host, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await make_switch_client(self.hass, data).check()
            except SwitchBusyError:
                errors["base"] = "switch_busy"
            except SwitchAuthError:
                errors["base"] = "invalid_auth"
            except SwitchError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input.get("name") or self._name or host,
                    data=data,
                    unique_id=f"switch:{host}",
                )
        schema: dict = {vol.Required("name", default=self._name or "Switch"): str}
        if not self._host:
            schema[vol.Required(CONF_HOST)] = str
        schema[vol.Required(CONF_PASSWORD)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"host": self._host or ""},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """New address or password of the switch web page."""
        from . import make_switch_client
        from .ssc_switch import SwitchAuthError, SwitchBusyError, SwitchError

        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**sub.data, **user_input}
            data[CONF_HOST] = data[CONF_HOST].strip()
            try:
                await make_switch_client(self.hass, data).check()
            except SwitchBusyError:
                errors["base"] = "switch_busy"
            except SwitchAuthError:
                errors["base"] = "invalid_auth"
            except SwitchError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_and_abort(self._get_entry(), sub, data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=sub.data.get(CONF_HOST, "")): str,
                vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": sub.title},
        )


def _site_nets(entry) -> list[str]:
    """The site's own subnets, collapsed to its /16 when they all fit."""
    import ipaddress

    from .admin_vpn import site_allowed_ips

    runtime = getattr(entry, "runtime_data", None)
    site = getattr(getattr(runtime, "site", None), "site", None)
    nets = [g.network for g in site.segments.values()] if site else []
    if not nets:
        return []
    text = site_allowed_ips(nets, nets[0])
    return [str(ipaddress.ip_network(n.strip())) for n in text.split(",")]


def _default_client_net(nets: list[str]) -> str:
    """10.250.N.0/24 for a 10.N.x.x site, by the house numbering."""
    if nets and nets[0].startswith("10."):
        n = nets[0].split(".")[1]
        if n.isdigit() and int(n) < 250:
            return f"10.250.{int(n)}.0/24"
    return "10.250.1.0/24"


class GatewaySubentryFlow(ConfigSubentryFlow):
    """A provider's hub that admins dial in to when the site has no public address.

    HA gets no login on the hub. The hub runs a script that fetches this
    site's wanted public keys from HA once a minute and keeps its peers in
    line; its firewall lets this site's client network reach the site and
    nothing else. See hub_vpn.py.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._title = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        from .hub_vpn import new_token

        entry = self._get_entry()
        nets = _site_nets(entry)
        errors: dict[str, str] = {}
        if user_input is not None:
            import ipaddress

            try:
                net = ipaddress.ip_network(user_input[CONF_CLIENT_NET], strict=True)
                if user_input.get(CONF_HUB_IP):
                    ipaddress.ip_address(user_input[CONF_HUB_IP])
            except ValueError:
                errors["base"] = "bad_network"
            else:
                self._title = user_input["name"].strip() or "VPN"
                self._data = {
                    CONF_ENDPOINT: user_input[CONF_ENDPOINT].strip(),
                    CONF_CLIENT_NET: str(net),
                    CONF_HUB_IP: user_input.get(CONF_HUB_IP, "").strip(),
                    "site_nets": nets,
                    "token": new_token(),
                }
                return await self.async_step_script()
        d = user_input or {}
        schema = vol.Schema(
            {
                vol.Required("name", default=d.get("name", "")): str,
                vol.Required(CONF_ENDPOINT, default=d.get(CONF_ENDPOINT, "")): str,
                vol.Required(
                    CONF_CLIENT_NET,
                    default=d.get(CONF_CLIENT_NET, _default_client_net(nets)),
                ): str,
                vol.Optional(CONF_HUB_IP, default=d.get(CONF_HUB_IP, "")): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"nets": ", ".join(nets) or "?"},
        )

    def _script(self, data: Mapping[str, Any]) -> str:
        from .hub_vpn import hub_script

        entry = self._get_entry()
        ha_ip = entry.data.get(CONF_HA_IP) or "HA_IP"
        port = getattr(getattr(self.hass, "http", None), "server_port", None) or 8123
        return hub_script(
            site=entry.title,
            ha_url=f"http://{ha_ip}:{port}",
            token=data["token"],
            client_net=data[CONF_CLIENT_NET],
            site_nets=list(data.get("site_nets") or []),
            src_address=data.get(CONF_HUB_IP, ""),
        )

    async def async_step_script(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._title,
                data=self._data,
                unique_id=f"gw:{self._data[CONF_CLIENT_NET]}",
            )
        return self.async_show_form(
            step_id="script",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._title,
                "script": self._script(self._data),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        if user_input is not None:
            data = {**sub.data, **{k: v.strip() for k, v in user_input.items()}}
            return self.async_update_and_abort(self._get_entry(), sub, data=data)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ENDPOINT, default=sub.data.get(CONF_ENDPOINT, "")
                ): str,
                vol.Optional(CONF_HUB_IP, default=sub.data.get(CONF_HUB_IP, "")): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"script": self._script(sub.data)},
        )


NONE = "none"


class WatchdogSubentryFlow(ConfigSubentryFlow):
    """Auto restart: how to see that the internet is gone, and what to do."""

    def _runtime(self):
        return getattr(self._get_entry(), "runtime_data", None)

    def _schema(self, d: dict[str, Any]) -> vol.Schema:
        from .watchdog import DEFAULTS, STEP_WAIT_DEFAULT, STEPS, action_choices

        runtime = self._runtime()
        site = getattr(getattr(runtime, "site", None), "site", None)
        d = {**DEFAULTS, **d}
        sel = selector.SelectSelector
        cfg = selector.SelectSelectorConfig
        opt = selector.SelectOptionDict

        def num(lo: int, hi: int, unit: str | None) -> selector.NumberSelector:
            conf: dict[str, Any] = {
                "min": lo, "max": hi, "step": 1,
                "mode": selector.NumberSelectorMode.BOX,
            }  # fmt: skip
            if unit:
                conf["unit_of_measurement"] = unit
            return selector.NumberSelector(selector.NumberSelectorConfig(**conf))

        tables = ["main"]
        hosts: list[str] = []
        if site is not None:
            for wan in site.wans.values():
                for route in getattr(wan, "routes", []) or []:
                    if route.table and route.table not in tables:
                        tables.append(route.table)
            hosts = [p.host for p in site.probes.values() if p.host]
        actions = action_choices(runtime)
        step_opts = [opt(value=NONE, label="— нет —")] + [
            opt(value=k, label=v) for k, v in actions.items()
        ]
        modems = list(getattr(runtime, "modems", {}) or {})
        guess = {1: "", 2: "", 3: "", 4: ""}
        if modems:
            guess[1] = f"modem:{modems[0]}:reconnect"
            guess[2] = f"modem:{modems[0]}:reboot"
        notify = sorted(
            f"notify.{name}"
            for name in self.hass.services.async_services().get("notify", {})
        )
        fields: dict[Any, Any] = {
            vol.Required("name", default=d.get("name", "Нет интернета")): str,
            vol.Required("probe", default=d["probe"]): sel(
                cfg(options=["router", "netwatch", "ha"], translation_key="wd_probe")
            ),
            vol.Required("targets", default=d["targets"]): str,
            vol.Optional("routing_table", default=d["routing_table"]): sel(
                cfg(options=tables, custom_value=True)
            ),
            vol.Optional("netwatch_host", default=d["netwatch_host"] or ""): (
                sel(cfg(options=hosts, custom_value=True)) if hosts else str
            ),
            vol.Required("interval_s", default=d["interval_s"]): num(10, 600, "с"),
            vol.Required("down_after_min", default=d["down_after_min"]): num(
                1, 240, "мин"
            ),
        }
        for i in range(1, STEPS + 1):
            cur = d.get(f"step{i}", guess[i]) or NONE
            if cur != NONE and cur not in actions:
                step_opts.append(opt(value=cur, label=f"{cur} (нет на объекте)"))
            fields[vol.Required(f"step{i}", default=cur)] = sel(cfg(options=step_opts))
            fields[
                vol.Required(
                    f"step{i}_wait",
                    default=d.get(f"step{i}_wait", STEP_WAIT_DEFAULT[i]),
                )
            ] = num(1, 120, "мин")
        power = d.get("power_entity") or None
        fields[
            vol.Optional(
                "power_entity",
                description={"suggested_value": power} if power else None,
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "light", "input_boolean"])
        )
        fields[vol.Required("power_off_s", default=d["power_off_s"])] = num(3, 600, "с")
        fields[vol.Required("max_cycles", default=d["max_cycles"])] = num(1, 50, None)
        fields[vol.Required("window_h", default=d["window_h"])] = num(1, 168, "ч")
        fields[vol.Required("cooldown_min", default=d["cooldown_min"])] = num(
            0, 1440, "мин"
        )
        fields[vol.Required("notify_admins", default=d["notify_admins"])] = bool
        fields[
            vol.Optional("notify_services", default=list(d["notify_services"] or []))
        ] = sel(cfg(options=notify, multiple=True, custom_value=True))
        fields[vol.Required("notify_only", default=d["notify_only"])] = bool
        return vol.Schema(fields)

    @staticmethod
    def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
        data = dict(user_input)
        for k, v in list(data.items()):
            if isinstance(v, float) and v.is_integer():
                data[k] = int(v)
            if v == NONE:
                data[k] = ""
        data.setdefault("power_entity", "")
        return data

    def _errors(self, data: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if data["probe"] == "netwatch" and not data.get("netwatch_host"):
            errors["netwatch_host"] = "netwatch_needed"
        if data["probe"] != "netwatch" and not str(data.get("targets") or "").strip():
            errors["targets"] = "targets_needed"
        if any(data.get(f"step{i}") == "power" for i in range(1, 5)) and not data.get(
            "power_entity"
        ):
            errors["power_entity"] = "power_needed"
        return errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = self._clean(user_input)
            errors = self._errors(data)
            if not errors:
                title = data.pop("name")
                return self.async_create_entry(title=title, data=data)
        return self.async_show_form(
            step_id="user", data_schema=self._schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = self._clean(user_input)
            errors = self._errors(data)
            if not errors:
                title = data.pop("name")
                return self.async_update_and_abort(
                    self._get_entry(), sub, title=title, data=data
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(user_input or {**sub.data, "name": sub.title}),
            errors=errors,
        )
