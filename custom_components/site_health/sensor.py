"""Sensors: device vitals, uplinks, probes, tunnels, radio links, segments."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import SiteHealthConfigEntry
from .const import SEGMENT_ORDER
from .coordinator import DeviceCoordinator
from .dynamic import setup_dynamic
from .entity import SiteHealthEntity
from .models import DeviceSnapshot, SiteSnapshot
from .parsers import slug

ValueFn = Callable[[DeviceSnapshot], Any]


class SiteSensor(SiteHealthEntity, SensorEntity):
    def __init__(
        self,
        coordinator: DeviceCoordinator,
        key: str,
        value_fn: ValueFn,
        *,
        name: str | None = None,
        translation_key: str | None = None,
        placeholders: dict[str, str] | None = None,
        attrs_fn: Callable[[DeviceSnapshot], dict] | None = None,
        **attrs: Any,
    ) -> None:
        super().__init__(coordinator, key)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        if translation_key:
            self._attr_translation_key = translation_key
            self._attr_translation_placeholders = placeholders or {}
        else:
            self._attr_name = name
        for attr, value in attrs.items():
            setattr(self, f"_attr_{attr}", value)

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if data is None:
            return None
        try:
            return self._value_fn(data)
        except (KeyError, AttributeError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self._attrs_fn or self.coordinator.data is None:
            return None
        try:
            return self._attrs_fn(self.coordinator.data)
        except (KeyError, AttributeError, TypeError):
            return None


def _boot_time(d: DeviceSnapshot) -> datetime | None:
    if d.system.uptime_s is None:
        return None
    boot = dt_util.utcnow() - timedelta(seconds=d.system.uptime_s)
    # Round so the state does not tick every poll.
    return boot.replace(second=0, microsecond=0)


def _mbps(value: float | None) -> float | None:
    return round(value / 1_000_000, 2) if value is not None else None


MBPS = {
    "native_unit_of_measurement": UnitOfDataRate.MEGABITS_PER_SECOND,
    "device_class": SensorDeviceClass.DATA_RATE,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 2,
}


def _device_sensors(c: DeviceCoordinator) -> Iterable[tuple[str, Callable]]:
    yield "boot", lambda: SiteSensor(
        c, "boot", _boot_time, translation_key="boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    )  # fmt: skip
    yield "cpu", lambda: SiteSensor(
        c, "cpu", lambda d: d.system.cpu_load, translation_key="cpu_load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    )  # fmt: skip
    yield "memory", lambda: SiteSensor(
        c, "memory", lambda d: d.system.memory_used_pct, translation_key="memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    )  # fmt: skip
    if c.data and c.data.system.temperature is not None:
        yield "temperature", lambda: SiteSensor(
            c, "temperature", lambda d: d.system.temperature,
            translation_key="temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
        )  # fmt: skip
    yield "version", lambda: SiteSensor(
        c, "version", lambda d: d.system.version, translation_key="version",
        entity_category=EntityCategory.DIAGNOSTIC,
    )  # fmt: skip


def _radio_sensors(c: DeviceCoordinator) -> Iterable[tuple[str, Callable]]:
    data = c.data
    if data is None:
        return
    for key, link in data.radio_links.items():
        label = link.peer_name or link.mac
        ph = {"peer": label}
        yield f"link_{key}_signal", lambda k=key, ph=ph: SiteSensor(
            c, f"link_{k}_signal",
            lambda d, k=k: d.radio_links[k].signal_dbm,
            translation_key="link_signal", placeholders=ph,
            native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT,
            attrs_fn=lambda d, k=k: {
                "mac": d.radio_links[k].mac,
                "interface": d.radio_links[k].interface,
                "ssid": d.radio_links[k].ssid,
                "band": d.radio_links[k].band,
                "uptime_s": d.radio_links[k].uptime_s,
            },
        )  # fmt: skip
        for direction in ("tx", "rx"):
            yield f"link_{key}_{direction}", lambda k=key, dr=direction, ph=ph: (
                SiteSensor(
                    c, f"link_{k}_{dr}",
                    lambda d, k=k, dr=dr: getattr(d.radio_links[k], f"{dr}_rate_mbps"),
                    translation_key=f"link_{dr}_rate", placeholders=ph,
                    native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
                    device_class=SensorDeviceClass.DATA_RATE,
                    state_class=SensorStateClass.MEASUREMENT,
                )
            )  # fmt: skip


def _site_sensors(c: DeviceCoordinator) -> Iterable[tuple[str, Callable]]:
    data = c.data
    if not isinstance(data, SiteSnapshot):
        return
    for iface, wan in data.wans.items():
        ph = {"wan": wan.comment or iface}
        key = slug(iface)
        yield f"wan_{key}_ip", lambda i=iface, k=key, ph=ph: SiteSensor(
            c, f"wan_{k}_ip", lambda d, i=i: d.wans[i].address or None,
            translation_key="wan_ip", placeholders=ph,
            attrs_fn=lambda d, i=i: {
                "interface": i, "gateway": d.wans[i].gateway,
                "status": d.wans[i].status,
            },
        )  # fmt: skip
        for direction in ("rx", "tx"):
            yield f"wan_{key}_{direction}", lambda i=iface, k=key, dr=direction, ph=ph: (  # noqa: E501
                SiteSensor(
                    c, f"wan_{k}_{dr}",
                    lambda d, i=i, dr=dr: _mbps(getattr(d.wans[i], f"{dr}_bps")),
                    translation_key=f"wan_{dr}", placeholders=ph, **MBPS,
                )
            )  # fmt: skip
    for key, probe in data.probes.items():
        ph = {"probe": probe.name}
        if probe.rtt_ms is None:  # "simple" netwatch type has no RTT
            continue
        yield f"probe_{key}_rtt", lambda k=key, ph=ph: SiteSensor(
            c, f"probe_{k}_rtt", lambda d, k=k: d.probes[k].rtt_ms,
            translation_key="probe_rtt", placeholders=ph,
            native_unit_of_measurement=UnitOfTime.MILLISECONDS,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
        )  # fmt: skip
        if probe.loss_pct is not None:
            yield f"probe_{key}_loss", lambda k=key, ph=ph: SiteSensor(
                c, f"probe_{k}_loss", lambda d, k=k: d.probes[k].loss_pct,
                translation_key="probe_loss", placeholders=ph,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            )  # fmt: skip
    for key, tun in data.tunnels.items():
        if tun.dial_in:
            continue
        ph = {"tunnel": tun.name}
        yield f"tunnel_{key}_handshake", lambda k=key, ph=ph: SiteSensor(
            c, f"tunnel_{k}_handshake", lambda d, k=k: d.tunnels[k].handshake_age_s,
            translation_key="tunnel_handshake", placeholders=ph,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            attrs_fn=lambda d, k=k: {"endpoint": d.tunnels[k].endpoint},
        )  # fmt: skip
    kinds = {s.kind for s in data.segments.values()}
    for kind in [k for k in SEGMENT_ORDER if k in kinds]:
        yield f"clients_{kind}", lambda kind=kind: SiteSensor(
            c, f"clients_{kind}",
            lambda d, kind=kind: sum(
                1 for cl in d.clients.values()
                if cl.online and cl.segment_kind == kind
            ),
            translation_key=f"clients_{kind}",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="clients",
        )  # fmt: skip
    yield "clients_total", lambda: SiteSensor(
        c, "clients_total",
        lambda d: sum(1 for cl in d.clients.values() if cl.online),
        translation_key="clients_total",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="clients",
    )  # fmt: skip
    yield "family_devices", lambda: SiteSensor(
        c, "family_devices",
        lambda d: sum(1 for cl in d.clients.values() if cl.family),
        translation_key="family_devices",
        native_unit_of_measurement="devices",
    )  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: SiteHealthConfigEntry, async_add_entities
) -> None:
    runtime = entry.runtime_data

    def builder(c: DeviceCoordinator):
        yield from _device_sensors(c)
        yield from _radio_sensors(c)
        yield from _site_sensors(c)

    setup_dynamic(entry, runtime.all(), async_add_entities, builder)
    for sub_id, modem in entry.runtime_data.modems.items():
        from .modem_entity import modem_sensors

        async_add_entities(modem_sensors(modem), config_subentry_id=sub_id)
