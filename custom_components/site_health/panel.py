"""Sidebar panel: a single web component served from the integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ELEMENT, PANEL_STATIC, PANEL_URL

_LOGGER = logging.getLogger(__name__)

WWW = Path(__file__).parent / "www"
BUNDLE = "site-health-panel.js"


async def async_register_panel(hass: HomeAssistant) -> None:
    if not (WWW / BUNDLE).is_file():
        _LOGGER.warning("Panel bundle missing, sidebar panel not registered")
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_STATIC, str(WWW), cache_headers=False)]
    )
    # Cache-bust with the manifest version so an update is picked up at once.
    from homeassistant.loader import async_get_integration

    version = (await async_get_integration(hass, DOMAIN)).version or "0"
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_ELEMENT,
        frontend_url_path=PANEL_URL,
        module_url=f"{PANEL_STATIC}/{BUNDLE}?v={version}",
        sidebar_title="Здоровье сети",
        sidebar_icon="mdi:lan-check",
        require_admin=False,
        config={},
    )
