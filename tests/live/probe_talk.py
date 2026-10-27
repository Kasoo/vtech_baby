"""Talk-back check. MAKES SOUND in the nursery: run it only with the owner at the camera.

Runs inside the HA image, on a host on the camera's LAN, via run.sh ($VTECH_LIVE_RUNNER), with vtech_lan.zip and the vtech-link module
(_vtech_link.abi3.so) next to the probe:

    cd tests/live && ./run.sh probe_talk.py --owner-present [media URL]
    cd tests/live && ./run.sh probe_talk.py --silent

Plays a 3 s tone (440 Hz, then 880 Hz) through the camera's speaker with the real
vtech_lan.talkback.TalkBack, served from a loopback HTTP server so it goes through the same PyAV path as
any URL. With --silent it plays 3 s of digital silence instead, which needs nobody at the camera. Then plays it again with the live view running, and reports whether camera audio comes back
afterwards. With a media URL, that is played instead of the tone. Changes no settings; the speaker
volume (0x734 @20) is read and reported only.
"""
import io
import math
import os
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import LinkSession, link  # noqa: E402
from vtech_lan.media import LiveStream  # noqa: E402
from vtech_lan.talkback import TalkBack  # noqa: E402

T0 = time.monotonic()


def log(*a):
    print(f"[{time.monotonic() - T0:7.2f}]", *a, flush=True)


def tone_wav(silent: bool = False) -> bytes:
    rate, out = 8000, []
    for freq in (440, 880):
        amp = 0 if silent else 6000
        out += [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(1.5 * rate))]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(out)}h", *out))
    return buf.getvalue()


def serve(body: bytes) -> str:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/tone.wav"


def play(talk: TalkBack, url: str) -> None:
    done = threading.Event()
    result = {}

    def on_end(err):
        result["err"] = err
        done.set()

    frames = talk.stats["frames"]
    t = time.monotonic()
    talk.start(url, on_end)
    log(f"talk-back started in {time.monotonic() - t:.2f} s")
    if not done.wait(600):
        log("still playing after 10 min: stopping")
        talk.stop()
        done.wait(30)
    log(f"talk-back ended: {result.get('err')!r}; {talk.stats['frames'] - frames} frames "
        f"in {time.monotonic() - t:.1f} s")


def main() -> int:
    args = sys.argv[1:]
    if "--silent" in args:
        args = ["--owner-present", serve(tone_wav(silent=True))]
    if "--owner-present" not in args:
        print("This probe makes sound at the camera. Run it only with the owner there, "
              "and pass --owner-present.", file=sys.stderr)
        return 2
    args.remove("--owner-present")
    url = args[0] if args else serve(tone_wav())

    module = link.load(os.path.join(HERE, "_vtech_link.abi3.so"))
    s = LinkSession(module, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                os.environ.get("VTECH_ACCOUNT", "admin"), name="talk", keepalive_s=20)
    log("connected", s.connect(), s.info())
    try:
        log("uplink format", s.get_audio_out_format())
        log("speaker volume (0x734 @20)", s.get_params().volume)
        stream = LiveStream(lambda: s, name="talk")
        talk = TalkBack(lambda: s, name="talk", audio_wanted=lambda: stream.running)

        log("── 1. talk-back alone")
        play(talk, url)

        log("── 2. talk-back with the live view running")
        sub = stream.subscribe(lambda _d: None)
        try:
            time.sleep(3)
            before = stream.stats["audio"]
            log(f"live view: {before} audio frames before talking")
            play(talk, url)
            mid = stream.stats["audio"]
            time.sleep(3)
            after = stream.stats["audio"]
            log(f"live view: {mid - before} audio frames while talking, {after - mid} in the 3 s after "
                f"(camera audio {'resumed' if after > mid else 'did NOT resume'})")
        finally:
            sub.close()
            stream.close()
        log("talk-back stats", talk.stats)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
