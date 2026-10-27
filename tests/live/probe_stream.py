"""Live-view check — READ-ONLY (no light, sound or setting changes; no images are kept or viewed).

Runs inside the HA image, on a host on the camera's LAN, via run.sh ($VTECH_LIVE_RUNNER), with vtech_lan
shipped as a zip and the vtech-link module (_vtech_link.abi3.so) next to the probe:

    cd tests/live && ./run.sh probe_stream.py [seconds] [cycles]

Exercises the real vtech_lan.media.LiveStream: snapshots with nothing
streaming, CYCLES start/stop cycles driven by HTTP clients, then HA's go2rtc usage. A private go2rtc (the HA image's binary, on spare loopback
ports) is then given that URL the way HA's go2rtc integration does (the URL plus
`ffmpeg:<name>#audio=opus`), and driven as HA would: a snapshot, an RTSP consumer, and an Opus consumer.
Reports codecs, time to first output, keyframe spacing, and that everything stops cleanly.
"""
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vtech_lan.zip"))

from vtech_lan import LinkSession, link  # noqa: E402
from vtech_lan.media import LiveStream  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
CYCLES = int(sys.argv[2]) if len(sys.argv) > 2 else 2
T0 = time.monotonic()


def log(*a):
    print(f"[{time.monotonic() - T0:7.2f}]", *a, flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(media: LiveStream, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path != "/cam":
                self.send_error(404)
                return
            q: queue.Queue = queue.Queue(maxsize=600)
            sub = media.subscribe(q.put_nowait)
            log(f"http: client {self.client_address[1]} connected")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            try:
                while (chunk := q.get(timeout=15)) is not None:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, queue.Empty):
                pass
            finally:
                sub.close()
                log(f"http: client {self.client_address[1]} gone")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def go2rtc(url: str):
    api, rtsp = free_port(), free_port()
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "go2rtc.yaml")
    with open(cfg, "w") as f:
        f.write(f"""api:\n  listen: "127.0.0.1:{api}"\nrtsp:\n  listen: "127.0.0.1:{rtsp}"\n"""
                f"""webrtc:\n  listen: ""\nlog:\n  level: warn\n"""
                f"""streams:\n  cam:\n    - {url}\n    - ffmpeg:cam#audio=opus\n""")
    p = subprocess.Popen(["go2rtc", "-c", cfg], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=lambda: [log("go2rtc:", ln.rstrip()) for ln in p.stdout], daemon=True).start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{api}/api", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    return p, api, rtsp


def ffprobe(src: str, extra=()) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", *extra, "-show_entries",
                        "stream=codec_type,codec_name,profile,has_b_frames,width,height,sample_rate,channels",
                        "-of", "compact=p=0:nk=0", "-i", src], capture_output=True, text=True, timeout=40)
    return (r.stdout.strip().replace("\n", " | ") or r.stderr.strip()[-300:])


def consume(src: str, secs: int, extra=()) -> str:
    """Read `src` for `secs` with ffmpeg into a throwaway TS, then report what arrived (codecs, duration)."""
    out = tempfile.mktemp(suffix=".ts")
    t = time.monotonic()
    r = subprocess.run(["ffmpeg", "-v", "error", *extra, "-i", src, "-t", str(secs), "-map", "0",
                        "-c", "copy", "-f", "mpegts", out], capture_output=True, text=True, timeout=secs + 40)
    took = time.monotonic() - t
    info = ffprobe(out, ("-show_entries", "format=duration")) if os.path.exists(out) else "no output"
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                       capture_output=True, text=True).stdout.strip() if os.path.exists(out) else "?"
    try:
        os.remove(out)
    except OSError:
        pass
    return f"took {took:.1f}s, media {d}s: {info} {r.stderr.strip()[-200:]}"


def wait_idle(media, timeout=15):
    end = time.monotonic() + timeout
    while media._thread is not None and time.monotonic() < end:
        time.sleep(0.1)
    return media._thread is None


def main():
    module = link.load(os.path.join(HERE, "_vtech_link.abi3.so"))
    log(f"vtech-link {module.__version__}")
    s = LinkSession(module, os.environ["VTECH_UID"], os.environ["VTECH_AUTHKEY"], os.environ["VTECH_PWD"],
                os.environ.get("VTECH_ACCOUNT", "admin"), name="stream", keepalive_s=20)
    log("connected", s.connect(), s.info())
    media = LiveStream(lambda: s, name="live", grace_s=2.0)
    port = free_port()
    srv = serve(media, port)
    url = f"http://127.0.0.1:{port}/cam"

    log("=== snapshot with nothing streaming")
    for w in (None, 640):
        t = time.monotonic()
        jpg = media.snapshot(width=w)
        log(f"snapshot width={w}: {len(jpg)} B JPEG {jpg[:2].hex()} in {time.monotonic() - t:.2f}s (not kept)")
    log("idle again after grace:", wait_idle(media), media.stats)

    for cycle in range(1, CYCLES + 1):
        log(f"=== cycle {cycle}: ffprobe (probe limited to 1 s / 200 kB)")
        t = time.monotonic()
        log(ffprobe(url, ("-analyzeduration", "1000000", "-probesize", "200000")), f"in {time.monotonic() - t:.1f}s")
        log("idle after grace:", wait_idle(media), {k: media.stats.get(k) for k in
            ("starts", "video", "audio", "keyframes", "bad", "bytes", "receive", "last_error")})

    g, api, rtsp = go2rtc(url)
    log(f"=== go2rtc api :{api} rtsp :{rtsp}")
    t = time.monotonic()
    try:
        jpg = urllib.request.urlopen(f"http://127.0.0.1:{api}/api/frame.jpeg?src=cam", timeout=40).read()
        log(f"go2rtc snapshot: {len(jpg)} B JPEG in {time.monotonic() - t:.1f}s (not kept)")
    except OSError as e:
        log("go2rtc snapshot failed:", e)
    log("go2rtc RTSP consumer:", consume(f"rtsp://127.0.0.1:{rtsp}/cam", SECONDS // 2, ("-rtsp_transport", "tcp")))
    log("go2rtc Opus consumer:", consume(f"rtsp://127.0.0.1:{rtsp}/cam?video&audio=opus", SECONDS // 2,
                                         ("-rtsp_transport", "tcp")))
    log("snapshot while go2rtc may still hold the stream:", len(media.snapshot()), "B")
    g.terminate(); g.wait(5)
    log("idle after go2rtc left:", wait_idle(media), {k: media.stats.get(k) for k in
        ("starts", "video", "audio", "keyframes", "bad", "receive", "last_error")})
    srv.shutdown()
    media.close()
    s.close()
    time.sleep(0.5)
    log("threads left:", sorted(t.name for t in threading.enumerate()))


if __name__ == "__main__":
    main()
