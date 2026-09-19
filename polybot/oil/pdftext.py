"""Compatibility alias for oilbot.pdftext."""

import importlib
import sys

_impl = importlib.import_module("oilbot.pdftext")
if __name__ == "__main__":
    raise SystemExit(_impl.main())
sys.modules[__name__] = _impl
