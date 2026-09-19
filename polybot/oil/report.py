"""Compatibility alias for oilbot.report."""

import importlib
import sys

_impl = importlib.import_module("oilbot.report")
sys.modules[__name__] = _impl
