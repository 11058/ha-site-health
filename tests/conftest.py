"""Test bootstrap.

parsers.py and models.py are pure; load them under a synthetic package so the
integration's __init__ (which imports Home Assistant) never runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "site_health"
PKG = "site_health_under_test"


def load_module(name: str):
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(COMPONENT)]
        sys.modules[PKG] = pkg
    full = f"{PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, COMPONENT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def parsers():
    return load_module("parsers")


@pytest.fixture
def gw():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "routeros_gw.json").read_text()
    )
