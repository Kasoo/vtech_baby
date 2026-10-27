"""Home Assistant's side of the worker (`worker.py`): start it, talk to it, notice when it dies.

`WorkerClient` is one per Home Assistant process. It hands out proxies with the same blocking methods
as `Session` and the live view, so the integration drives them exactly as before (from executor
threads). Every proxy call is a request over the socket (`ipc.py`).

If the worker dies (a crash in the module, or killed), every session it held is reported lost with
`WorkerDied`, and the next connect starts a new worker with the same module. The live-view socket is
bound here and passed to each worker, so the stream URL survives worker restarts.
"""
from __future__ import annotations

import itertools
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Protocol

from .errors import SessionGone, VtechError, WorkerDied
from .ipc import Channel

_LOG = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = __name__.rsplit(".", 2)[0]       # the integration's package, e.g. custom_components.vtech_baby
REQUEST_TIMEOUT_S = 120.0                  # backstop: every module call has its own, shorter timeout
STOP_TIMEOUT_S = 10.0


class WorkerHandle(Protocol):
    pid: int | None

    def wait(self, timeout: float | None = None) -> int | None: ...
    def kill(self) -> None: ...


def spawn_process(child: socket.socket, listen: socket.socket, extra_args: tuple[str, ...] = ()) -> WorkerHandle:
    """Start the worker as a child Python process (isolated mode: nothing from the environment)."""
    cmd = [sys.executable, "-I", os.path.join(HERE, "_worker_main.py"), PACKAGE,
           "--fd", str(child.fileno()), "--listen-fd", str(listen.fileno()), *extra_args]
    return subprocess.Popen(cmd, pass_fds=(child.fileno(), listen.fileno()), stdin=subprocess.DEVNULL)


def exit_reason(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code < 0:
        try:
            return signal.Signals(-code).name
        except ValueError:
            return f"signal {-code}"
    return f"exit status {code}"


class WorkerClient:
    def __init__(self, spawn: Callable[[socket.socket, socket.socket], WorkerHandle] = spawn_process,
                 on_crash: Callable[[str, int], None] | None = None, log_level: int | None = None):
        """`on_crash(reason, count)` runs on the reader thread when a worker dies unexpectedly."""
        self._spawn = spawn
        self.on_crash = on_crash
        self._log_level = log_level
        self._listen = socket.create_server(("127.0.0.1", 0), backlog=16)
        self.port: int = self._listen.getsockname()[1]
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.RLock()
        self._chan: Channel | None = None
        self._handle: WorkerHandle | None = None
        self.gen = 0                            # worker generation; proxies remember theirs
        self._link_path: str | None = None
        self._fingerprint: str | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[Future, int]] = {}
        self._sessions: dict[int, SessionProxy] = {}
        self._media: dict[str, MediaProxy] = {}
        self._talks: dict[str, TalkProxy] = {}
        self._stopping = False
        self.closed = False
        self.crashes = 0
        self.last_exit: str | None = None
        self.pid: int | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────
    def ensure(self, link_path: str, fingerprint: str = "") -> dict[str, Any]:
        """Make sure a worker runs with this `_vtech_link` module. A different module restarts the
        worker, so its sessions are lost and reconnect on the new one. Blocking."""
        with self._lock:
            if self.closed:
                raise SessionGone("the worker is shut down", call="load")
            wanted = (link_path, fingerprint)
            if self._chan is not None and wanted != (self._link_path, self._fingerprint):
                _LOG.info("module changed; restarting the worker")
                self._stop_worker()
            self._link_path, self._fingerprint = wanted
            if self._chan is None:
                self._start()
        return self.request("module_info")

    def _start(self) -> None:
        """Start a worker and load the module in it (lock held)."""
        if self._link_path is None:
            raise SessionGone("the module isn't loaded", call="load")
        ours, theirs = socket.socketpair()
        try:
            handle = self._spawn(theirs, self._listen)
        finally:
            theirs.close()
        self.gen += 1
        self._chan, self._handle, self._stopping = Channel(ours), handle, False
        threading.Thread(target=self._read, args=(self._chan, self.gen, handle), daemon=True,
                         name=f"vtech-worker-reader-{self.gen}").start()
        level = self._log_level if self._log_level is not None else logging.getLogger(PACKAGE).getEffectiveLevel()
        try:
            self.pid = self.request("init", self.token, level)
            self.request("load_link", self._link_path)
        except BaseException:
            self._stop_worker()
            raise
        _LOG.debug("worker %s started (pid %s)", self.gen, self.pid)

    def _ensure_running(self) -> int:
        with self._lock:
            if self.closed:
                raise SessionGone("the worker is shut down", call="worker")
            if self._chan is None:
                self._start()
            return self.gen

    def _stop_worker(self) -> None:
        """Ask the worker to exit, then make sure it has (lock held)."""
        chan, handle = self._chan, self._handle
        if chan is None:
            return
        self._stopping = True
        try:
            self.request("shutdown", timeout=STOP_TIMEOUT_S)
        except VtechError:
            pass
        chan.close()
        if handle is not None and handle.wait(STOP_TIMEOUT_S) is None:
            handle.kill()
            handle.wait(STOP_TIMEOUT_S)
        self._worker_gone(self.gen, WorkerDied("the worker was restarted", call="worker"))

    def stop(self) -> None:
        """Shut down for good (Home Assistant stopping)."""
        with self._lock:
            self.closed = True
            self._stop_worker()
        self._listen.close()

    @property
    def running(self) -> bool:
        return self._chan is not None

    # ── Requests ─────────────────────────────────────────────────────────────
    def request(self, kind: str, *args: Any, gen: int | None = None, timeout: float | None = REQUEST_TIMEOUT_S) -> Any:
        """Send one request and wait for its answer. Raises what the worker raised, or WorkerDied."""
        with self._lock:
            chan = self._chan
            if chan is None or (gen is not None and gen != self.gen):
                raise WorkerDied("the worker isn't running", call=kind)
            rid, fut = next(self._ids), Future()
            self._pending[rid] = (fut, self.gen)
        try:
            chan.send((kind, rid, *args))
        except OSError as err:
            self._pending.pop(rid, None)
            raise WorkerDied(f"the worker went away: {err}", call=kind) from err
        try:
            return fut.result(timeout)
        except FutureTimeout:
            self._pending.pop(rid, None)
            raise VtechError(f"the worker didn't answer {kind} within {timeout:.0f} s", call=kind) from None

    def _read(self, chan: Channel, gen: int, handle: WorkerHandle) -> None:
        try:
            while True:
                self._dispatch(chan.recv())
        except EOFError:
            pass
        except Exception:                                       # noqa: BLE001
            _LOG.exception("worker: bad message")
        code = handle.wait()
        with self._lock:
            if gen != self.gen or self._chan is not chan:
                return                                          # an old worker, already dealt with
            expected = self._stopping
            self.last_exit = exit_reason(code)
            if not expected:
                self.crashes += 1
            self._worker_gone(gen, WorkerDied(f"the worker stopped ({self.last_exit})", call="worker"))
        if not expected:
            _LOG.error("The VTech worker died (%s); reconnecting with a new one", self.last_exit)
            if self.on_crash is not None:
                self.on_crash(self.last_exit, self.crashes)

    def _worker_gone(self, gen: int, err: WorkerDied) -> None:
        """Fail everything that belonged to worker `gen` (lock held)."""
        if self._chan is not None and gen == self.gen:
            self._chan.close()
            self._chan = self._handle = None
            self.pid = None
        pending = [rid for rid, (_f, g) in self._pending.items() if g == gen]
        for rid in pending:
            fut, _g = self._pending.pop(rid)
            fut.set_exception(err)
        sessions = [p for p in self._sessions.values() if p._gen == gen]   # noqa: SLF001
        for p in sessions:
            self._sessions.pop(p._key, None)                                # noqa: SLF001
        media, talks = list(self._media.values()), list(self._talks.values())
        # Callbacks without the lock: they may call back into the client.
        threading.Thread(target=self._notify_gone, args=(sessions, media, talks, err), daemon=True).start()

    @staticmethod
    def _notify_gone(sessions: list[SessionProxy], media: list[MediaProxy], talks: list[TalkProxy],
                     err: WorkerDied) -> None:
        for p in sessions:
            p._lost(err)                                                    # noqa: SLF001
        for m in media:
            m._set_running(False)                                           # noqa: SLF001
        for t in talks:
            t._end_all(err)                                                 # noqa: SLF001

    def _dispatch(self, msg: tuple) -> None:
        kind = msg[0]
        if kind in ("ret", "err"):
            entry = self._pending.pop(msg[1], None)
            if entry is not None:
                fut = entry[0]
                fut.set_result(msg[2]) if kind == "ret" else fut.set_exception(msg[2])
        elif kind == "push":
            if (p := self._sessions.get(msg[1])) is not None:
                p._push(msg[2])                                             # noqa: SLF001
        elif kind == "lost":
            if (p := self._sessions.get(msg[1])) is not None:
                p._lost(msg[2])                                             # noqa: SLF001
        elif kind == "running":
            if (m := self._media.get(msg[1])) is not None:
                m._set_running(msg[2])                                      # noqa: SLF001
        elif kind == "talk":
            if (t := self._talks.get(msg[1])) is not None:
                t._ended(msg[2], msg[3])                                    # noqa: SLF001
        elif kind == "log":
            _level, name, text = msg[1:]
            logging.getLogger(name).log(_level, "%s", text)

    # ── Proxies ──────────────────────────────────────────────────────────────
    def session(self, uid: str, authkey: str, password: str, account: str = "admin", *,
                on_push: Callable[[Any], None] | None = None,
                on_lost: Callable[[VtechError], None] | None = None,
                media: bool = False, **options: Any) -> SessionProxy:
        """A session to one camera, opened in the worker on `connect()`. `media=True` makes it the one
        this camera's live view uses. `options` are session keyword arguments (name, keepalive_s, ...)."""
        return SessionProxy(self, uid, authkey, password, account, on_push, on_lost, media, options)

    def media(self, uid: str, on_running: Callable[[bool], None] | None = None) -> MediaProxy:
        m = MediaProxy(self, uid, on_running)
        self._media[uid] = m
        return m

    def talk(self, uid: str) -> TalkProxy:
        t = TalkProxy(self, uid)
        self._talks[uid] = t
        return t

    def url(self, uid: str) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/{uid}.ts"

    def module_info(self) -> dict[str, Any] | None:
        if not self.running:
            return None
        try:
            return self.request("module_info", timeout=5)
        except VtechError:
            return None

    def stats(self) -> dict[str, Any]:
        return {"running": self.running, "pid": self.pid, "generation": self.gen, "crashes": self.crashes,
                "last_exit": self.last_exit}


class SessionProxy:
    """Stands in for a session: the same blocking methods, run in the worker."""

    def __init__(self, client: WorkerClient, uid: str, authkey: str, password: str, account: str,
                 on_push: Callable[[Any], None] | None, on_lost: Callable[[VtechError], None] | None,
                 media: bool, options: dict[str, Any]):
        self._client = client
        self._args = (uid, authkey, password, account, options, media)
        self.uid = uid
        self.name: str = options.get("name", "camera")
        self.on_push, self.on_lost = on_push, on_lost
        self.devinfo: Any = None
        self._key: int | None = None
        self._gen: int | None = None
        self._lock = threading.Lock()
        self._lost_fired = self._closed = False

    def connect(self) -> Any:
        client = self._client
        with self._lock:
            if self._key is not None or self._closed:
                raise SessionGone("a session can only connect once", call="connect")
            gen = client._ensure_running()                                  # noqa: SLF001
            self._key, self._gen = next(client._ids), gen                   # noqa: SLF001
            client._sessions[self._key] = self                              # noqa: SLF001
        client.request("open", self._key, *self._args, gen=gen)
        self.devinfo = self._call("connect")
        return self.devinfo

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            key = self._key
        if key is None:
            return
        self._client._sessions.pop(key, None)                               # noqa: SLF001
        try:
            self._client.request("close", key, gen=self._gen)
        except VtechError:
            pass                                                            # the worker is gone anyway

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._key is None or self._closed:
            raise SessionGone(f"{self.name}: not connected", call=method)
        return self._client.request("call", self._key, method, args, kwargs, gen=self._gen)

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_"):
            raise AttributeError(method)
        return lambda *args, **kwargs: self._call(method, *args, **kwargs)

    def _push(self, push: Any) -> None:
        if self.on_push is not None:
            self.on_push(push)

    def _lost(self, err: VtechError) -> None:
        with self._lock:
            if self._lost_fired or self._closed:
                return
            self._lost_fired = True
        if self.on_lost is not None:
            self.on_lost(err)


class MediaProxy:
    """Stands in for one camera's `LiveStream`. The stream itself is served by the worker over HTTP."""

    def __init__(self, client: WorkerClient, uid: str, on_running: Callable[[bool], None] | None):
        self._client, self.uid, self._on_running = client, uid, on_running
        self.running = False

    def _set_running(self, running: bool) -> None:
        if running != self.running:
            self.running = running
            if self._on_running is not None:
                self._on_running(running)

    def _request(self, method: str, *args: Any) -> Any:
        if not self._client.running:
            raise SessionGone(f"{self.uid[-4:]}: camera not connected", call="live view")
        return self._client.request("media", self.uid, method, args)

    def snapshot(self, width: int | None = None, height: int | None = None) -> bytes:
        return self._request("snapshot", width, height)

    def stats(self) -> dict[str, Any]:
        try:
            return self._request("stats")
        except VtechError as err:
            return {"running": False, "error": str(err)}

    def close(self) -> None:
        self._client._media.pop(self.uid, None)                             # noqa: SLF001
        try:
            self._request("close")
        except VtechError:
            pass


class TalkProxy:
    """Stands in for one camera's `TalkBack`. The media is fetched and decoded in the worker."""

    def __init__(self, client: WorkerClient, uid: str):
        self._client, self.uid = client, uid
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._on_end: dict[int, Callable[[VtechError | None], None]] = {}

    def _request(self, method: str, *args: Any) -> Any:
        if not self._client.running:
            raise SessionGone(f"{self.uid[-4:]}: camera not connected", call="talk-back")
        return self._client.request("talk", self.uid, method, args)

    def start(self, url: str, on_end: Callable[[VtechError | None], None]) -> None:
        """Play `url` through the camera's speaker; returns once the camera receives it. `on_end(err)`
        runs on the client's reader thread when it ends: None when it played out or was stopped."""
        tid = next(self._ids)
        with self._lock:
            self._on_end[tid] = on_end
        try:
            self._request("start", tid, url)
        except BaseException:
            with self._lock:
                self._on_end.pop(tid, None)
            raise

    def stop(self) -> None:
        try:
            self._request("stop")
        except VtechError:
            pass                                                            # nothing is playing without a worker

    def stats(self) -> dict[str, Any]:
        try:
            return self._request("stats")
        except VtechError as err:
            return {"running": False, "error": str(err)}

    def close(self) -> None:
        self._client._talks.pop(self.uid, None)                             # noqa: SLF001
        self.stop()
        self._end_all(None)

    def _ended(self, tid: int, err: VtechError | None) -> None:
        with self._lock:
            on_end = self._on_end.pop(tid, None)
        if on_end is not None:
            on_end(err)

    def _end_all(self, err: VtechError | None) -> None:
        with self._lock:
            ends, self._on_end = list(self._on_end.values()), {}
        for on_end in ends:
            on_end(err)
