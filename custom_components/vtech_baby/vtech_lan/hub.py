"""The process-wide hub: the loaded `_vtech_link` module, each camera's live view and talk-back, and the
loopback HTTP server that serves the live views.

One `Hub` per Home Assistant process serves every camera. It hands out `LinkSession`s; the one a camera's
manager opens with `media=True` is the session its live view and talk-back run on, from its connect
until it is lost or closed.

The live view is served as MPEG-TS over HTTP on 127.0.0.1 (`GET /<token>/<uid>.ts`), for HA's go2rtc
(WebRTC) and stream component (HLS). The server runs on its own threads, off the event loop, and lives
as long as the hub, so its URL survives entry reloads.
"""
from __future__ import annotations

import http.server
import logging
import secrets
import threading
from collections import deque
from typing import Any, Callable

from . import link
from .errors import SessionGone, VtechError
from .media import LiveStream
from .session import LinkSession
from .talkback import TalkBack

_LOG = logging.getLogger(__name__)

QUEUE_CHUNKS = 600            # ~15–30 s of stream; a live-view client further behind is cut off


class Hub:
    def __init__(self, link_loader: Callable[[], Any] = link.load,
                 link_is_current: Callable[[Any], bool] = link.is_current):
        self._link_loader = link_loader
        self._link_is_current = link_is_current
        self.link: Any = None                        # the _vtech_link module, once loaded
        self.current: dict[str, LinkSession] = {}    # uid -> the session the live view uses
        self.streams: dict[str, LiveStream] = {}     # uid -> live view
        self.talks: dict[str, TalkBack] = {}         # uid -> talk-back
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._http = _MediaServer(self)
        self.port: int = self._http.port
        self.closed = False

    # ── The module ───────────────────────────────────────────────────────────
    def load(self) -> bool:
        """Load the module once. Returns False if a different version has been installed since (it needs
        a Home Assistant restart; the loaded one keeps running). Blocking."""
        with self._lock:
            if self.closed:
                raise SessionGone("the hub is shut down", call="load")
            if self.link is None:
                self.link = self._link_loader()
            return self._link_is_current(self.link)

    def module_info(self) -> dict[str, Any] | None:
        if self.link is None:
            return None
        return {"link": link.info(self.link)}

    # ── Sessions ─────────────────────────────────────────────────────────────
    def session(self, uid: str, authkey: str, password: str, account: str = "admin", *,
                on_push: Callable[[Any], None] | None = None,
                on_lost: Callable[[VtechError], None] | None = None,
                media: bool = False, **options: Any) -> LinkSession:
        """A session to one camera; `connect()` it from an executor thread. `media=True` makes it the one
        this camera's live view and talk-back use once it connects. `options` are `LinkSession` keyword
        arguments (name, keepalive_s, ...)."""
        if self.link is None:
            raise SessionGone("vtech-link isn't loaded", call="open")
        return _HubSession(self, media, self.link, uid, authkey, password, account,
                           on_push=on_push, on_lost=on_lost, **options)

    def _connected(self, s: _HubSession) -> None:
        with self._lock:
            if s.connected:
                self.current[s.uid] = s

    def _gone(self, s: _HubSession, closing: bool) -> None:
        """`s` is lost or closing: the live view and talk-back stop using it. A talk-back on a healthy
        session is stopped before it closes, since its channel is on the session; on a lost one it ends
        by itself, with the error."""
        with self._lock:
            if self.current.get(s.uid) is s:
                del self.current[s.uid]
            talk = self.talks.get(s.uid)
        if closing and s.connected and talk is not None and talk.session is s:
            talk.stop()

    # ── Live view and talk-back ──────────────────────────────────────────────
    def media(self, uid: str, on_running: Callable[[bool], None] | None = None) -> LiveStream:
        """This camera's live view. `on_running(bool)` runs on the media thread."""
        stream = LiveStream(
            lambda: self.current.get(uid), name=uid[-4:], on_running=on_running,
            audio_held=lambda: (t := self.talks.get(uid)) is not None and t.hold_audio_start())
        with self._lock:
            old, self.streams[uid] = self.streams.get(uid), stream
        if old is not None:
            old.close()
        return stream

    def talk(self, uid: str) -> TalkBack:
        """This camera's talk-back. Its `on_end` callbacks run on its own thread."""
        talk = TalkBack(lambda: self.current.get(uid), name=uid[-4:],
                        audio_wanted=lambda: (st := self.streams.get(uid)) is not None and st.running)
        with self._lock:
            old, self.talks[uid] = self.talks.get(uid), talk
        if old is not None:
            old.stop()
        return talk

    def release(self, uid: str) -> None:
        """Entry unload: stop this camera's talk-back and live view, before its session closes. Blocking."""
        with self._lock:
            talk, stream = self.talks.pop(uid, None), self.streams.pop(uid, None)
        if talk is not None:
            talk.stop()
        if stream is not None:
            stream.close()

    def url(self, uid: str) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/{uid}.ts"

    def stop(self) -> None:
        """Shut down for good (Home Assistant stopping). The entries close their own sessions."""
        with self._lock:
            self.closed = True
            uids = set(self.talks) | set(self.streams)
        for uid in uids:
            self.release(uid)
        self._http.shutdown()


class _HubSession(LinkSession):
    """A `LinkSession` that tells the hub when it connects and when it goes."""

    def __init__(self, hub: Hub, media: bool, module: Any, uid: str, *args: Any,
                 on_lost: Callable[[VtechError], None] | None = None, **kw: Any):
        super().__init__(module, uid, *args, on_lost=self._on_lost if media else on_lost, **kw)
        self._hub, self._media, self._user_on_lost = hub, media, on_lost
        self.uid = uid

    def connect(self) -> Any:
        devinfo = super().connect()
        if self._media:
            self._hub._connected(self)                                  # noqa: SLF001
        return devinfo

    def close(self) -> None:
        if self._media:
            self._hub._gone(self, closing=True)                         # noqa: SLF001
        super().close()

    def _on_lost(self, err: VtechError) -> None:
        self._hub._gone(self, closing=False)                            # noqa: SLF001
        if self._user_on_lost is not None:
            self._user_on_lost(err)


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
    stream ends."""

    def __init__(self, hub: Hub):
        class Handler(_Handler):
            pass
        Handler.hub = hub
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="vtech-http", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Handler(http.server.BaseHTTPRequestHandler):
    hub: Hub

    def do_GET(self) -> None:                                   # noqa: N802 - http.server's name
        parts = self.path.split("/")
        if (len(parts) != 3 or not parts[2].endswith(".ts")
                or not secrets.compare_digest(parts[1].encode(), self.hub.token.encode())):
            self.send_error(404)
            return
        stream = self.hub.streams.get(parts[2][:-3])
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
