"""Entities for a cellular router (Tandem-4GT)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
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
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ModemCoordinator
from .models import ModemInfo
from .tandem import signal_quality


class ModemEntity(CoordinatorEntity[ModemCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ModemCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.subentry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.subentry_id)},
            name=c.device_name,
            manufacturer="Microdrive",
            model="Tandem-4GT",
            configuration_url=f"http://{c.host}",
        )


class ModemSensor(ModemEntity, SensorEntity):
    def __init__(
        self,
        coordinator: ModemCoordinator,
        key: str,
        value_fn: Callable[[ModemInfo], Any],
        attrs_fn: Callable[[ModemInfo], dict] | None = None,
        **attrs: Any,
    ) -> None:
        super().__init__(coordinator, key)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        self._attr_translation_key = f"modem_{key}"
        for attr, value in attrs.items():
            setattr(self, f"_attr_{attr}", value)

    @property
    def native_value(self) -> Any:
        return self._value_fn(self.coordinator.data) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict | None:
        if self._attrs_fn and self.coordinator.data:
            return self._attrs_fn(self.coordinator.data)
        return None


def _since(info: ModemInfo):
    if not info.connected or info.uptime_s is None:
        return None
    return (dt_util.utcnow() - timedelta(seconds=info.uptime_s)).replace(
        second=0, microsecond=0
    )


MEAS = {"state_class": SensorStateClass.MEASUREMENT}


def modem_sensors(c: ModemCoordinator) -> list[SensorEntity]:
    dbm = {
        "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        "device_class": SensorDeviceClass.SIGNAL_STRENGTH,
        **MEAS,
    }
    return [
        ModemSensor(
            c, "active_sim", lambda d: d.active_sim,
            attrs_fn=lambda d: {
                "slots": d.sim_slots, "sim_status": d.sim_status,
                "last_by_slot": d.last_by_slot,
            },
        ),
        ModemSensor(c, "operator", lambda d: d.operator or None),
        ModemSensor(
            c, "network", lambda d: d.tech or None,
            attrs_fn=lambda d: {
                "band": d.band, "bandwidth_mhz": d.bandwidth_mhz,
                "registration": d.registration, "cell_id": d.cell_id,
                "roaming": d.roaming,
            },
        ),
        ModemSensor(c, "rsrp", lambda d: d.rsrp, **dbm),
        ModemSensor(
            c, "rsrq", lambda d: d.rsrq,
            native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS, **MEAS,
        ),
        ModemSensor(
            c, "sinr", lambda d: d.sinr,
            native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS, **MEAS,
        ),
        ModemSensor(c, "rssi", lambda d: d.rssi, **dbm),
        ModemSensor(
            c, "signal", lambda d: d.signal_pct,
            native_unit_of_measurement=PERCENTAGE, **MEAS,
            attrs_fn=lambda d: {"quality": signal_quality(d)},
        ),
        ModemSensor(
            c, "temperature", lambda d: d.temperature,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE, **MEAS,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        ModemSensor(
            c, "connected_since", _since,
            device_class=SensorDeviceClass.TIMESTAMP,
        ),
        ModemSensor(
            c, "balance", lambda d: d.balance,
            native_unit_of_measurement="RUB",
            device_class=SensorDeviceClass.MONETARY,
            attrs_fn=lambda d: {
                "message": d.balance_text, "received": d.balance_when,
            },
        ),
        ModemSensor(
            c, "failover", lambda d: (d.failover or {}).get("sim1_control") or "off",
            entity_category=EntityCategory.DIAGNOSTIC,
            attrs_fn=lambda d: d.failover,
        ),
    ]  # fmt: skip


class ModemBinary(ModemEntity, BinarySensorEntity):
    def __init__(
        self,
        coordinator: ModemCoordinator,
        key: str,
        value_fn: Callable[[ModemInfo], bool | None],
        device_class: BinarySensorDeviceClass | None,
    ) -> None:
        super().__init__(coordinator, key)
        self._value_fn = value_fn
        self._attr_translation_key = f"modem_{key}"
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool | None:
        return self._value_fn(self.coordinator.data) if self.coordinator.data else None


def modem_binaries(c: ModemCoordinator) -> list[BinarySensorEntity]:
    return [
        ModemBinary(
            c, "connected", lambda d: d.connected,
            BinarySensorDeviceClass.CONNECTIVITY,
        ),
        ModemBinary(c, "registered", lambda d: d.registered, None),
        ModemBinary(c, "roaming", lambda d: d.roaming, None),
    ]  # fmt: skip


class ModemSimButton(ModemEntity, ButtonEntity):
    """Switch the active SIM. Cuts the uplink for a few seconds."""

    def __init__(self, coordinator: ModemCoordinator, slot: int) -> None:
        super().__init__(coordinator, f"sim{slot}")
        self._slot = slot
        self._attr_translation_key = "modem_select_sim"
        self._attr_translation_placeholders = {"slot": str(slot)}
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_select_sim(self._slot)


class ModemBalanceButton(ModemEntity, ButtonEntity):
    """Ask the operator for the balance by a free SMS."""

    _attr_translation_key = "modem_balance_request"

    def __init__(self, coordinator: ModemCoordinator) -> None:
        super().__init__(coordinator, "balance_request")

    async def async_press(self) -> None:
        await self.coordinator.async_request_balance()


def modem_buttons(c: ModemCoordinator) -> list[ButtonEntity]:
    slots = (c.data.sim_slots if c.data else None) or 2
    buttons: list[ButtonEntity] = [ModemSimButton(c, s) for s in range(1, slots + 1)]
    buttons.append(ModemBalanceButton(c))
    return buttons
