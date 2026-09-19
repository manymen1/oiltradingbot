"""Compatibility alias for oilbot.research."""

import importlib
import sys

_impl = importlib.import_module("oilbot.research")
sys.modules[__name__] = _impl
