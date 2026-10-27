"""Characterise the IOCtrl stall seen after a 0x731 SET_PARAMS write (2026-10-05).

Needs vtech_lan.zip and _vtech_link.abi3.so next to it. Writes only the camera's CURRENT values back (no setting changes, no light or sound), then times a
GET 0x733 every second. Scenarios (argv[1]):
  731       0x731 with the current values
  790       0x790 with the current notification interval (fire-and-forget), then 0x792 re-read
  790+731   both, in that order (the HA round-trip sequence that stalled)
  none      no write: the baseline
  replay    the HA round-trip's real changes, each restored: motion high->orig, interval 5 min->orig,
            lullaby timer 3600, then GETs (timer restored at the end)
  timer     only the lullaby timer change to 3600 (restored at the end)
  abandon   connect, then exit WITHOUT closing (leaves a dead session on the camera)
  burst     no write: 8 rounds of all ten GETs back-to-back (an HA full re-read is one round)
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import LinkSession, link  # noqa: E402
from vtech_lan.errors import VtechError  # noqa: E402

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "731"
WATCH = int(sys.argv[2]) if len(sys.argv) > 2 else 45


def ts():
    return time.strftime("%H:%M:%S")


def timed(label, fn):
    t = time.monotonic()
    try:
        v = fn()
        print(f"{ts()} {label}: {time.monotonic() - t:.2f}s {v}", flush=True)
    except VtechError as e:
        print(f"{ts()} {label}: FAILED after {time.monotonic() - t:.2f}s: {type(e).__name__}: {e}", flush=True)


def main():
    s = LinkSession(link.load(os.path.join(HERE, "_vtech_link.abi3.so")), os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                name="stall", keepalive_s=0,
                on_push=lambda p: print(f"{ts()}  push 0x{p.code:x}: {p.value}", flush=True))
    print(f"{ts()} connected: {s.connect()} mode={s.connection_mode()}", flush=True)
    if SCENARIO == "abandon":
        s.get_params()
        print(f"{ts()} abandoning the session (no close)", flush=True)
        os._exit(0)
    before = s.get_params()
    interval = s.get_notify_interval()
    print(f"{ts()} before: {before} interval={interval}", flush=True)
    if "790" in SCENARIO:
        timed("0x790 same interval", lambda: s.set_notify_interval(interval))
    if "731" in SCENARIO:
        timed("0x731 same values", lambda: s.update_params())
    if SCENARIO == "replay":
        from vtech_lan import ops
        motion = s.get_motion()
        timed("0x7a6 motion high", lambda: s.set_motion_sensitivity(ops.MotionSensitivity.HIGH))
        timed("0x7a6 motion restore", lambda: s.set_motion_sensitivity(motion.sensitivity))
        timed("0x790 interval 5 min", lambda: s.set_notify_interval(ops.NotifyInterval.MIN_5))
        timed("0x790 interval restore", lambda: s.set_notify_interval(interval))
    if SCENARIO in ("replay", "timer"):
        timed("0x731 timer 3600", lambda: s.update_params(lullaby_timer_s=3600))
    if SCENARIO == "burst":
        getters = [s.get_params, s.get_night_light, s.get_status_led, s.get_night_vision, s.get_quality,
                   s.get_temp_alert, s.get_motion, s.get_sound_detect, s.get_notify_interval, s.get_parent_active]
        for rnd in range(8):
            t = time.monotonic()
            for g in getters:
                timed(f"burst{rnd} {g.__name__}", g)
            print(f"{ts()} round {rnd}: {time.monotonic() - t:.2f}s", flush=True)
    end = time.monotonic() + WATCH
    while time.monotonic() < end:
        timed("GET 0x733", s.get_params)
        time.sleep(1)
    if SCENARIO in ("replay", "timer"):
        timed("0x731 timer restore", lambda: s.update_params(lullaby_timer_s=before.lullaby_timer_s))
    after = s.get_params()
    print(f"{ts()} unchanged: {after.lullaby_timer_s == before.lullaby_timer_s and after.volume == before.volume}")
    s.close()
    print("DONE")


main()
