"""Connectivity binary sensors: devices, uplinks, netwatch probes, tunnels."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant

from . import SiteHealthConfigEntry
from .coordinator import DeviceCoordinator
from .dynamic import setup_dynamic
from .entity import SiteHealthEntity
from .models import DeviceSnapshot, SiteSnapshot
from .parsers import slug


class SiteBinary(SiteHealthEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: DeviceCoordinator,
        key: str,
        value_fn: Callable[[DeviceSnapshot], bool | None],
        *,
        translation_key: str,
        placeholders: dict[str, str] | None = None,
        attrs_fn: Callable[[DeviceSnapshot], dict] | None = None,
    ) -> None:
        super().__init__(coordinator, key)
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        self._attr_translation_key = translation_key
        self._attr_translation_placeholders = placeholders or {}

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        try:
            return self._value_fn(self.coordinator.data)
        except (KeyError, AttributeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._attrs_fn or self.coordinator.data is None:
            return None
        try:
            return self._attrs_fn(self.coordinator.data)
        except (KeyError, AttributeError):
            return None


class DeviceOnline(SiteHealthEntity, BinarySensorEntity):
    """Is the device answering its REST API? Stays available when it is not."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator: DeviceCoordinator) -> None:
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


def _builder(c: DeviceCoordinator) -> Iterable[tuple[str, Callable]]:
    yield "online", lambda: DeviceOnline(c)
    data = c.data
    if not isinstance(data, SiteSnapshot):
        return
    for iface, wan in data.wans.items():
        key = slug(iface)
        ph = {"wan": wan.comment or iface}
        yield f"wan_{key}", lambda i=iface, k=key, ph=ph: SiteBinary(
            c, f"wan_{k}", lambda d, i=i: d.wans[i].bound and not d.wans[i].disabled,
            translation_key="wan", placeholders=ph,
            attrs_fn=lambda d, i=i: {
                "interface": i, "status": d.wans[i].status,
                "link": d.wans[i].running, "disabled": d.wans[i].disabled,
            },
        )  # fmt: skip
    for key, probe in data.probes.items():
        yield f"probe_{key}", lambda k=key, name=probe.name: SiteBinary(
            c, f"probe_{k}", lambda d, k=k: d.probes[k].up,
            translation_key="probe", placeholders={"probe": name},
            attrs_fn=lambda d, k=k: {
                "host": d.probes[k].host, "since": d.probes[k].since,
            },
        )  # fmt: skip
    for key, tun in data.tunnels.items():
        if tun.dial_in:
            continue
        yield f"tunnel_{key}", lambda k=key, name=tun.name: SiteBinary(
            c, f"tunnel_{k}", lambda d, k=k: d.tunnels[k].connected,
            translation_key="tunnel", placeholders={"tunnel": name},
            attrs_fn=lambda d, k=k: {
                "endpoint": d.tunnels[k].endpoint,
                "handshake_age_s": d.tunnels[k].handshake_age_s,
            },
        )  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: SiteHealthConfigEntry, async_add_entities
) -> None:
    setup_dynamic(entry, entry.runtime_data.all(), async_add_entities, _builder)
