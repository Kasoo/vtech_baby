"""Opcode table: encodings and decodings against the byte layouts in vtech-lan-kit/docs/30, 60, 70."""
import datetime as dt
import struct

import pytest

from vtech_lan import ops
from vtech_lan.errors import ProtocolError


def i32(*w):
    return struct.pack(f"<{len(w)}i", *w)


def test_codes_unique_and_replies_explicit():
    codes = [o.code for o in ops.ALL_OPS]
    assert len(codes) == len(set(codes))
    # The two documented exceptions to "reply = request + 1".
    assert ops.GET_STATUS_LED.reply == 0x710
    assert ops.GET_DST.reply == 0x730
    # Fire-and-forget ops.
    for op in (ops.SND_DATA_DELAY, ops.START, ops.VIDEO_FULLRATE, ops.SET_NOTIFY_INTERVAL,
               ops.SPEAKER_START, ops.AUDIO_START):
        assert op.reply is None


@pytest.mark.parametrize("op,args,expected", [
    (ops.SND_DATA_DELAY, (), i32(0)),
    (ops.START, (), i32(0, 0)),
    (ops.VIDEO_FULLRATE, (), i32(0, 0)),
    (ops.SET_STREAM_CTRL, (ops.Quality.FHD_1080P,), i32(0) + bytes([2, 0, 0, 0])),
    (ops.SET_NIGHT_LIGHT, (ops.NightLight.RED,), bytes([3])),
    (ops.SET_STATUS_LED, (True,), i32(0, 1)),          # 1 = LED off
    (ops.SET_STATUS_LED, (False,), i32(0, 0)),
    (ops.SET_NIGHT_VISION, (ops.NightVisionMode.OFF,), i32(0, 2)),
    (ops.SET_PARAMS, (1800, 5, 2, 50, 77), i32(0, 1800, 5, 2, 50, 77)),
    (ops.SET_TEMP_ALERT, (True, 25.5, 16.0), i32(1, 255, 160)),
    (ops.SET_MOTION, (ops.MotionSensitivity.HIGH, 1), i32(75, 1, 0)),
    (ops.SET_SOUND, (True,), i32(1, 0, 0, 0)),
    (ops.SET_DST, (True,), i32(0, 1)),
    (ops.SET_NOTIFY_INTERVAL, (ops.NotifyInterval.MIN_5,), i32(0, 3)),
    (ops.LULLABY, (ops.LullabyTrack.STOP,), i32(0, -1)),
    (ops.LULLABY, (ops.LullabyTrack.BIRDS,), i32(0, 9)),
    (ops.GET_PARAMS, (), i32(0)),
    (ops.GET_STATUS_LED, (), i32(0, 0)),
])
def test_encodings(op, args, expected):
    assert op.encode(*args) == expected


def test_request_sizes():
    assert len(ops.DEVINFO.encode()) == 20
    assert len(ops.GET_TIMEZONE.encode()) == 268
    assert len(ops.LIST_EVENTS.encode(dt.datetime(2026, 9, 22, tzinfo=ops.UTC),
                                      dt.datetime(2026, 9, 23, tzinfo=ops.UTC))) == 24


def test_stimeday_roundtrip_and_wday():
    t = dt.datetime(2026, 9, 20, 13, 5, 7, tzinfo=ops.UTC)       # a Sunday
    b = ops.stimeday(t)
    assert b == struct.pack("<HBBBBBB", 2026, 9, 20, 1, 13, 5, 7)
    assert ops.parse_stimeday(b) == t
    sat = ops.stimeday(dt.datetime(2026, 9, 26, tzinfo=ops.UTC))
    assert sat[4] == 7
    # Converted to UTC first.
    bst = dt.timezone(dt.timedelta(hours=1))
    assert ops.stimeday(dt.datetime(2026, 9, 20, 14, 5, 7, tzinfo=bst)) == b
    assert ops.parse_stimeday(bytes(8)) is None


def test_devinfo():
    r = b"RM5754".ljust(16, b"\0") + b"vtech".ljust(16, b"\0") + bytes([9, 5, 2, 4]) + bytes(20)
    assert ops.DEVINFO.dec(r) == ops.DevInfo("RM5754", "vtech", "4.2.5.9")


def test_params():
    r = i32(21, -1, 70, 1800, 2, 5, 50, 1234, 0)
    p = ops.GET_PARAMS.dec(r)
    assert (p.temp_c, p.lullaby_track, p.temp_f, p.lullaby_timer_s) == (21, -1, 70, 1800)
    assert (p.sound_sensitivity, p.volume, p.motion_sensitivity, p.p28) == (2, 5, 50, 1234)
    with pytest.raises(ProtocolError):
        ops.GET_PARAMS.dec(r[:32])


def test_simple_gets():
    assert ops.GET_NIGHT_LIGHT.dec(bytes([2])) == 2
    assert ops.GET_STATUS_LED.dec(i32(0, 1)) is True         # LED off
    assert ops.GET_NIGHT_VISION.dec(i32(0, 1)) == 1
    assert ops.GET_STREAM_CTRL.dec(i32(0) + bytes([1, 0, 0, 0])) == 1
    assert ops.GET_TEMP_ALERT.dec(i32(1, 255, 160)) == ops.TempAlert(True, 25.5, 16.0)
    assert ops.GET_MOTION.dec(i32(25, 1, 0)) == ops.MotionSetting(25, 1)
    assert ops.GET_SOUND.dec(i32(1, 0, 0, 0)) is True
    assert ops.GET_DST.dec(i32(0, 1, 1)) is True
    assert ops.GET_NOTIFY_INTERVAL.dec(i32(0, 4)) == 4
    assert ops.GET_PARENT_ACTIVE.dec(i32(1)) is True
    assert ops.GET_TIMEZONE.dec(i32(16, 1, 60, 72)) == ops.Timezone(60, 72)
    fmt = ops.GET_AUDIO_OUT_FORMAT.dec(i32(0, 0x8A) + bytes(4))
    assert fmt.codec == 0x8A and fmt.sample_rate_idx == 0


def test_acks():
    assert ops.SET_NIGHT_LIGHT.ok(bytes([1])) and not ops.SET_NIGHT_LIGHT.ok(bytes([0]))
    assert ops.SET_STATUS_LED.ok(i32(0)) and not ops.SET_STATUS_LED.ok(i32(1))
    assert ops.SET_NIGHT_VISION.ok(i32(1))
    assert ops.SET_PARAMS.ok(i32(1)) and not ops.SET_PARAMS.ok(i32(0))
    assert ops.SET_TEMP_ALERT.ok(i32(1))
    assert ops.SET_MOTION.ok(bytes([1]))
    assert ops.SET_SOUND.ok(i32(1, 0, 0, 0))
    assert ops.SET_DST.ok(i32(0, 0)) and not ops.SET_DST.ok(i32(0, 1))
    assert ops.LULLABY.ok(i32(1, 5))
    assert not ops.SET_PARAMS.ok(b"")


def test_pushes():
    ts = 1_790_000_000
    ev = struct.pack("<HBBBBBB", 2026, 9, 21, 2, 22, 13, 20) + struct.pack("<I", ts) + i32(0, 0x15, 0)
    rep = ops.PUSH_DECODERS[ops.EVENT_REPORT](ev)
    assert rep.code == ops.EventCode.SOUND
    assert rep.timestamp == dt.datetime.fromtimestamp(ts, ops.UTC)
    assert rep.local_time == dt.datetime(2026, 9, 21, 22, 13, 20)
    assert ops.PUSH_DECODERS[ops.TEMP_NOTIFY](i32(22, 72, 1)) == ops.TempNotify(22, 72, ops.TempState.TOO_HOT)
    assert ops.PUSH_DECODERS[ops.DAYNIGHT_NOTIFY](i32(1)) is True
    assert ops.PUSH_DECODERS[ops.PARENT_NOTIFY](i32(0)) is False
    pn = ops.PUSH_DECODERS[ops.PARAM_NOTIFY](i32(8, 1) + bytes(16))
    assert pn == ops.ParamNotify(ops.ParamSubtype.STATUS_LED, 1)


def event_page(total, index, end, records):
    head = i32(0, total) + bytes([index, end, len(records), 0])
    body = b"".join(ops.stimeday(t) + bytes([4, 2, 0xAA, 0xBB]) for t in records)
    return head + body


def test_event_page():
    t1 = dt.datetime(2026, 9, 22, 8, 0, 1, tzinfo=ops.UTC)
    t2 = dt.datetime(2026, 9, 22, 9, 30, 0, tzinfo=ops.UTC)
    page = ops.dec_event_page(event_page(2, 0, 1, [t1, t2]))
    assert page.total == 2 and page.end
    assert page.records == (ops.StoredEvent(t1, 4, 2), ops.StoredEvent(t2, 4, 2))
    assert not ops.dec_event_page(event_page(40, 0, 0, [t1])).end
    assert ops.dec_event_page(event_page(0, 0, 0, [])).end         # count 0 ends the list too
