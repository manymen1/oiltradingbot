"""Compatibility alias for oilbot.schema."""

import importlib
import sys

_impl = importlib.import_module("oilbot.schema")
sys.modules[__name__] = _impl
