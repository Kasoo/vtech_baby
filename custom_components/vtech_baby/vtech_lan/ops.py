"""The RM5754HD IOCtrl opcode table: codes, reply opcodes, encoders, decoders, enums, push decoders.

Authority: docs/vtech-lan-kit/docs/30-opcode-reference.md (plus 60-event-channel.md, 70-device-info.md).
All payloads are little-endian.

Design rules:
- Every op names its reply opcode explicitly. Never compute it as request + 1: `0x709 → 0x710` and
  `0x729 → 0x730` break that rule.
- A SET's ack is checked with a per-op predicate, because ack encodings differ per opcode.
- Pure data and pure functions: no SDK, no threads.
"""
from __future__ import annotations

import datetime as dt
import enum
import struct
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ProtocolError

UTC = dt.timezone.utc


# ── Enums ────────────────────────────────────────────────────────────────────
class NightLight(enum.IntEnum):
    """0x7a2 colour preset. Values >= 4 are a no-op at the LED."""
    OFF = 0
    YELLOW = 1
    ORANGE = 2
    RED = 3


class NightVisionMode(enum.IntEnum):
    """0x715 / 0x714 day/night mode setting (the state is the 0x785 push)."""
    AUTO = 0
    ON = 1       # forces night/IR even in a lit room
    OFF = 2


class MotionSensitivity(enum.IntEnum):
    """0x7a6 / 0x7a5 word0, and 0x734 @24."""
    OFF = 0
    LOW = 25
    MEDIUM = 50
    HIGH = 75


class SoundSensitivity(enum.IntEnum):
    """0x734 @16. Read-only from the LAN (only the parent handset changes it)."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class NotifyInterval(enum.IntEnum):
    """0x790 / 0x793 index. Also rate-limits the 0x1fff event pushes."""
    DEFAULT = 0
    MIN_1 = 1
    MIN_3 = 2
    MIN_5 = 3
    MIN_10 = 4
    MIN_30 = 5
    NEVER = 6


class Quality(enum.IntEnum):
    """0x320 / 0x323 quality byte = resolution. Persists camera-side."""
    HD_720P = 1
    FHD_1080P = 2


class LullabyTrack(enum.IntEnum):
    """0x735 track index, in the parent handset's menu order. STOP = -1."""
    STOP = -1
    PLAY_ALL = 0          # cycles all tracks; 0x734 @4 stays 0, no now-playing track
    MY_BONNIE = 1
    ITSY_BITSY_SPIDER = 2
    HOT_CROSS_BUNS = 3
    HUSH_LITTLE_BABY = 4
    BRAHMS_LULLABY = 5
    WHITE_NOISE = 6
    CRICKETS = 7
    STREAM = 8
    BIRDS = 9


LULLABY_NAMES = {
    LullabyTrack.PLAY_ALL: "Play All", LullabyTrack.MY_BONNIE: "My Bonnie",
    LullabyTrack.ITSY_BITSY_SPIDER: "Itsy Bitsy Spider", LullabyTrack.HOT_CROSS_BUNS: "Hot Cross Buns",
    LullabyTrack.HUSH_LITTLE_BABY: "Hush Little Baby", LullabyTrack.BRAHMS_LULLABY: "Brahms' Lullaby",
    LullabyTrack.WHITE_NOISE: "White Noise", LullabyTrack.CRICKETS: "Crickets",
    LullabyTrack.STREAM: "Stream", LullabyTrack.BIRDS: "Birds",
}

#: 0x734 @12 lullaby auto-off timer values, seconds (0 = non-stop).
LULLABY_TIMERS_S = (900, 1800, 3600, 0)


class EventCode(enum.IntEnum):
    """0x1fff event_code@16."""
    MOTION = 0x01
    SOUND = 0x15
    TEMP_HIGH = 0x16
    TEMP_LOW = 0x17


class TempState(enum.IntEnum):
    """0x738 int@8, against the 0x74d thresholds (evaluated even while alerts are disabled)."""
    IN_RANGE = 0
    TOO_HOT = 1
    TOO_COLD = 2


class ParamSubtype(enum.IntEnum):
    """0x7ac int@0: which setting changed."""
    NIGHT_LIGHT = 0
    LULLABY_TRACK = 1          # -1 = stopped
    TEMP_DETECT_ENABLE = 2
    STATUS_LED = 8             # the "LED disabled" flag
    SPEAKER_VOLUME = 0x14
    LULLABY_TIMER = 0x15       # seconds


# ── Decoded payloads ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DevInfo:
    model: str
    vendor: str
    firmware: str | None


@dataclass(frozen=True)
class Params:
    """0x734 (9 × int32). `p28` is opaque: echo it back unchanged in 0x731 word5."""
    temp_c: int
    lullaby_track: int
    temp_f: int
    lullaby_timer_s: int
    sound_sensitivity: int
    volume: int
    motion_sensitivity: int
    p28: int
    flag: int


@dataclass(frozen=True)
class TempAlert:
    enabled: bool
    max_c: float
    min_c: float


@dataclass(frozen=True)
class MotionSetting:
    sensitivity: int
    record: int


@dataclass(frozen=True)
class AudioFormat:
    channel: int
    codec: int                 # 0x8a = G.711 A-law
    sample_rate_idx: int | None = None
    data_bits: int | None = None
    channels: int | None = None
    serv_channel: int | None = None


@dataclass(frozen=True)
class Timezone:
    gmt_offset_min: int        # effective, includes the DST hour when DST is on
    tz_index: int


@dataclass(frozen=True)
class EventReport:
    """0x1fff live alert."""
    local_time: dt.datetime | None     # camera-local wall clock, naive
    timestamp: dt.datetime             # from unix_ts@8, UTC
    code: int                          # EventCode, kept raw so unknown codes survive


@dataclass(frozen=True)
class TempNotify:
    """0x738 temperature push."""
    temp_c: int
    temp_f: int
    state: int                         # TempState


@dataclass(frozen=True)
class ParamNotify:
    """0x7ac state-change push."""
    subtype: int                       # ParamSubtype, kept raw
    value: int


@dataclass(frozen=True)
class StoredEvent:
    """One 0x789 record."""
    time: dt.datetime | None           # UTC
    event: int                         # 4 = motion (1 is also treated as motion)
    status: int


@dataclass(frozen=True)
class EventPage:
    total: int
    index: int
    end: bool
    records: tuple[StoredEvent, ...]


# ── Encoding helpers ─────────────────────────────────────────────────────────
def _i32(*words: int) -> bytes:
    return struct.pack(f"<{len(words)}i", *words)


def _need(r: bytes, n: int, what: str) -> None:
    if len(r) < n:
        raise ProtocolError(f"{what}: reply too short ({len(r)} < {n} bytes)")


def _word(r: bytes, i: int) -> int:
    return struct.unpack_from("<i", r, 4 * i)[0]


def enc_channel(ch: int = 0) -> bytes:
    """Standard TUTK 8-byte request, word0 = channel."""
    return _i32(ch, 0)


def enc_zero4() -> bytes:
    """The 4-byte `0` argument most VTech GETs take."""
    return _i32(0)


def stimeday(t: dt.datetime) -> bytes:
    """8-byte STimeDay, UTC. wday 1 = Sunday … 7 = Saturday."""
    t = t.astimezone(UTC) if t.tzinfo else t
    return struct.pack("<HBBBBBB", t.year, t.month, t.day, t.isoweekday() % 7 + 1,
                       t.hour, t.minute, t.second)


def parse_stimeday(b: bytes, tz: dt.tzinfo | None = UTC) -> dt.datetime | None:
    """Decode an 8-byte STimeDay (the wday byte is ignored). None if the date is invalid."""
    y, mo, d, _wd, h, mi, s = struct.unpack_from("<HBBBBBB", b, 0)
    try:
        return dt.datetime(y, mo, d, h, mi, s, tzinfo=tz)
    except ValueError:
        return None


# ── Decoders ─────────────────────────────────────────────────────────────────
def dec_devinfo(r: bytes) -> DevInfo:
    _need(r, 32, "DEVINFO")
    model = r[0:16].split(b"\0", 1)[0].decode("latin1", "replace")
    vendor = r[16:32].split(b"\0", 1)[0].decode("latin1", "replace")
    fw = ".".join(str(b) for b in reversed(r[32:36])) if len(r) >= 36 else None
    return DevInfo(model, vendor, fw)


def dec_params(r: bytes) -> Params:
    _need(r, 36, "GET_PARAMS")
    return Params(*struct.unpack_from("<9i", r, 0))


def dec_temp_alert(r: bytes) -> TempAlert:
    _need(r, 12, "GET_TEMP_ALERT")
    en, mx, mn = struct.unpack_from("<3i", r, 0)
    return TempAlert(bool(en), mx / 10, mn / 10)


def dec_motion(r: bytes) -> MotionSetting:
    _need(r, 8, "GET_MOTION")
    return MotionSetting(_word(r, 0), _word(r, 1))


def dec_audio_format(r: bytes) -> AudioFormat:
    _need(r, 8, "GET_AUDIO_OUT_FORMAT")
    ch, codec = struct.unpack_from("<ii", r, 0)
    if len(r) >= 12:
        return AudioFormat(ch, codec, r[8], r[9], r[10], r[11])
    return AudioFormat(ch, codec)


def dec_timezone(r: bytes) -> Timezone:
    _need(r, 16, "GET_TIMEZONE")
    return Timezone(_word(r, 2), _word(r, 3))


def dec_event_report(r: bytes) -> EventReport:
    _need(r, 20, "EVENT_REPORT")
    (ts,) = struct.unpack_from("<I", r, 8)
    return EventReport(parse_stimeday(r[0:8], tz=None), dt.datetime.fromtimestamp(ts, UTC), _word(r, 4))


def dec_temp_notify(r: bytes) -> TempNotify:
    _need(r, 12, "TEMP_NOTIFY")
    return TempNotify(*struct.unpack_from("<3i", r, 0))


def dec_param_notify(r: bytes) -> ParamNotify:
    _need(r, 8, "PARAM_NOTIFY")
    return ParamNotify(_word(r, 0), _word(r, 1))


def dec_event_page(r: bytes) -> EventPage:
    _need(r, 12, "LIST_EVENTS page")
    total = _word(r, 1)
    index, end, count = r[8], r[9], r[10]
    recs = []
    for i in range(count):
        rec = r[12 + 12 * i: 24 + 12 * i]
        if len(rec) < 12:
            break
        recs.append(StoredEvent(parse_stimeday(rec), rec[8], rec[9]))
    return EventPage(total, index, end == 1 or count == 0, tuple(recs))


def _dec_word(i: int, what: str) -> Callable[[bytes], int]:
    def dec(r: bytes) -> int:
        _need(r, 4 * i + 4, what)
        return _word(r, i)
    return dec


def _dec_byte(i: int, what: str) -> Callable[[bytes], int]:
    def dec(r: bytes) -> int:
        _need(r, i + 1, what)
        return r[i]
    return dec


# ── Ack predicates ───────────────────────────────────────────────────────────
def _ack_byte0_is(v: int) -> Callable[[bytes], bool]:
    return lambda r: len(r) >= 1 and r[0] == v


def _ack_word_is(i: int, v: int) -> Callable[[bytes], bool]:
    return lambda r: len(r) >= 4 * i + 4 and _word(r, i) == v


# ── The op table ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Op:
    """One IOCtrl opcode.

    reply: the response opcode, or None for fire-and-forget.
    enc:   args -> request payload bytes.
    dec:   reply bytes -> value (None = return raw bytes).
    ok:    reply bytes -> ack success (None = no ack check).
    """
    code: int
    name: str
    enc: Callable[..., bytes]
    reply: int | None = None
    dec: Callable[[bytes], Any] | None = None
    ok: Callable[[bytes], bool] | None = None

    def encode(self, *args: Any) -> bytes:
        return self.enc(*args)

    def __repr__(self) -> str:
        return f"<Op {self.name} 0x{self.code:x}>"


# Standard TUTK
SND_DATA_DELAY = Op(0x0FF, "IOTYPE_INNER_SND_DATA_DELAY", enc_zero4)   # first IOCtrl on every AV channel
START = Op(0x1FF, "IOTYPE_USER_IPCAM_START", enc_channel)
STOP = Op(0x2FF, "IOTYPE_USER_IPCAM_STOP", enc_channel)
AUDIO_START = Op(0x300, "IOTYPE_USER_IPCAM_AUDIOSTART", enc_channel)
AUDIO_STOP = Op(0x301, "IOTYPE_USER_IPCAM_AUDIOSTOP", enc_channel)
SET_STREAM_CTRL = Op(0x320, "IOTYPE_USER_IPCAM_SETSTREAMCTRL_REQ",
                     lambda quality, ch=0: _i32(ch) + bytes([int(quality) & 0xFF, 0, 0, 0]),
                     reply=0x321)     # reply layout (incl. a reject while the parent unit is on) is unknown
GET_STREAM_CTRL = Op(0x322, "IOTYPE_USER_IPCAM_GETSTREAMCTRL_REQ", enc_channel, reply=0x323,
                     dec=_dec_byte(4, "GET_STREAM_CTRL"))
GET_AUDIO_OUT_FORMAT = Op(0x32A, "IOTYPE_USER_IPCAM_GETAUDIOOUTFORMAT_REQ", enc_channel, reply=0x32B,
                          dec=dec_audio_format)
DEVINFO = Op(0x330, "IOTYPE_USER_IPCAM_DEVINFO_REQ", lambda: bytes([0, 1, 0x12, 0x34]) + bytes(16),
             reply=0x331, dec=dec_devinfo)
SPEAKER_START = Op(0x350, "IOTYPE_USER_IPCAM_SPEAKERSTART", enc_channel)
SPEAKER_STOP = Op(0x351, "IOTYPE_USER_IPCAM_SPEAKERSTOP", enc_channel)
GET_TIMEZONE = Op(0x3A0, "IOTYPE_USER_IPCAM_GET_TIMEZONE_REQ", lambda: bytes(268), reply=0x3A1,
                  dec=dec_timezone)
VIDEO_FULLRATE = Op(0x719, "VIDEO_FULLRATE", lambda: _i32(0, 0))       # send just before START
LIVE_VIEW_KEY = Op(0x820, "LIVE_VIEW_KEY", lambda payload=b"": payload, reply=0x821)

# Night-light
SET_NIGHT_LIGHT = Op(0x7A2, "IOTYPE_VTECH_SET_NIGHT_LIGHT_REQ", lambda v: bytes([int(v) & 0xFF]),
                     reply=0x7A3, ok=_ack_byte0_is(1))
GET_NIGHT_LIGHT = Op(0x7A0, "IOTYPE_VTECH_RM_SERIES_GET_NIGHT_LIGHT_REQ", enc_zero4, reply=0x7A1,
                     dec=_dec_byte(0, "GET_NIGHT_LIGHT"))

# Status LED — the wire flag is "LED disabled": 1 = off
SET_STATUS_LED = Op(0x711, "SET_STATUS_LED", lambda led_off: _i32(0, 1 if led_off else 0),
                    reply=0x712, ok=_ack_word_is(0, 0))
GET_STATUS_LED = Op(0x709, "GET_STATUS_LED", enc_channel, reply=0x710,
                    dec=lambda r: bool(_dec_word(1, "GET_STATUS_LED")(r)))   # True = LED off

# Night-vision mode
SET_NIGHT_VISION = Op(0x715, "IOTYPE_VTECH_SET_MODE_REQ", lambda mode: _i32(0, int(mode)),
                      reply=0x716, ok=_ack_word_is(0, 1))
GET_NIGHT_VISION = Op(0x713, "GET_DAYNIGHT_MODE", enc_zero4, reply=0x714,
                      dec=_dec_word(1, "GET_NIGHT_VISION"))

# Consolidated parameters
GET_PARAMS = Op(0x733, "IOTYPE_VTECH_RM_SERIES_GET_PARAMS_REQ", enc_zero4, reply=0x734, dec=dec_params)
SET_PARAMS = Op(0x731, "SET_PARAMS",
                lambda timer_s, volume, sound_sens, motion_sens, p28: _i32(0, timer_s, volume, sound_sens,
                                                                           motion_sens, p28),
                reply=0x732, ok=_ack_word_is(0, 1))

# Temperature alert thresholds (°C × 10 on the wire)
GET_TEMP_ALERT = Op(0x74B, "GET_TEMP_DETECT", enc_zero4, reply=0x74C, dec=dec_temp_alert)
SET_TEMP_ALERT = Op(0x74D, "SET_TEMP_DETECT",
                    lambda enabled, max_c, min_c: _i32(1 if enabled else 0, round(max_c * 10), round(min_c * 10)),
                    reply=0x74E, ok=_ack_word_is(0, 1))

# Motion detection
GET_MOTION = Op(0x7A4, "IOTYPE_VTECH_GET_MOTION_TRACKING_REQ", enc_zero4, reply=0x7A5, dec=dec_motion)
SET_MOTION = Op(0x7A6, "SET_MOTION_TRACKING",
                lambda sensitivity, record: _i32(int(sensitivity), int(record), 0),
                reply=0x7A7, ok=_ack_byte0_is(1))

# Sound detection
GET_SOUND = Op(0x7B0, "IOTYPE_VTECH_GET_SOUND_SETTING_REQ", enc_zero4, reply=0x7B1,
               dec=lambda r: bool(_dec_word(0, "GET_SOUND")(r)))
SET_SOUND = Op(0x7B2, "SET_SOUND_SETTING", lambda on: _i32(1 if on else 0, 0, 0, 0),
               reply=0x7B3, ok=_ack_word_is(0, 1))

# Clock
GET_DST = Op(0x729, "GET_DAYLIGHT_SAVING", enc_zero4, reply=0x730,
             dec=lambda r: bool(_dec_word(2, "GET_DST")(r)))
SET_DST = Op(0x727, "SET_DAYLIGHT_SAVING", lambda on: _i32(0, 1 if on else 0), reply=0x728,
             ok=_ack_word_is(1, 0))

# Notification interval — the SET's ack is undocumented; treat as fire-and-forget and re-read
GET_NOTIFY_INTERVAL = Op(0x792, "GET_NOTIFY_INTERVAL", enc_zero4, reply=0x793,
                         dec=_dec_word(1, "GET_NOTIFY_INTERVAL"))
SET_NOTIFY_INTERVAL = Op(0x790, "SET_NOTIFY_INTERVAL", lambda index: _i32(0, int(index)))

# Parent unit
GET_PARENT_ACTIVE = Op(0x75A, "GET_PARENT_ACTIVE", enc_zero4, reply=0x75B,
                       dec=lambda r: bool(_dec_word(0, "GET_PARENT_ACTIVE")(r)))

# Lullaby
LULLABY = Op(0x735, "IOTYPE_VTECH_RM_SERIES_STARTSTOP_LULLABY_REQ", lambda track: _i32(0, int(track)),
             reply=0x736, ok=_ack_word_is(0, 1))

# Stored-event list (multi-page reply)
LIST_EVENTS = Op(0x788, "IOTYPE_VTECH_IPCAM_LISTEVENT_REQ",
                 lambda start, end: _i32(0) + stimeday(start) + stimeday(end) + bytes(4),
                 reply=0x789, dec=dec_event_page)


# ── Pushes (camera-initiated, no request) ────────────────────────────────────
EVENT_REPORT = 0x1FFF
TEMP_NOTIFY = 0x738
DAYNIGHT_NOTIFY = 0x785
PARENT_NOTIFY = 0x74F
PARAM_NOTIFY = 0x7AC

PUSH_DECODERS: dict[int, Callable[[bytes], Any]] = {
    EVENT_REPORT: dec_event_report,
    TEMP_NOTIFY: dec_temp_notify,
    DAYNIGHT_NOTIFY: lambda r: bool(_dec_word(0, "DAYNIGHT_NOTIFY")(r)),   # True = night/IR active
    PARENT_NOTIFY: lambda r: bool(_dec_word(0, "PARENT_NOTIFY")(r)),       # True = handset on
    PARAM_NOTIFY: dec_param_notify,
}

ALL_OPS: tuple[Op, ...] = tuple(v for v in globals().values() if isinstance(v, Op))
