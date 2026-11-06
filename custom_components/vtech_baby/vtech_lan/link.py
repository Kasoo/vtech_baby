"""Load `_vtech_link`, the Rust protocol library (vtech-link), and forward its log records.

The module is the `vtech-link` package, which Home Assistant installs from the manifest's requirement.
It is imported once per process and never unloaded. Its Rust threads never take the GIL: a daemon thread here drains its log ring into Python logging under
this module's logger.

Design: docs/vtech-link-design/75-python-api.md.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import threading
from typing import Any

from .errors import UnsupportedHost

_LOG = logging.getLogger(__name__)

MODULE_NAME = "_vtech_link"
DISTRIBUTION = "vtech-link"
LOG_WAIT_S = 1.0

_lock = threading.Lock()
_loaded: Any = None


def load() -> Any:
    """The module, imported once per process. Raises UnsupportedHost if it can't be imported here."""
    global _loaded
    with _lock:
        if _loaded is not None:
            return _loaded
        try:
            module = importlib.import_module(MODULE_NAME)
        except (ImportError, OSError) as e:
            raise UnsupportedHost(f"can't load {MODULE_NAME}: {e}", call="load") from e
        module.set_log_level(_LOG.getEffectiveLevel())
        threading.Thread(target=_forward_logs, args=(module,), name="vtech-link-logs", daemon=True).start()
        _loaded = module
        _LOG.debug("loaded %s %s from %s", MODULE_NAME, module.__version__, module.__file__)
        return module


def is_current(module: Any) -> bool:
    """False if a different version of the package was installed after `module` was loaded."""
    try:
        return importlib.metadata.version(DISTRIBUTION) == module.__version__.partition("+")[0]
    except importlib.metadata.PackageNotFoundError:
        return True


def info(module: Any) -> dict[str, Any]:
    """The module's version and LAN-only counters, and every live link, for diagnostics."""
    return {"version": module.__version__, "lan_only": module.lan_only_stats(), "links": module.links(),
            "logs_dropped": module.logs_dropped()}


def _forward_logs(module: Any) -> None:
    """Drain the module's log ring for the life of the process. Each record keeps its Rust module path
    in the message, since the logger name is this module's."""
    while True:
        try:
            records = module.next_logs(LOG_WAIT_S)
        except Exception:                                   # noqa: BLE001 - never let logging die
            _LOG.exception("reading %s logs failed", MODULE_NAME)
            return
        for level, target, message in records:
            _LOG.log(level, "%s: %s", target.removeprefix("vtech_link_core::"), message)
