"""Phase 5 spike: live-view STOP/START cycles on one session — READ-ONLY (video only, no settings changed).

    cd tests/live && ./run.sh probe_restart.py [secs_per_cycle] [cycles_per_variant]

The first spike saw restarts on the same session that delivered a handful of frames with no keyframe or
SPS/PPS. The vendor app calls avClientCleanVideoBuf before every START (com/tutk/IOTC/w.java). Each
variant is run for several cycles; per cycle we count frames, IDRs, SPS/PPS and the first-IDR delay.
"""
import ctypes as C
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import Session, ops, sdk  # noqa: E402
from vtech_lan.crypto import decrypt_frame  # noqa: E402

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 8
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def license_key():
    with open(os.path.join(HERE, "LICENSE_KEY.txt")) as f:
        return next(ln.split("license_key=", 1)[1].strip() for ln in f if "license_key=" in ln)


def nal_types(pt: bytes) -> set[int]:
    out, i = set(), 0
    while (i := pt.find(b"\x00\x00\x01", i)) != -1 and i < 8192:
        if i + 3 < len(pt):
            out.add(pt[i + 3] & 0x1F)
        i += 3
    return out


def main():
    s_dk = sdk.load(HERE, license_key())
    import vtech_lan.sdk as m
    h = C.CDLL(os.path.join(HERE, "libAVAPIs.so"), mode=os.RTLD_NOW | os.RTLD_LOCAL)
    clean_video = m._bind(h, "avClientCleanVideoBuf", C.c_int, C.c_int)
    clean_all = m._bind(h, "avClientCleanBuf", None, C.c_int)
    s = Session(s_dk, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                os.environ.get("VTECH_ACCOUNT", "admin"), name="restart", keepalive_s=20)
    print("connected", s.connect(), flush=True)
    master = s.live_view_master()
    av = s.av
    fbuf = C.create_string_buffer(1 << 21); finfo = C.create_string_buffer(64)
    a, e, ai, fi = C.c_int(), C.c_int(), C.c_int(), C.c_int()

    def run(label, pre_stop_wait, clean, order):
        t0 = time.monotonic()
        if clean == "video":
            rc = clean_video(av)
        elif clean == "all":
            clean_all(av); rc = "-"
        else:
            rc = "-"
        for op in order:
            s.send(op)
        frames = idr = sps = bad = 0
        first_idr = first_frame = None
        while time.monotonic() - t0 < SECS:
            r = s_dk.recv_frame(av, fbuf, len(fbuf), C.byref(a), C.byref(e), finfo, 64, C.byref(ai), C.byref(fi))
            if r > 60:
                try:
                    pt = decrypt_frame(fbuf.raw[:r], master)
                except Exception:                       # noqa: BLE001
                    bad += 1
                    continue
                frames += 1
                first_frame = first_frame or round(time.monotonic() - t0, 2)
                n = nal_types(pt)
                if 7 in n:
                    sps += 1
                if 5 in n:
                    idr += 1
                    first_idr = first_idr or round(time.monotonic() - t0, 2)
            elif r <= 0 and r not in (-20013, -20014):
                time.sleep(0.003)
        s.send(ops.STOP)
        print(f"  {label:34} clean={str(clean):5} rc={rc} frames={frames:3} ({frames/SECS:4.1f} fps) idr={idr:2} "
              f"sps={sps:2} bad={bad} first_frame={first_frame} first_idr={first_idr}", flush=True)
        time.sleep(pre_stop_wait)

    FR, ST = ops.VIDEO_FULLRATE, ops.START
    variants = [
        ("A: no clean, wait 1.5", 1.5, None, (FR, ST)),
        ("B: clean video, wait 1.5", 1.5, "video", (FR, ST)),
        ("C: clean all, wait 1.5", 1.5, "all", (FR, ST)),
        ("D: clean video, wait 5", 5, "video", (FR, ST)),
        ("E: clean video, START then FULLRATE", 1.5, "video", (ST, FR)),
    ]
    for label, wait, clean, order in variants:
        print(f"== {label}", flush=True)
        for _ in range(N):
            run(label, wait, clean, order)
    s.close()


if __name__ == "__main__":
    main()
