"""Load `_vtech_link`, the Rust protocol library (vtech-link), and forward its log records.

The module is a Python extension built for this host's arch and libc (`_vtech_link.abi3.so`). It is
loaded once per process from an explicit path, and never unloaded. Its
Rust threads never take the GIL: a daemon thread here drains its log ring into Python logging under
this module's logger.

Design: docs/vtech-link-design/75-python-api.md.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import logging
import os
import threading
from typing import Any

from .errors import ModuleMismatch, UnsupportedHost

_LOG = logging.getLogger(__name__)

MODULE_NAME = "_vtech_link"
LOG_WAIT_S = 1.0

_lock = threading.Lock()
_loaded: tuple[str, Any] | None = None          # (path, module)


def load(path: str) -> Any:
    """The module at `path`, loaded once per process. Raises ModuleMismatch if a different path was loaded
    first, and UnsupportedHost if it can't be loaded here."""
    global _loaded
    path = os.path.abspath(path)
    with _lock:
        if _loaded is not None:
            if _loaded[0] != path:
                raise ModuleMismatch(f"this process already loaded {MODULE_NAME} from {_loaded[0]}", call="load")
            return _loaded[1]
        try:
            loader = importlib.machinery.ExtensionFileLoader(MODULE_NAME, path)
            spec = importlib.util.spec_from_file_location(MODULE_NAME, path, loader=loader)
            if spec is None:
                raise ImportError(f"no module spec for {path}")
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        except (ImportError, OSError) as e:
            raise UnsupportedHost(f"can't load {path}: {e}", call="load") from e
        module.set_log_level(_LOG.getEffectiveLevel())
        threading.Thread(target=_forward_logs, args=(module,), name="vtech-link-logs", daemon=True).start()
        _loaded = (path, module)
        _LOG.debug("loaded %s %s from %s", MODULE_NAME, module.__version__, path)
        return module


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
