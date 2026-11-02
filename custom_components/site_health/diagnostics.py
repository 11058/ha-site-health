"""Diagnostics download with secrets removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SiteHealthConfigEntry
from .const import CONF_PASSWORD, CONF_USERNAME
from .websocket_api import site_overview

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "serial", "endpoint"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SiteHealthConfigEntry
) -> dict[str, Any]:
    sites = [s for s in site_overview(hass) if s["entry_id"] == entry.entry_id]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "subentries": [
            {
                "title": sub.title,
                "type": sub.subentry_type,
                "data": async_redact_data(dict(sub.data), TO_REDACT),
            }
            for sub in entry.subentries.values()
        ],
        "site": async_redact_data(sites[0], TO_REDACT) if sites else None,
    }
