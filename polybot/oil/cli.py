"""Compatibility alias for oilbot.cli."""

import importlib
import sys

_impl = importlib.import_module("oilbot.cli")
if __name__ == "__main__":
    raise SystemExit(_impl.main())
sys.modules[__name__] = _impl
