# 75 — The Python API and the integration changes

Serves R10–R14, R17–R18, R22, R25–R30, R32, R37. Reference: `vtech_lan/sdk.py` (today's binding),
`session.py`, `media.py`, `talkback.py`, `worker.py`, `manager.py`, `native.py`.

## The extension module `_vtech_link`

Built by `vtech-link-py` as `_vtech_link.abi3.so` (`80-build.md`). Every method that can wait takes a
timeout in seconds (float) and releases the GIL while it waits. No method calls back into Python.

```python
# module level
__version__: str                        # crate version + git revision
SDK_VERSION: int                        # what we advertise on the wire (R40)

def connect(uid: str, authkey: str, password: str, account: str = "admin", *,
            connect_timeout: float = 15.0, av_timeout: float = 30.0, search_port: int = 32762,
            peer_hint: str | None = None,          # last known IPv4 address, unicast discovery too
            interface: str | None = None,          # restrict discovery to one interface
            trace: str | None = None,              # packet-trace file (70-runtime.md)
            ) -> Link: ...                         # blocks until up; raises LinkError

def lan_only_stats() -> dict[str, int]             # {"refused_sends": n, "refused_lookups": 0}
def next_logs(timeout: float) -> list[tuple[int, str, str]]   # (level, target, message); [] on timeout
def links() -> list[dict]                          # info() of every live link, for diagnostics

class Link:
    def send(self, type: int, payload: bytes = b"", timeout: float = 5.0) -> None
        # returns when the camera has acknowledged the frame (R10). Concurrent callers queue (R11).
    def request(self, type: int, payload: bytes, reply: int, timeout: float = 5.0) -> bytes   # R12
    def exchange(self, type: int, payload: bytes, reply: int, timeout: float = 15.0) -> Exchange   # R13
    def next_push(self, timeout: float) -> tuple[int, bytes] | None    # None on timeout (R14)
    def media(self) -> MediaReceiver                                   # at most one open per link
    def talkback(self) -> TalkBack                                     # at most one open per link
    def info(self) -> dict      # peer, ports, versions, dtls_suite, mode="lan", up_since, close_reason (R29)
    def stats(self) -> dict     # transport counters, RTT, ack-latency histogram (R31)
    def close(self) -> None     # idempotent; safe from any thread (R8)
    @property
    def closed(self) -> LinkError | None     # why the link ended, once it has

class Exchange:
    def next(self, timeout: float) -> bytes | None    # the next reply page; None on timeout
    def close(self) -> None                           # frees the reply opcode for the next request

class MediaReceiver:
    def start(self, timeout: float = 5.0) -> None     # live-view key exchange if not done yet (R17)
    def reset(self, timeout: float = 2.0) -> None     # drop queued media, reset numbering (R19)
    def next(self, timeout: float) -> Frame | None    # decrypted, in order; None on timeout (R18, R22)
    def stats(self) -> dict                           # the receive counters (R23)
    def close(self) -> None                           # stop queueing frames; drops what is queued

class Frame:                                          # immutable
    kind: str            # "video" | "audio"
    key: bool            # media kind 5/6
    number: int          # shared frame number
    arrived: float       # time.monotonic() seconds when it completed
    lost_before: int     # frames lost in this stream just before this one
    data: bytes          # Annex-B access unit, or A-law

class TalkBack:
    channel: int                                      # announce with SPEAKERSTART (0x350)
    def accept(self, timeout: float = 10.0) -> None   # the camera has logged in (R25)
    def send(self, alaw: bytes, timeout: float | None = None) -> None   # 160 bytes; blocks while full (R26, R28)
    def close(self, drain: bool = True, timeout: float = 2.0) -> None   # R27
    def stats(self) -> dict

class LinkError(Exception):
    kind: str            # "unreachable" | "iotc_rejected" | "av_auth_failed" | "closed" | "timeout" |
                         # "protocol" | "capacity" | "invalid_argument" | "io" | "internal"
    detail: str          # e.g. "auth_key", "remote_timeout", "dtls_alert:51"
    code: int | None     # the equivalent legacy SDK code, for logs (-68, -20009, -23, ...)
```

**Clock**: `Frame.arrived` uses the same `CLOCK_MONOTONIC` as Python's `time.monotonic()` (on Linux
both read it), so `media.py` can use it directly for PTS. Today PTS is taken at the moment Python reads
the frame (`media.py` `pts()`). Using the arrival time removes the read-loop jitter.

**Frame data** is returned as `bytes` (one copy out of Rust). A zero-copy buffer-protocol object would
save about 1 MB/s of copying at 1080p. That isn't worth the complexity.

## Status

**Built 2026-10-22** (`py/src/`), with `py/tests/test_module.py`: 13 end-to-end tests that load the
module with `ExtensionFileLoader` and drive it against the simulated camera, run as a separate process
(`sim/src/bin/sim-camera.rs`, controlled over stdin). They cover each error kind the simulator can
produce (`unreachable`, `iotc_rejected/auth_key`, `av_auth_failed/dtls_alert:51`, `closed/remote_close`,
`closed/local`, `invalid_argument`), requests, exchanges, pushes, media frames, talk-back, logs, and a
second Python thread running while a call waits (the GIL is released). Run them with `py/tests/run.sh`.
They use stdlib `unittest`, as the host has no pytest.

Differences from the API above:
- `connect` has no `trace` argument yet; the packet trace isn't built (`70-runtime.md`).
- `Link.talkback(timeout=10.0)` takes a timeout, because opening the channel can run the key exchange.
- `TalkBack.send(alaw, timeout=None)`: `None` waits up to a day; every wait is capped at a day.
- Extra module functions: `set_log_level(level)` (a Python `logging` level; the default is INFO, so
  DEBUG records cost nothing until asked for), `logs_dropped()`, and `_panic()` for the import check.
- `LinkError` has `kind`, `detail` and `code` as in the table below. A DTLS handshake that goes silent
  (the real camera's answer to a wrong password) has the detail `dtls_silent`. A refused login has
  `login_rejected:N`.
- `Link.info()` adds `local_port`, `proto_version`, `iotc_version` and `partial_encryption`.
  `close_reason` is the error's detail (`"remote_close"`), or its kind when there is no detail.
- `Link.stats()` returns `iotc`, `control` (with `ack_latency` counts per `ack_latency_buckets_ms`,
  plus one bucket above), `media` (the same as `MediaReceiver.stats()`), `talk`, `rtt_ms`, `resets`
  and the datagram counters.
- `TalkBack.stats()` still returns the last channel's counters after it closes (a change in the
  reactor). It returns `None` only before a channel has opened.
- At most one `MediaReceiver` per link is not enforced: a second one competes for the same frames.
- `__version__` is the crate version, plus `+<12-hex git revision>` when built by `release.sh`.
- `Frame.arrived` is computed as `time.monotonic()` minus the frame's age when `next` returns it, so the
  two clocks only need to tick at the same rate.

## Mapping errors onto the existing exceptions

`vtech_lan/errors.py` keeps its classes, and so `manager.py` and the entities don't change. A new
`errors.from_link(e: LinkError) -> VtechError`:

| `kind` / `detail` | Exception | Legacy `code` |
|---|---|---|
| `unreachable` | `Unreachable` | `-13` |
| `iotc_rejected` / `auth_key` | `AuthFailed` | `-68` |
| `iotc_rejected` / `max_sessions` | `CapacityExceeded` | `-48` |
| `iotc_rejected` / `no_key_auth`, `port`, `other:N` | `VtechError` | `-69`, `-67`, None |
| `av_auth_failed` | `AuthFailed` | `-20009` |
| `closed` / `remote_close` | `SessionGone` | `-22` |
| `closed` / `remote_timeout` | `SessionGone` | `-23` |
| `closed` / `local`, `dtls_alert:N`, `thread_failed` | `SessionGone` | None |
| `timeout` | `RequestTimeout` | `-20011` |
| `protocol` | `ProtocolError` | None |
| `capacity` | `CapacityExceeded` | None |
| `invalid_argument`, `io`, `internal` | `VtechError` | None |

`NotLanDirect`, `SdkMismatch`, `Unlicensed`, `WorkerDied` and the SDK code constants become dead once
the migration ends, and are removed then (`90-migration.md`).

## Changes in `vtech_lan`

| Module | Change |
|---|---|
| `link.py` (new) | `load(libdir)`: imports `_vtech_link` from `libdir` with `importlib.machinery.ExtensionFileLoader`, once per process, and starts the log-forwarding thread (`70-runtime.md`). Rejects a second load from a different directory (as `sdk.load` does). |
| `session.py` | Split. `SessionBase` keeps the public surface and everything above transport: the typed helpers, the read-modify-write locks, push decoding through `ops.PUSH_DECODERS`, the keepalive thread, and `on_push`/`on_lost`. It is the same API, so the worker's proxy and the manager don't notice. `SdkSession` holds today's ctypes transport (`_send_raw`, `_transact`, `_read_loop`) for the A/B period. `LinkSession` wraps a `Link`: `send` → `link.send`; `request` → `link.request(op.code, op.encode(*args), op.reply)`, then the op's ack check and decoder; `list_events` → `link.exchange` until `dec_event_page(...).end`; a reader thread loops on `link.next_push(1.0)` and on `LinkError` calls `_lose(from_link(e))`. `_send_data_delay` (`0x0ff`) is dropped (R15). `live_view_master()` is replaced by `media()`/`talkback()`, which return the core objects. `connection_mode()` returns `"lan"`. |
| `media.py` | `_stream` becomes: `rx = session.media(); rx.start(); rx.reset()`; send `AUDIO_START` (unless held), `VIDEO_FULLRATE` and `START`; then loop on `rx.next(0.2)`. Video frames go to the keyframe cache and `mux.video(frame.data, pts(frame.arrived), key)`; audio goes to `mux.audio`. The `bad` count is `sum(lost_before)` plus the decrypt counters from `rx.stats()`. `stats["receive"]` comes from `rx.stats()`. The polling sleep (`IDLE_SLEEP_S`), `decrypt_frame` and the `FrameReader` go. Teardown: `STOP`, `AUDIO_STOP`, `rx.close()`. |
| `talkback.py` | `_run`: `fmt` check and `AUDIO_STOP` as now; `tb = session.talkback()`; `SPEAKER_START(tb.channel)`; `tb.accept(SERV_TIMEOUT_S)`; `ready.set()`; then `for body in frames: if stop.is_set(): break; tb.send(body, timeout=1.0)`. Pacing, `encrypt_frame`, `frame_info` and the `-20006` reset go. `_teardown`: `tb.close(drain=not stopped)`, `SPEAKER_STOP`, `AUDIO_START` if wanted. |
| `crypto.py` | Removed. Its known-answer vectors move to the Rust tests (`85-testing.md`). |
| `sdk.py`, `ipc.py`, `worker.py`, `worker_client.py`, `_worker_main.py` | Kept for the A/B phase, then removed (`90-migration.md`). The worker's `_MediaServer` moves into the HA process as `vtech_lan/media_server.py`. |
| `errors.py` | Add `from_link`. Later, drop the SDK codes. |
| `ops.py`, `avmux.py`, `audio.py`, `provision.py` | Unchanged. |

## Changes in the integration

| File | Change |
|---|---|
| `native.py` | Replaced by `link_loader.py`: `bundled_module() -> str | None` finds `native/<arch>-<libc>/_vtech_link.abi3.so` (arch and libc detection as today, `native.py` `host_libc`). No set → `UnsupportedHost` at setup, with a message naming the supported platforms. The fingerprint is the module's sha256 and its version. |
| `config_flow.py` | The SDK upload step goes. Setup is just the pairing QR (or typed credentials). |
| `manager.py` | During A/B: `get_client()` passes the chosen backend to the worker. After the worker is retired, `get_client()` returns an in-process `LocalClient` with `WorkerClient`'s methods (`open_session`, `media`, `talk`, `sdk_info`). It starts the media HTTP server on the socket HA already binds, so `manager.py`'s callers don't change. The `worker_crashed` Repairs issue goes. |
| `diagnostics.py` | `sdk` → `link`: the module version, `lan_only_stats()`, and each link's `info()` and `stats()`. |
| `const.py` | Drop `LIB_SUBDIR` and the SDK upload constants. |
| `tooling/`, `native/<arch>/libtk.so`, `native/<arch>-<libc>/lib*APIs.so`, `sync_vendored.sh`, `sync_sdk.sh` | Removed once the Rust module ships. `sync_link.sh` replaces `sync_sdk.sh` (`80-build.md`). |

## Offline tests

`tests/fake_sdk.py` gets a sibling, `tests/fake_link.py`: a pure-Python `_vtech_link` look-alike with
scripted replies, pushes, frames and talk-back, so the 234 integration tests run without the native
module. The tests that drive `Session` against the fake SDK move to `SessionBase` through `LinkSession`
with the fake link. Tests of ctypes, the worker, the patcher and the companion are deleted with that
code.
