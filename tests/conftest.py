"""Test setup.

- `vtech_lan` is importable as a top-level package (it only uses relative imports, so it works both here
  and as `custom_components.vtech_baby.vtech_lan` inside Home Assistant).
- The integration itself is importable as `custom_components.vtech_baby`: the repo root goes on sys.path.
- The development SDK (user-supplied, never committed) is in `vtech-lan-kit/sdk/<arch>/`, or `$VTECH_SDK_DIR`.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "custom_components", "vtech_baby")
sys.path.insert(0, ROOT)
# Last, not first: the package has platform modules named like stdlib modules (select.py).
sys.path.append(PKG)
