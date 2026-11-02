"""Add entities as the things they describe appear (a new WAN, probe, link)."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity

from .coordinator import DeviceCoordinator

Builder = Callable[[DeviceCoordinator], Iterable[tuple[str, Callable[[], Entity]]]]


def setup_dynamic(
    entry: ConfigEntry,
    coordinators: Iterable[DeviceCoordinator],
    async_add_entities: Callable,
    builder: Builder,
) -> None:
    for coord in coordinators:
        known: set[str] = set()

        @callback
        def _check(coord: DeviceCoordinator = coord, known: set[str] = known) -> None:
            # Builders cope with data=None (device down at startup): they yield
            # only the entities that make sense without data, e.g. "online".
            new = []
            for key, factory in builder(coord):
                if key in known:
                    continue
                known.add(key)
                new.append(factory())
            if not new:
                return
            if coord.subentry_id:
                async_add_entities(new, config_subentry_id=coord.subentry_id)
            else:
                async_add_entities(new)

        _check()
        entry.async_on_unload(coord.async_add_listener(_check))
