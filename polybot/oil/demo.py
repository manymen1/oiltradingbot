"""Compatibility alias for oilbot.demo."""

import importlib
import sys

_impl = importlib.import_module("oilbot.demo")
sys.modules[__name__] = _impl
