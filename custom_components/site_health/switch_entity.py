"""Entities for SSC web-smart switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitchCoordinator
from .ssc_switch import SwitchPort


class SwitchEntity(CoordinatorEntity[SwitchCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SwitchCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.subentry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        d = c.data
        info = DeviceInfo(
            identifiers={(DOMAIN, c.subentry_id)},
            name=c.device_name,
            manufacturer="SSC",
            model=(d.model if d else None) or "Web-smart switch",
            sw_version=d.firmware if d else None,
            configuration_url=f"http://{c.host}",
        )
        if d and d.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, d.mac.lower())}
        return info

    def _port(self, index: int) -> SwitchPort | None:
        d = self.coordinator.data
        return d.ports[index] if d and index < len(d.ports) else None


class SwitchOnline(SwitchEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator: SwitchCoordinator) -> None:
        super().__init__(coordinator, "online")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"host": self.coordinator.host, "error": self.coordinator.last_error}


class SwitchValue(SwitchEntity, SensorEntity):
    def __init__(self, coordinator, key, fn, **attrs) -> None:
        super().__init__(coordinator, key)
        self._fn = fn
        self._attr_translation_key = f"switch_{key}"
        for attr, value in attrs.items():
            setattr(self, f"_attr_{attr}", value)

    @property
    def native_value(self) -> Any:
        return self._fn(self.coordinator.data) if self.coordinator.data else None


class PortSensor(SwitchEntity, SensorEntity):
    """Link state of one port; VLAN, PoE and learned MACs as attributes."""

    _attr_translation_key = "switch_port"

    def __init__(self, coordinator: SwitchCoordinator, port: SwitchPort) -> None:
        super().__init__(coordinator, f"port_{port.index}")
        self._index = port.index
        self._attr_translation_placeholders = {"port": port.name}

    @property
    def native_value(self) -> str | None:
        port = self._port(self._index)
        if port is None:
            return None
        return port.speed if port.up else "down"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        port = self._port(self._index)
        if port is None:
            return None
        return {
            "vlan": port.vlan,
            "trunk": port.trunk,
            "uplink": port.uplink,
            "poe_w": port.poe_w,
            "poe_enabled": port.poe_enabled,
            "macs": port.macs,
        }


class PortPower(SwitchEntity, SensorEntity):
    _attr_translation_key = "switch_port_power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SwitchCoordinator, port: SwitchPort) -> None:
        super().__init__(coordinator, f"port_{port.index}_power")
        self._index = port.index
        self._attr_translation_placeholders = {"port": port.name}
        # Only ports that power something now are on by default.
        self._attr_entity_registry_enabled_default = bool(port.poe_w)

    @property
    def native_value(self) -> float | None:
        port = self._port(self._index)
        return port.poe_w if port else None


class PortRestart(SwitchEntity, ButtonEntity):
    """Power-cycle the PoE device on a port (camera, access point)."""

    _attr_translation_key = "switch_port_restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwitchCoordinator, port: SwitchPort) -> None:
        super().__init__(coordinator, f"port_{port.index}_restart")
        self._index = port.index
        self._attr_translation_placeholders = {"port": port.name}

    async def async_press(self) -> None:
        await self.coordinator.async_run(lambda c: c.restart_port(self._index))


def _ports(c: SwitchCoordinator) -> list[SwitchPort]:
    return c.data.ports if c.data else []


def switch_sensors(c: SwitchCoordinator) -> list[SensorEntity]:
    out: list[SensorEntity] = [
        SwitchValue(
            c, "temperature", lambda d: d.temperature,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SwitchValue(
            c, "poe_total", lambda d: d.poe_total_w,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        ),
    ]  # fmt: skip
    for port in _ports(c):
        out.append(PortSensor(c, port))
        if port.poe_capable:
            out.append(PortPower(c, port))
    return out


def switch_binaries(c: SwitchCoordinator) -> list[BinarySensorEntity]:
    return [SwitchOnline(c)]


def switch_buttons(c: SwitchCoordinator) -> list[ButtonEntity]:
    return [PortRestart(c, p) for p in _ports(c) if p.poe_capable and not p.uplink]
