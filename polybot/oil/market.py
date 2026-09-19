"""Compatibility alias for oilbot.market."""

import importlib
import sys

_impl = importlib.import_module("oilbot.market")
sys.modules[__name__] = _impl
