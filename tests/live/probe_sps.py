"""Watch the video's SPS/PPS and frame rate per second — READ-ONLY on this session.

    cd tests/live && ./run.sh probe_sps.py [seconds]

A separate session streams video (no audio) and logs, every second, frames and keyframes received, and on
every keyframe whether its SPS/PPS differ from the previous keyframe's (with the decoded size). Used to
see what a night-light or day/night change does to the H.264 headers while someone else drives the
camera (the HA integration, through its API).
"""
import ctypes as C
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import Session, ops, sdk  # noqa: E402
from vtech_lan.crypto import decrypt_frame  # noqa: E402
from vtech_lan.media import is_keyframe  # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 60


def license_key():
    with open(os.path.join(HERE, "LICENSE_KEY.txt")) as f:
        return next(ln.split("license_key=", 1)[1].strip() for ln in f if "license_key=" in ln)


def nals(au: bytes):
    out, i, starts = [], 0, []
    while (i := au.find(b"\x00\x00\x01", i)) != -1:
        starts.append(i + 3)
        i += 3
    for a, b in zip(starts, starts[1:] + [len(au) + 3]):
        out.append(au[a:b - 3].rstrip(b"\x00"))
    return out


def size_of(au: bytes) -> str:
    try:
        import av
        dec = av.CodecContext.create("h264", "r")
        f = (dec.decode(av.Packet(au)) + dec.decode(None))[0]
        return f"{f.width}x{f.height}"
    except Exception as e:                                    # noqa: BLE001
        return f"?({type(e).__name__})"


def main():
    s_dk = sdk.load(HERE, license_key())
    s = Session(s_dk, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                os.environ.get("VTECH_ACCOUNT", "admin"), name="sps", keepalive_s=20)
    s.connect()
    master = s.live_view_master()
    s_dk.clean_media_buffers(s.av)
    s.send(ops.VIDEO_FULLRATE)
    s.send(ops.START)
    rd = s_dk.frame_reader(s.av)
    t0 = time.monotonic()
    sec, frames, keys, last_hdr, last_gap = 0, 0, 0, None, time.monotonic()
    maxgap = 0.0
    try:
        while time.monotonic() - t0 < SECS:
            rc, fr = rd.video()
            now = time.monotonic()
            if rc > 0:
                au = decrypt_frame(fr, master)
                frames += 1
                maxgap, last_gap = max(maxgap, now - last_gap), now
                if is_keyframe(au):
                    keys += 1
                    hdr = [n for n in nals(au) if n and n[0] & 0x1F in (7, 8)]
                    h = hashlib.sha1(b"|".join(hdr)).hexdigest()[:10]
                    if h != last_hdr:
                        sps = next((n for n in hdr if n[0] & 0x1F == 7), b"")
                        print(f"[{now - t0:6.2f}] KEYFRAME headers {last_hdr} -> {h}  sps={sps[:16].hex()} "
                              f"len(sps)={len(sps)} size={size_of(au)}", flush=True)
                        last_hdr = h
            elif rc <= 0:
                time.sleep(0.005)
            if int(now - t0) != sec:
                print(f"[{now - t0:6.2f}] {sec:3d}s: frames {frames:2d} keys {keys} max gap {maxgap * 1000:.0f} ms",
                      flush=True)
                sec, frames, keys, maxgap = int(now - t0), 0, 0, 0.0
    finally:
        s.send(ops.STOP)
        s.close()


if __name__ == "__main__":
    main()
