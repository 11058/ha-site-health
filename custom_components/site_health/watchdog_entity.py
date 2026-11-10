"""Entities of an auto restart rule: on/off switch, state sensor, reset button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .watchdog import ACTING, DOWN, GAVE_UP, OK, Watchdog

STATES = [OK, DOWN, ACTING, GAVE_UP]


class WatchdogEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, wd: Watchdog, key: str) -> None:
        self.wd = wd
        self._attr_unique_id = f"{wd.sub_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, wd.sub_id)},
            name=f"Автоперезапуск: {wd.title}",
            manufacturer="Site Health",
            model="Автоперезапуск",
        )

    async def async_added_to_hass(self) -> None:
        cb = self.async_write_ha_state
        self.wd.listeners.append(cb)
        self.async_on_remove(lambda: self.wd.listeners.remove(cb))


class WatchdogSwitch(WatchdogEntity, SwitchEntity):
    """On = the rule may act; off = paused (still checks and notifies)."""

    _attr_translation_key = "watchdog_enabled"

    def __init__(self, wd: Watchdog) -> None:
        super().__init__(wd, "enabled")

    @property
    def is_on(self) -> bool:
        return not self.wd.st.paused

    async def async_turn_on(self, **kwargs) -> None:
        await self.wd.set_paused(False)

    async def async_turn_off(self, **kwargs) -> None:
        await self.wd.set_paused(True)


class WatchdogSensor(WatchdogEntity, SensorEntity):
    _attr_translation_key = "watchdog_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES

    def __init__(self, wd: Watchdog) -> None:
        super().__init__(wd, "state")

    @property
    def native_value(self) -> str:
        return self.wd.st.state

    @property
    def extra_state_attributes(self) -> dict:
        v = self.wd.view()
        return {
            "last_detail": v["last_detail"],
            "since": v["since"],
            "step": v["step"],
            "cycles_used": v["cycles_used"],
            "notify_only": v["notify_only"],
            "last_event": v["log"][0]["text"] if v["log"] else None,
        }


class WatchdogResetButton(WatchdogEntity, ButtonEntity):
    _attr_translation_key = "watchdog_reset"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, wd: Watchdog) -> None:
        super().__init__(wd, "reset")

    async def async_press(self) -> None:
        await self.wd.reset()
