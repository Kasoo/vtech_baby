"""Message framing between Home Assistant and the SDK worker process (`worker.py`, `worker_client.py`).

Each message is a 4-byte big-endian length followed by a pickle. Both ends are this package's own code,
so pickle is safe here, and `vtech_lan` classes (exceptions, `ops` dataclasses, `Push`) cross intact: the
worker imports the package under the same dotted name as Home Assistant does (`_worker_main.py`).

Requests carry an id; the worker answers each with ("ret", id, value) or ("err", id, exception).
Everything else the worker sends is unsolicited: pushes, lost sessions, live-view state and log records.
"""
from __future__ import annotations

import pickle
import socket
import struct
import threading
from typing import Any

MAX_MESSAGE = 16 << 20            # a snapshot JPEG is ~100 KB; anything near this is a bug
_HDR = struct.Struct(">I")


class Channel:
    """One end of the socket pair. `send` is thread-safe; `recv` belongs to a single reader thread."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._wlock = threading.Lock()

    def send(self, msg: Any) -> None:
        try:
            data = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as err:                                # noqa: BLE001 - e.g. an unpicklable exception
            if msg and msg[0] == "err":
                data = pickle.dumps(("err", msg[1], RuntimeError(f"{type(msg[2]).__name__}: {msg[2]}")))
            else:
                raise TypeError(f"cannot send {msg[0]!r}: {err}") from err
        if len(data) > MAX_MESSAGE:
            raise ValueError(f"message too large ({len(data)} bytes)")
        with self._wlock:
            self.sock.sendall(_HDR.pack(len(data)) + data)

    def recv(self) -> Any:
        """The next message. Raises EOFError when the other end has gone (closed or died)."""
        (n,) = _HDR.unpack(self._read(_HDR.size))
        if n > MAX_MESSAGE:
            raise EOFError(f"bad message length {n}")
        return pickle.loads(self._read(n))

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError as err:
                raise EOFError(str(err)) from err
            if not chunk:
                raise EOFError("connection closed")
            buf += chunk
        return bytes(buf)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
