"""Refresh button on each device."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from . import SiteHealthConfigEntry
from .coordinator import DeviceCoordinator
from .dynamic import setup_dynamic
from .entity import SiteHealthEntity


class RefreshButton(SiteHealthEntity, ButtonEntity):
    _attr_translation_key = "refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeviceCoordinator) -> None:
        super().__init__(coordinator, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant, entry: SiteHealthConfigEntry, async_add_entities
) -> None:
    setup_dynamic(
        entry,
        entry.runtime_data.all(),
        async_add_entities,
        lambda c: [("refresh", lambda: RefreshButton(c))],
    )
    for sub_id, modem in entry.runtime_data.modems.items():
        from .modem_entity import modem_buttons

        async_add_entities(modem_buttons(modem), config_subentry_id=sub_id)
    for sub_id, sw in entry.runtime_data.switches.items():
        from .switch_entity import switch_buttons

        async_add_entities(switch_buttons(sw), config_subentry_id=sub_id)
    for sub_id, wd in entry.runtime_data.watchdogs.items():
        from .watchdog_entity import WatchdogResetButton

        async_add_entities([WatchdogResetButton(wd)], config_subentry_id=sub_id)
