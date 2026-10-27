"""Module loader for a real worker process in tests: `--link-loader worker_fake:make --path <tests dir>`.

`fake_link` imports `vtech_lan` as a top-level package; in the worker it is only importable under the
integration's name, so the modules are aliased first (no sys.path change, so select.py can't shadow).
A camera whose UID starts with CRASH segfaults the worker on connect, natively.
"""
import ctypes
import importlib
import sys

for _sub in ("", ".errors", ".ops", ".media"):
    sys.modules.setdefault(f"vtech_lan{_sub}", importlib.import_module(f"custom_components.vtech_baby.vtech_lan{_sub}"))

from fake_link import FakeLinkModule  # noqa: E402


class CrashingModule(FakeLinkModule):
    def connect(self, uid, *args, **kwargs):
        if uid.startswith("CRASH"):
            ctypes.string_at(0)                     # SIGSEGV in native code
        return super().connect(uid, *args, **kwargs)

    def lan_only_stats(self):
        # Reported through module_info()["link"]["lan_only"], so a test can check the worker imported
        # no Home Assistant.
        return {"homeassistant_modules": sorted(m for m in sys.modules if m.split(".")[0] == "homeassistant")}


def make(_path):
    return CrashingModule()
