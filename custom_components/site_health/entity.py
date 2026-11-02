"""Shared entity plumbing."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeviceCoordinator


def device_key(coordinator: DeviceCoordinator) -> str:
    return coordinator.subentry_id or coordinator.config_entry.entry_id


class SiteHealthEntity(CoordinatorEntity[DeviceCoordinator]):
    """Entity bound to one polled RouterOS device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DeviceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{device_key(coordinator)}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        system = c.data.system if c.data else None
        macs = set()
        if c.data:
            macs = {
                (CONNECTION_NETWORK_MAC, i.mac.lower())
                for i in c.data.interfaces.values()
                if i.mac and i.type in ("ether", "bridge")
            }
        info = DeviceInfo(
            identifiers={(DOMAIN, device_key(c))},
            connections=macs,
            name=c.device_name,
            manufacturer="MikroTik",
            model=(system.model or system.board) if system else None,
            sw_version=system.version if system else None,
            hw_version=system.firmware if system else None,
            serial_number=system.serial if system and system.serial else None,
            configuration_url=f"https://{c.host}",
        )
        # No via_device: deprecated since 2026.9 and it can abort entity setup
        # (see ha-raysharp-nvr entity.py); nesting is only cosmetic here.
        return info
