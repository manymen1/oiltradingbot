"""Compatibility alias for oilbot.config."""

import importlib
import sys

_impl = importlib.import_module("oilbot.config")
sys.modules[__name__] = _impl
