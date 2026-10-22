#!/bin/sh
# Regenerate fuzz/corpus/<target>/ seeds from the known-good packets in core/tests/fixtures/tutk_host.txt
# and core/tests/service.rs. The corpora are gitignored.
set -eu
cd "$(dirname "$0")"
exec python3 -I - <<'PY'
import hashlib, os, re

fixtures = {}
for line in open("../core/tests/fixtures/tutk_host.txt"):
    if line.startswith("#") or " = " not in line:
        continue
    name, hexs = line.strip().split(" = ")
    fixtures[name] = bytes.fromhex(hexs)

service = open("../core/tests/service.rs").read()
reply = bytes.fromhex(re.search(r'const REPLY: &str = "([0-9a-f]+)"', service).group(1))
frame = bytes.fromhex(re.search(r'const FRAME: &str = "([0-9a-f]+)"', service).group(1))

def put(target, name, data):
    d = os.path.join("corpus", target)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, hashlib.sha1(name.encode()).hexdigest()[:16]), "wb").write(data)

av = {n: v for n, v in fixtures.items() if n.startswith("av.") and not n.startswith("av.fec")}
iotc = {n: v for n, v in fixtures.items() if n.startswith("iotc.") and (n.endswith(".wire") or n.endswith(".plain") or n.endswith(".built"))}
shards = {n: v for n, v in fixtures.items() if n.startswith("av.fec") and ".shard" in n or n.endswith(".parity")}

for n, v in iotc.items():
    put("iotc_decode", n, v)
    put("iotc_session", n, bytes([1, 0]) + v)
for n, v in av.items():
    for t in ("av_packet", "av_client", "av_server", "control_stream", "media_rx"):
        put(t, n, v)
for n, v in shards.items():
    # A group header is the first 8 bytes of the fuzz input; the rest is the shard.
    put("fec", n, bytes(8) + v)
    put("media_rx", n, v)
put("media_key", "reply", reply)
put("media_key", "frame", frame)
print("seeded", sum(len(f) for _, _, f in os.walk("corpus")), "files")
PY
