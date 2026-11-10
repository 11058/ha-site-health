"""Switches: auto restart rules on/off."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import SiteHealthConfigEntry
from .watchdog_entity import WatchdogSwitch


async def async_setup_entry(
    hass: HomeAssistant, entry: SiteHealthConfigEntry, async_add_entities
) -> None:
    for sub_id, wd in entry.runtime_data.watchdogs.items():
        async_add_entities([WatchdogSwitch(wd)], config_subentry_id=sub_id)
