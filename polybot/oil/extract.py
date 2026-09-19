"""Compatibility alias for oilbot.extract."""

import importlib
import sys

_impl = importlib.import_module("oilbot.extract")
sys.modules[__name__] = _impl
