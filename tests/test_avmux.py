"""avmux: PyAV MPEG-TS of H.264 passthrough + A-law → AAC; live output, keyframe join points, timing."""
import math
import shutil
import struct
import subprocess

import pytest

av = pytest.importorskip("av")
pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")

from vtech_lan.avmux import RATE, AvTsMuxer, join_offset  # noqa: E402


@pytest.fixture(scope="module")
def clip():
    """6 s of 10 fps H.264 like the camera's (no B-frames, keyframe every 2 s) and a 440 Hz A-law tone."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10", "-t", "6",
         "-c:v", "libx264", "-bf", "0", "-g", "20", "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", "-"],
        capture_output=True, check=True).stdout
    aus = [b"\x00\x00\x00\x01\x09" + a for a in raw.split(b"\x00\x00\x00\x01\x09") if a]
    enc = av.CodecContext.create("pcm_alaw", "w")
    enc.sample_rate, enc.layout, enc.format = RATE, "mono", "s16"
    n = 6 * RATE
    f = av.AudioFrame(format="s16", layout="mono", samples=n)
    f.planes[0].update(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / RATE)))
                                for i in range(n)))
    f.sample_rate = RATE
    return aus, b"".join(bytes(p) for p in enc.encode(f))


def is_key(au):
    return b"\x00\x00\x01\x65" in au


def mux(aus, alaw):
    m, chunks = AvTsMuxer(aus[0]), []
    for i, au in enumerate(aus):                       # 100 ms video, two 40 ms + one 20 ms audio blocks
        t = 1_000_000 + i * 9000
        chunks.append(("v", m.video(au, t, is_key(au))))
        for j, (a, b) in enumerate(((0, 320), (320, 640), (640, 800))):
            k = i * 800
            chunks.append(("a", m.audio(alaw[k + a:k + b], t + a * 90_000 // RATE)))
    chunks.append(("end", m.close()))
    return chunks


def probe(path, *args):
    return subprocess.run(["ffprobe", "-v", "error", *args, "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, check=True).stdout


def test_streams_and_clean_decode(clip, tmp_path):
    f = tmp_path / "o.ts"
    f.write_bytes(b"".join(c for _, c in mux(*clip)))
    lines = {ln for ln in probe(f, "-show_entries", "stream=codec_name,width,height,sample_rate,channels")
             .splitlines() if ln.strip()}
    assert lines == {"h264,640,360", "aac,8000,1"}
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""


def test_output_is_live(clip):
    """Bytes come out within a couple of frames, not only at close (max_interleave_delta)."""
    sizes = [len(c) for k, c in mux(*clip) if k == "v"]
    assert sum(1 for s in sizes if s == 0) <= 3
    assert all(len(c) % 188 == 0 for _, c in mux(*clip))


def test_join_points_are_keyframes(clip, tmp_path):
    aus, alaw = clip
    ts = b"".join(c for _, c in mux(aus, alaw))
    offs, i = [], 0
    while (j := join_offset(ts[i:])) is not None:
        offs.append(i + j)
        i += j + 188
    assert len(offs) == sum(map(is_key, aus)) == 3
    f = tmp_path / "late.ts"                            # a reader joining at the second keyframe decodes cleanly
    f.write_bytes(ts[offs[1]:])
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""


def test_audio_follows_arrival_time(clip, tmp_path):
    f = tmp_path / "o.ts"
    f.write_bytes(b"".join(c for _, c in mux(*clip)))
    v = [float(x.strip(",")) for x in probe(f, "-select_streams", "v", "-show_entries", "packet=pts_time").split()]
    a = [float(x.strip(",")) for x in probe(f, "-select_streams", "a", "-show_entries", "packet=pts_time").split()]
    assert abs(a[0] - v[0]) < 0.2 and abs(a[-1] - v[-1]) < 0.3
    assert a == sorted(a)


def test_bursty_audio_and_gaps_stay_monotonic(clip, tmp_path):
    """Audio drained in bursts (same arrival time) and after a 2 s gap: PTS only ever increase, and the
    gap is caught up (re-anchored) instead of leaving audio behind the video."""
    aus, alaw = clip
    m, out = AvTsMuxer(aus[0]), bytearray()
    t = 0
    for burst in range(3):                              # 25 blocks of 40 ms arriving at once, three times
        for k in range(25):
            i = (burst * 25 + k) * 320
            out += m.audio(alaw[i:i + 320], t)
        out += m.video(aus[burst], t, is_key(aus[burst]))
        t += 90_000 + (2 * 90_000 if burst == 1 else 0)   # a 2 s gap before the third burst
    out += m.close()
    f = tmp_path / "b.ts"
    f.write_bytes(bytes(out))
    a = [float(x.strip(",")) for x in probe(f, "-select_streams", "a", "-show_entries", "packet=pts_time").split()]
    assert a == sorted(a) and len(set(a)) == len(a)
    assert a[-1] - a[0] > 3.5                           # bursts at 0, 1 s and 4 s: the gap was caught up


def test_video_arriving_out_of_order_stays_monotonic(clip, tmp_path):
    """A frame whose first fragment came late (recovered by a NAK) is stamped after the next frame's
    arrival: the muxer gets strictly increasing DTS instead of failing with EINVAL."""
    aus, _ = clip
    m, out = AvTsMuxer(aus[0]), bytearray()
    for i, au in enumerate(aus[:20]):
        t = 1_000_000 + i * 9000 + (9500 if i == 5 else 0)   # frame 5 stamped after frame 6
        out += m.video(au, t, is_key(au))
    out += m.close()
    f = tmp_path / "v.ts"
    f.write_bytes(bytes(out))
    v = [float(x.strip(",")) for x in probe(f, "-select_streams", "v", "-show_entries", "packet=dts_time").split()]
    assert len(v) == 20 and v == sorted(v) and len(set(v)) == len(v)


def test_alaw_table_matches_ffmpeg():
    from vtech_lan import avmux
    dec = av.CodecContext.create("pcm_alaw", "r")
    dec.sample_rate, dec.layout = RATE, "mono"
    (f,) = dec.decode(av.Packet(bytes(range(256))))
    want = struct.unpack("<256h", bytes(f.planes[0])[:512])
    assert [avmux._alaw_to_linear(a) for a in range(256)] == list(want)


def test_encoders_are_reused_and_never_shared(clip):
    """A stream borrows an AAC encoder and returns it on close; two streams at once get two."""
    from vtech_lan import avmux
    aus, _alaw = clip
    a = AvTsMuxer(aus[0])
    b = AvTsMuxer(aus[0])
    assert a._enc is not b._enc
    ea, eb = a._enc, b._enc
    a.close()
    b.close()
    c = AvTsMuxer(aus[0])
    assert c._enc in (ea, eb)
    c.close()
    assert ea in avmux._pool and eb in avmux._pool


def test_a_reused_encoder_starts_like_a_fresh_one(clip, tmp_path):
    """A stream on a borrowed encoder decodes cleanly, and its audio starts with the video."""
    mux(*clip)                                          # leaves an encoder in the pool
    f = tmp_path / "again.ts"
    f.write_bytes(b"".join(c for _, c in mux(*clip)))
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""
    v = [float(x.strip(",")) for x in probe(f, "-select_streams", "v", "-show_entries", "packet=pts_time").split()]
    a = [float(x.strip(",")) for x in probe(f, "-select_streams", "a", "-show_entries", "packet=pts_time").split()]
    assert abs(a[0] - v[0]) < 0.2 and abs(a[-1] - v[-1]) < 0.3
