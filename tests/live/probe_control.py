"""Control and push check. CHANGES SETTINGS for a few seconds (the LEDs light), then restores them:

    cd tests/live && ./run.sh probe_control.py --allow-writes [temp_wait_s]

1. Night light: switch it to the next preset, read it back, wait for the 0x7ac push, restore.
2. Status LED: invert it, read it back, wait for the 0x7ac push, restore.
3. Temperature: with alerts left as they are (disabled stays disabled, so no handset alarm), set the
   upper threshold below the current reading and wait for a 0x738 push reporting TOO_HOT, then restore
   the thresholds and wait for IN_RANGE. The camera evaluates the thresholds even with alerts off.

Every 0x738/0x7ac push seen is listed. Exits non-zero if a step fails or a setting is not restored.
"""
import os
import queue
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import Session, ops, sdk  # noqa: E402

T0 = time.monotonic()


def log(*a):
    print(f"[{time.monotonic() - T0:7.2f}]", *a, flush=True)


def license_key():
    with open(os.path.join(HERE, "LICENSE_KEY.txt")) as f:
        return next(ln.split("license_key=", 1)[1].strip() for ln in f if "license_key=" in ln)


def main() -> int:
    args = sys.argv[1:]
    if "--allow-writes" not in args:
        print("This probe switches the LEDs and changes the temperature thresholds for a few seconds. "
              "Pass --allow-writes.", file=sys.stderr)
        return 2
    args.remove("--allow-writes")
    temp_wait = float(args[0]) if args else 90
    pushes: queue.Queue = queue.Queue()

    def on_push(p):
        if p.code in (ops.TEMP_NOTIFY, ops.PARAM_NOTIFY):
            log(f"push 0x{p.code:x}: {p.value}")
            pushes.put(p)

    def wait_push(pred, timeout, what):
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            try:
                p = pushes.get(timeout=left)
            except queue.Empty:
                break
            if pred(p):
                log(f"  {what}: received after {time.monotonic() - start:.2f} s")
                return True
        log(f"  {what}: NOT received in {timeout:.0f} s")
        return False

    s = Session(sdk.load(HERE, license_key()), os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"],
                os.environ["VTECH_PWD"], os.environ.get("VTECH_ACCOUNT", "admin"), name="control",
                keepalive_s=20, on_push=on_push)
    log("connected", s.connect())
    ok = True
    light, led, alert = s.get_night_light(), s.get_status_led(), s.get_temp_alert()
    temp = s.get_params().temp_c
    log(f"before: night_light={light} status_led={led} {alert} temp={temp} C")
    try:
        log("── 1. night light")
        target = ops.NightLight((int(light) % 3) + 1)
        start = time.monotonic()
        s.set_night_light(target)
        ok &= wait_push(lambda p: p.code == ops.PARAM_NOTIFY and p.value.subtype == ops.ParamSubtype.NIGHT_LIGHT,
                        5, "0x7ac night-light push")
        got = s.get_night_light()
        log(f"  set {target!r}, read back {got!r}")
        ok &= got == target
        time.sleep(1)
        s.set_night_light(light)

        log("── 2. status LED")
        start = time.monotonic()
        s.set_status_led(not led)
        ok &= wait_push(lambda p: p.code == ops.PARAM_NOTIFY and p.value.subtype == ops.ParamSubtype.STATUS_LED,
                        5, "0x7ac status-LED push")
        got = s.get_status_led()
        log(f"  set {not led}, read back {got}")
        ok &= got == (not led)
        time.sleep(1)
        s.set_status_led(led)

        log("── 3. temperature")
        start = time.monotonic()
        s.set_temp_alert(max_c=temp - 3, min_c=min(alert.min_c, temp - 6))
        ok &= wait_push(lambda p: p.code == ops.TEMP_NOTIFY and p.value.state == ops.TempState.TOO_HOT,
                        temp_wait, "0x738 TOO_HOT")
        start = time.monotonic()
        s.set_temp_alert(alert.enabled, alert.max_c, alert.min_c)
        ok &= wait_push(lambda p: p.code == ops.TEMP_NOTIFY and p.value.state == ops.TempState.IN_RANGE,
                        temp_wait, "0x738 IN_RANGE")
    finally:
        s.set_night_light(light)
        s.set_status_led(led)
        s.set_temp_alert(alert.enabled, alert.max_c, alert.min_c)
        after = (s.get_night_light(), s.get_status_led(), s.get_temp_alert())
        restored = after == (light, led, alert)
        log(f"after: night_light={after[0]} status_led={after[1]} {after[2]} restored={restored}")
        ok &= restored
        s.close()
    log("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
