"""Basic live check — READ-ONLY (no light, sound or setting changes; safe without an observer).

Runs inside the HA image, on a host on the camera's LAN, via run.sh ($VTECH_LIVE_RUNNER), with vtech_lan
shipped as a zip:

    cd tests/live && ./run.sh [watch_seconds] [cycles]

It connects, reads DEVINFO and every GET, lists the last day's stored events, runs the live-view key
exchange (no video START), then watches pushes and keepalives for `watch_seconds`. It repeats that for
`cycles` sessions in one process to exercise close + reconnect (session ID reuse).
"""
import datetime as dt
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import Push, Session, ops, sdk  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("vtech_lan").setLevel(logging.DEBUG)

WATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 120
CYCLES = int(sys.argv[2]) if len(sys.argv) > 2 else 2


def license_key():
    with open(os.path.join(HERE, "LICENSE_KEY.txt")) as f:
        return next(ln.split("license_key=", 1)[1].strip() for ln in f if "license_key=" in ln)


def ts():
    return time.strftime("%H:%M:%S")


def on_push(p: Push):
    print(f"{ts()}  push 0x{p.code:x}: {p.value}", flush=True)


def on_lost(e):
    print(f"{ts()}  LOST: {type(e).__name__}: {e}", flush=True)


def main():
    t0 = time.monotonic()
    s_dk = sdk.load(HERE, license_key())
    print(f"sdk loaded: arch={s_dk.arch} in {time.monotonic() - t0:.2f}s", flush=True)
    for cycle in range(1, CYCLES + 1):
        s = Session(s_dk, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                    os.environ.get("VTECH_ACCOUNT", "admin"), name=f"cycle{cycle}",
                    on_push=on_push, on_lost=on_lost, keepalive_s=20)
        t0 = time.monotonic()
        info = s.connect()
        print(f"\n== cycle {cycle}: connected in {time.monotonic() - t0:.2f}s sid={s.sid} av={s.av} {info}",
              flush=True)
        reads = [
            ("params", s.get_params), ("night_light", s.get_night_light), ("status_led_on", s.get_status_led),
            ("night_vision", s.get_night_vision), ("quality", s.get_quality), ("temp_alert", s.get_temp_alert),
            ("motion", s.get_motion), ("sound_detect", s.get_sound_detect),
            ("notify_interval", s.get_notify_interval), ("parent_active", s.get_parent_active),
            ("timezone", s.get_timezone), ("dst", s.get_dst), ("audio_out_format", s.get_audio_out_format),
        ]
        for name, fn in reads:
            t0 = time.monotonic()
            try:
                v = fn()
                print(f"  {name:17} = {v}   ({(time.monotonic() - t0) * 1000:.0f} ms)", flush=True)
            except Exception as e:                      # noqa: BLE001
                print(f"  {name:17} !! {type(e).__name__}: {e}", flush=True)
        end = dt.datetime.now(ops.UTC)
        try:
            evs = s.list_events(end - dt.timedelta(days=1), end)
            times = [e.time for e in evs if e.time]
            order = ("ascending" if times == sorted(times) else
                     "descending" if times == sorted(times, reverse=True) else "unordered")
            print(f"  stored events (24h) = {len(evs)}, {order}; first={times[0] if times else None} "
                  f"last={times[-1] if times else None} kinds={sorted({(e.event, e.status) for e in evs})}",
                  flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"  stored events !! {type(e).__name__}: {e}", flush=True)
        try:
            m = s.live_view_master()
            print(f"  live-view key exchange OK (master {len(m)} bytes)", flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"  live-view key exchange !! {type(e).__name__}: {e}", flush=True)
        print(f"  watching {WATCH}s (keepalive every 20s) ...", flush=True)
        end_t = time.monotonic() + WATCH
        while time.monotonic() < end_t and s.connected:
            time.sleep(1)
        print(f"  still connected after watch: {s.connected}", flush=True)
        t0 = time.monotonic()
        s.close()
        print(f"  closed in {time.monotonic() - t0:.2f}s", flush=True)
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
