"""Compatibility alias for oilbot.clock."""

import importlib
import sys

_impl = importlib.import_module("oilbot.clock")
sys.modules[__name__] = _impl
