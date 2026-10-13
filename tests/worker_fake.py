"""SDK factory for a real worker process in tests: `--sdk-factory worker_fake:make --path <tests dir>`.

`fake_sdk` imports `vtech_lan` as a top-level package; in the worker it is only importable under the
integration's name, so the modules are aliased first (no sys.path change, so select.py can't shadow).
A camera whose UID starts with CRASH segfaults the worker on connect, natively.
"""
import ctypes
import importlib
import sys

for _sub in ("", ".errors", ".ops", ".crypto"):
    sys.modules.setdefault(f"vtech_lan{_sub}", importlib.import_module(f"custom_components.vtech_baby.vtech_lan{_sub}"))

from fake_sdk import FakeSdk  # noqa: E402


class CrashingSdk(FakeSdk):
    def connect(self, uid, sid, authkey, timeout_s):
        if uid.startswith("CRASH"):
            ctypes.string_at(0)                     # SIGSEGV in native code
        return super().connect(uid, sid, authkey, timeout_s)


def make(libdir):
    sdk = CrashingSdk()
    sdk.libdir = libdir
    # Reported through sdk_info()["lan_only"], so a test can check the worker imported no Home Assistant.
    sdk.lan_only_stats = lambda: {"homeassistant_modules": sorted(
        m for m in sys.modules if m.split(".")[0] == "homeassistant")}
    return sdk
