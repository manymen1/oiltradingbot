"""Compatibility alias for oilbot.replay."""

import importlib
import sys

_impl = importlib.import_module("oilbot.replay")
sys.modules[__name__] = _impl
