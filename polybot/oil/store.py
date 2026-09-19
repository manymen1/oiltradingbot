"""Compatibility alias for oilbot.store."""

import importlib
import sys

_impl = importlib.import_module("oilbot.store")
sys.modules[__name__] = _impl
