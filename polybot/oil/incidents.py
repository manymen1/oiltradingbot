"""Compatibility alias for oilbot.incidents."""

import importlib
import sys

_impl = importlib.import_module("oilbot.incidents")
sys.modules[__name__] = _impl
