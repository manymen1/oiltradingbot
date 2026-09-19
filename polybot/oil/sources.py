"""Compatibility alias for oilbot.sources."""

import importlib
import sys

_impl = importlib.import_module("oilbot.sources")
sys.modules[__name__] = _impl
