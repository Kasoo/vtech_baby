"""Live-view receive counters over a fixed time, through the SDK — READ-ONLY (no light, sound or setting
changes; frames are counted, not kept).

The SDK-side twin of vtech-link's `vtech-link stream`, for comparing the two on the same camera: frames,
keyframes, frames lost, pauses between video frames, and the SDK's receive counters (`tk_av_recv_stats`,
`tutk-host` only). With a `tutk-host` debug build, `TK_DROP=N` drops every Nth media packet and
`TK_TRACE=1` logs each skipped and rebuilt packet.

    cd tests/live && ./run.sh probe_counters.py [seconds]

Also runs outside run.sh with `VTECH_LIBDIR` naming the library set and `vtech_lan.zip` next to it.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import Session, ops, sdk  # noqa: E402
from vtech_lan.media import is_keyframe  # noqa: E402

PAUSE_S = 0.5              # a gap between video frames at least this long is logged
STATS_EVERY_S = 60.0


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 600
    s_dk = sdk.load(os.environ.get("VTECH_LIBDIR", HERE))
    s = Session(s_dk, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                os.environ.get("VTECH_ACCOUNT", "admin"), name="counters", keepalive_s=0)
    t0 = time.monotonic()
    devinfo = s.connect()
    print(f"connected in {(time.monotonic() - t0) * 1000:.0f} ms: {devinfo}", flush=True)
    print(f"resolution: {s.get_quality()!r}", flush=True)
    src = s.media_source()
    src.start()
    for op in (ops.AUDIO_START, ops.VIDEO_FULLRATE, ops.START):
        s.send(op)
    started = time.monotonic()
    print(f"streaming from {time.time():.3f} (wall clock)", flush=True)
    video = keys = audio = bad = lost = 0
    last_video = first_key = None
    next_stats = started + STATS_EVERY_S
    try:
        while (now := time.monotonic()) - started < secs:
            for f in src.poll():
                lost += f.lost_before
                if f.data is None:
                    bad += 1
                    continue
                if not f.video:
                    audio += 1
                    continue
                video += 1
                if is_keyframe(f.data):
                    keys += 1
                    first_key = first_key or f.arrived - started
                if last_video is not None and f.arrived - last_video >= PAUSE_S:
                    print(f"{f.arrived - started:8.3f} pause: {(f.arrived - last_video) * 1000:.0f} ms between video "
                          "frames", flush=True)
                last_video = f.arrived
            if now >= next_stats:
                next_stats += STATS_EVERY_S
                print(f"{now - started:8.3f} receive: {src.stats()}", flush=True)
    finally:
        for op in (ops.STOP, ops.AUDIO_STOP):
            s.send(op)
        stats = src.stats()
        src.close()
        s.close()
    print(f"stream: {video} video ({keys} key, {video / secs:.1f}/s), {audio} audio, {bad} bad, {lost} lost; "
          f"first key {first_key}", flush=True)
    print(f"receive: {stats}", flush=True)


if __name__ == "__main__":
    main()
