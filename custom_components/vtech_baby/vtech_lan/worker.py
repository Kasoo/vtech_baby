"""The worker: the process that loads the native module (`_vtech_link`) and runs every camera session
and live view.

Running it in its own process means a crash in it (a segfault, an abort) ends this process only: Home
Assistant sees the socket close, and reconnects with a fresh worker (`worker_client.py`). Started by
`_worker_main.py`; this module holds the worker's logic, so it can also run on a thread (tests) with any
module object.

The worker:
- answers requests from Home Assistant (`ipc.py`), each on a pool thread since module calls block;
- owns the `Session`s and forwards their pushes and losses;
- serves each camera's live view as MPEG-TS over HTTP on a loopback socket Home Assistant bound and
  passed in, so the URL stays the same across worker restarts and decrypted frames never cross the IPC;
- runs each camera's talk-back, fetching and decoding the media here, so audio never crosses the IPC.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import faulthandler
import http.server
import importlib
import logging
import os
import secrets
import socket
import sys
import threading
from collections import deque
from typing import Any, Callable

from . import link
from .errors import ModuleMismatch, SessionGone, VtechError
from .ipc import Channel
from .media import LiveStream
from .session import LinkSession, SessionBase
from .talkback import TalkBack

_LOG = logging.getLogger(__name__)

QUEUE_CHUNKS = 600            # ~15–30 s of stream; a live-view client further behind is cut off
POOL_THREADS = 32             # concurrent blocking requests (connects take up to ~15 s)


class Worker:
    def __init__(self, chan: Channel, listen: socket.socket | None = None, forward_logs: bool = False,
                 link_loader: Callable[[str], Any] = link.load):
        self.chan = chan
        self._link_loader = link_loader
        self._listen = listen
        self._forward_logs = forward_logs
        self.link: Any = None                        # the _vtech_link module, once loaded
        self._link_path: str | None = None
        self.sessions: dict[int, SessionBase] = {}
        self.media_sessions: set[int] = set()        # sessions that feed their camera's live view
        self.current: dict[str, SessionBase] = {}    # uid -> the session the live view uses
        self.streams: dict[str, LiveStream] = {}     # uid -> live view
        self.talks: dict[str, TalkBack] = {}         # uid -> talk-back
        self.token: str | None = None
        self.http: _MediaServer | None = None
        self._lock = threading.Lock()

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        pool = concurrent.futures.ThreadPoolExecutor(POOL_THREADS, thread_name_prefix="vtech-worker")
        try:
            while True:
                try:
                    msg = self.chan.recv()
                except EOFError:
                    return                                  # Home Assistant went away
                if msg[0] == "shutdown":
                    self.shutdown()
                    self._reply(msg[1], value=None)
                    return
                pool.submit(self._handle, msg)
        finally:
            self.shutdown()
            pool.shutdown(wait=False, cancel_futures=True)

    def _handle(self, msg: tuple) -> None:
        kind, rid, *args = msg
        try:
            value = getattr(self, f"_do_{kind}")(*args)
        except BaseException as err:                        # noqa: BLE001 - every failure goes back
            self._reply(rid, err=err)
        else:
            self._reply(rid, value=value)

    def _reply(self, rid: int, *, value: Any = None, err: BaseException | None = None) -> None:
        try:
            self.chan.send(("err", rid, err) if err is not None else ("ret", rid, value))
        except OSError:
            pass                                            # Home Assistant went away

    def _notify(self, *msg: Any) -> None:
        try:
            self.chan.send(msg)
        except OSError:
            pass

    def shutdown(self) -> None:
        with self._lock:
            talks, self.talks = list(self.talks.values()), {}
            streams, self.streams = list(self.streams.values()), {}
            sessions, self.sessions = list(self.sessions.values()), {}
            self.current.clear()
            http_server, self.http = self.http, None
        for t in talks:
            t.stop()                                        # before the sessions: they use their channels
        for s in streams:
            s.close()
        for s in sessions:
            s.close()
        if http_server is not None:
            http_server.shutdown()

    # ── Requests ─────────────────────────────────────────────────────────────
    def _do_init(self, token: str, log_level: int) -> int:
        self.token = token
        if self._forward_logs:
            root = logging.getLogger()
            root.handlers[:] = [_ForwardHandler(self)]
            root.setLevel(log_level)
        if self._listen is not None and self.http is None:
            self.http = _MediaServer(self, self._listen)
        return os.getpid()

    def _do_load_link(self, path: str) -> dict[str, Any]:
        with self._lock:
            if self.link is not None and path != self._link_path:
                raise ModuleMismatch(f"worker already has vtech-link from {self._link_path}")
            if self.link is None:
                self.link, self._link_path = self._link_loader(path), path
        return self._do_module_info()

    def _do_module_info(self) -> dict[str, Any]:
        return {"pid": os.getpid(), "link": link.info(self.link) if self.link is not None else None,
                "link_path": self._link_path}

    def _do_open(self, key: int, uid: str, authkey: str, password: str, account: str,
                 options: dict[str, Any], media: bool) -> None:
        if self.link is None:
            raise SessionGone("vtech-link isn't loaded in the worker", call="open")
        s: SessionBase = LinkSession(self.link, uid, authkey, password, account,
                                     on_push=lambda p: self._notify("push", key, p),
                                     on_lost=lambda e: self._on_lost(key, s, e), **options)
        with self._lock:
            self.sessions[key] = s
            if media:
                self.media_sessions.add(key)
                if uid not in self.streams:
                    self.streams[uid] = LiveStream(
                        lambda: self.current.get(uid), name=uid[-4:],
                        on_running=lambda r: self._notify("running", uid, r),
                        audio_held=lambda: (t := self.talks.get(uid)) is not None and t.hold_audio_start())

    def _do_call(self, key: int, method: str, args: tuple, kwargs: dict[str, Any]) -> Any:
        s = self.sessions.get(key)
        if s is None:
            raise SessionGone("session closed", call=method)
        if method.startswith("_") or not callable(getattr(type(s), method, None)):
            raise AttributeError(f"{type(s).__name__} has no method {method!r}")
        value = getattr(s, method)(*args, **kwargs)
        if method == "connect" and key in self.media_sessions:
            with self._lock:
                if self.sessions.get(key) is s:
                    self.current[s._uid] = s                # noqa: SLF001
        return value

    def _do_close(self, key: int) -> None:
        with self._lock:
            s = self.sessions.pop(key, None)
            self.media_sessions.discard(key)
            if s is not None and self.current.get(s._uid) is s:     # noqa: SLF001
                del self.current[s._uid]                            # noqa: SLF001
        if s is None:
            return
        talk = self.talks.get(s._uid)                               # noqa: SLF001
        if talk is not None and talk.session is s:
            talk.stop()                                             # its channel is on this session
        s.close()

    def _on_lost(self, key: int, s: SessionBase, err: VtechError) -> None:
        with self._lock:
            if self.current.get(s._uid) is s:                       # noqa: SLF001
                del self.current[s._uid]                            # noqa: SLF001
        self._notify("lost", key, err)

    def _do_media(self, uid: str, method: str, args: tuple) -> Any:
        stream = self.streams.get(uid)
        if stream is None:
            raise SessionGone(f"{uid[-4:]}: no live view", call="live view")
        if method == "snapshot":
            return stream.snapshot(*args)
        if method == "stats":
            return {**stream.stats, "subscribers": stream.subscribers}
        if method == "close":
            with self._lock:
                self.streams.pop(uid, None)
            stream.close()
            return None
        raise AttributeError(f"no live-view method {method!r}")

    def _do_talk(self, uid: str, method: str, args: tuple) -> Any:
        """Talk-back: "start" (talk id, url) returns once the camera receives the audio, and its end
        is reported as a ("talk", uid, talk id, error or None) message; "stop" ends it."""
        if method == "start":
            tid, url = args
            with self._lock:
                talk = self.talks.get(uid)
                if talk is None:
                    talk = self.talks[uid] = TalkBack(
                        lambda: self.current.get(uid), name=uid[-4:],
                        audio_wanted=lambda: (st := self.streams.get(uid)) is not None and st.running)
            talk.start(url, on_end=lambda err: self._notify("talk", uid, tid, err))
            return None
        if method == "stop":
            if (talk := self.talks.get(uid)) is not None:
                talk.stop()
            return None
        if method == "stats":
            talk = self.talks.get(uid)
            return {**talk.stats, "running": talk.running} if talk else {"running": False}
        raise AttributeError(f"no talk-back method {method!r}")


# ── Live view over loopback HTTP ─────────────────────────────────────────────
class _Buffer:
    """Chunks from the media thread to one HTTP request. `None` ends the response."""

    def __init__(self, limit: int):
        self._items: deque[bytes | None] = deque()
        self._cond = threading.Condition()
        self._limit = limit
        self._ended = False

    def put(self, data: bytes | None) -> None:                  # media thread: must not block
        with self._cond:
            if self._ended:
                return
            if data is None or len(self._items) >= self._limit:
                self._ended, data = True, None
            self._items.append(data)
            self._cond.notify()

    def get(self) -> bytes | None:
        with self._cond:
            while not self._items:
                self._cond.wait()
            return self._items.popleft()


class _MediaServer:
    """`GET /<token>/<uid>.ts` streams that camera's live view as MPEG-TS until the client leaves or the
    stream ends. HA's go2rtc (WebRTC) and stream component (HLS) are the clients."""

    def __init__(self, worker: Worker, listen: socket.socket):
        class Handler(_Handler):
            pass
        Handler.worker = worker
        self._server = http.server.ThreadingHTTPServer(listen.getsockname(), Handler, bind_and_activate=False)
        self._server.socket.close()
        self._server.socket = listen
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="vtech-http", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()


class _Handler(http.server.BaseHTTPRequestHandler):
    worker: Worker

    def do_GET(self) -> None:                                   # noqa: N802 - http.server's name
        parts = self.path.split("/")
        token = self.worker.token or ""
        if (len(parts) != 3 or not parts[2].endswith(".ts")
                or not secrets.compare_digest(parts[1].encode(), token.encode())):
            self.send_error(404)
            return
        stream = self.worker.streams.get(parts[2][:-3])
        if stream is None:
            self.send_error(404)
            return
        buf = _Buffer(QUEUE_CHUNKS)
        try:
            sub = stream.subscribe(buf.put)
        except SessionGone as err:
            self.send_error(503, str(err))
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while (data := buf.get()) is not None:
                self.wfile.write(data)
        except OSError:
            pass                                                # the client went away
        finally:
            sub.close()

    def log_message(self, format: str, *args: Any) -> None:     # noqa: A002 - http.server's signature
        _LOG.debug("live view: " + format, *args)


class _ForwardHandler(logging.Handler):
    """Sends the worker's log records to Home Assistant, which logs them under the same logger names."""

    def __init__(self, worker: Worker):
        super().__init__()
        self._worker = worker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            self._worker.chan.send(("log", record.levelno, record.name, msg))
        except Exception:                                       # noqa: BLE001 - never fail the caller
            pass


# ── Process entry (via _worker_main.py) ──────────────────────────────────────
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="vtech_baby worker")
    ap.add_argument("--fd", type=int, required=True, help="the IPC socket")
    ap.add_argument("--listen-fd", type=int, help="the loopback live-view socket, already listening")
    ap.add_argument("--link-loader", default=None, help="module:callable taking the module path (tests)")
    ap.add_argument("--path", action="append", default=[], help="append to sys.path (tests)")
    a = ap.parse_args(argv)
    faulthandler.enable()                   # a native crash dumps the Python stacks to stderr
    sys.path.extend(a.path)
    loader = link.load
    if a.link_loader:
        mod, _, attr = a.link_loader.partition(":")
        loader = getattr(importlib.import_module(mod), attr)
    chan = Channel(socket.socket(fileno=a.fd))
    listen = socket.socket(fileno=a.listen_fd) if a.listen_fd is not None else None
    Worker(chan, listen, forward_logs=True, link_loader=loader).run()
    logging.shutdown()
    os._exit(0)                             # don't wait for the module's own threads
