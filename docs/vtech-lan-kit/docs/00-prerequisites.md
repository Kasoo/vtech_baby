# 00 — Prerequisites

Two categories of prerequisite are required before a session can be opened: **key material** (per-camera
credentials + an SDK license) and the **native SDK libraries** (with the load-time patch/companion).

## What this kit does and does not provide

This kit specifies how to *use* the key material and the SDK libraries. The **per-camera** credentials
are **provisioned offline from the owner's pairing QR** — the UID and AV password are in the QR and the
authKey is derived from the AV password; see `05-provisioning.md`. The app-wide values that provisioning
needs, **and the SDK license key**, are the same for every camera; they are needed to interoperate, so the
reference implementation embeds them (`../client/`). The one input the user must supply is the **SDK
libraries** for their architecture: the kit does not host or redistribute those binaries, but may point
the user at a public source to obtain them (`../sdk/README.md`).

A Home Assistant integration built on this kit therefore asks the user for just two things: the SDK
libraries (fetched from a public source), and their pairing QR (from which it derives the credentials).

## Key material

### App-wide (identical for every camera of this app family)

| Item | Source | Used for |
|---|---|---|
| **SDK license key** | embedded in the reference client | Passed to `TUTK_SDK_Set_License_Key` at init. Without it the SDK will not operate. |
| **QR AES key + IV, authKey HKDF salt** | embedded in the reference client (`../client/vtech_provision.py`) | Provisioning the per-camera credentials from the pairing QR (`05-provisioning.md`). Non-rotating. |

The license key is embedded in the reference client (`DEFAULT_LICENSE_KEY`), so no file is required. It
can still be **overridden** by a `LICENSE_KEY.txt` file in the library directory (`VTECH_LIBDIR`)
containing a `license_key=...` line, which takes precedence when present.

### Per-camera (specific to the individual device)

All of these come from the pairing QR via `05-provisioning.md` — the UID and AV password are encoded in
it, and the authKey is derived from the AV password. The client consumes them as the following inputs:

| Item | Env var | Origin | Used for |
|---|---|---|---|
| **UID** | `VTECH_UID` | QR (TLV type 3) | The Kalay device identifier; the connect target. |
| **AV account** | `VTECH_ACCOUNT` (default `admin`) | fixed (`admin`) | AV-layer authentication. |
| **AV password** | `VTECH_PWD` | QR (TLV type 5) | AV-layer authentication. |
| **IOTC authKey** | `VTECH_AUTHKEY` | **derived** from the AV password (`05-provisioning.md`) | Required because this camera runs in enhanced-security mode (see below). |

Produce these from a pairing QR with `../client/vtech_provision.py` (e.g. `--image photo.jpg --env`),
which prints the `VTECH_*` exports directly.

### Fixed connection parameters (not secret)

| Parameter | Value |
|---|---|
| security mode | **DTLS (1)** |
| auth type (both layers) | **PASSWORD (0)** |
| channel | **0** |

The RM5754HD runs in **enhanced-security mode**: it must be connected with `IOTC_Connect_ByUIDEx` plus a
valid **authKey**. A keyless connect is rejected with `-68` (`IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY`).
The AV session is started with DTLS (`security_mode = 1`).

## Native libraries

The four ThroughTek/TUTK Kalay SDK libraries (`libIOTCAPIs.so`, `libAVAPIs.so`, `libNebulaAPIs.so`,
`libTUTKGlobalAPIs.so`), for the host's architecture, plus the companion (`libtk.so`). The SDK libraries
are user-supplied (fetched from a public source — `../sdk/README.md`); the kit never ships the binaries.
`../sdk/README.md` gives the required version (SDK 4.3.8.1); `../tooling/` has the
load-time patch and the companion build. On a host that is not Android, patch the SDK set with
`../tooling/patch.sh` (or `../tooling/patch_sdk.py` plus a prebuilt companion), then point `VTECH_LIBDIR`
at the output directory (the embedded license is used automatically; drop a `LICENSE_KEY.txt` alongside
only to override it).

## Host / networking

- The client host must share the camera's **L2 LAN segment**. A client on a different subnet cannot open
  a session with the camera.
- The connect target is the camera's **UID**; there is no connect-by-IP. The UID is the address passed to
  the connect call.
- The SDK's LAN search is a **UDP broadcast to each local subnet's broadcast address**: a 52-byte and an
  88-byte probe, repeated about every 100 ms. The destination port is set by `IOTC_Set_LanSearchPort` and
  otherwise defaults to **32761**.
- The RM5754HD **does not answer on 32761** (nor 32108): those ports give ICMP port-unreachable and the
  default search receives no reply.
- The camera **answers the 88-byte probe on fixed UDP port 32762** (a 200-byte reply), by broadcast or by
  unicast. The camera also answers on the ephemeral UDP port its live sessions use (that port is random
  per camera boot), but 32762 is fixed and is sufficient; the ephemeral port is not needed.
- The camera's search responder is **rate-limited**: under rapid probing some probes draw no reply, so a
  single probe is not a reliable liveness test; gently spaced probes are answered consistently.
- **`IOTC_Set_LanSearchPort(32762)` makes the LAN search succeed**: the broadcast reaches the camera, it
  replies, and `IOTC_Connect_ByUIDEx` resolves LAN-direct (`Mode = 2`) in about 0.3 s, both with no internet and
  with full internet. The call takes a host-order port, rejects a value above
  65535 with **-46**, and works before or after `IOTC_Initialize2`.
- Without `IOTC_Set_LanSearchPort(32762)` (i.e. at the default 32761), a LAN-direct (`Mode = 2`) session
  only happens **after ThroughTek's master introduces the camera**: the master hands the client the
  camera's LAN address and port, and the data path then runs directly on the LAN. `Mode = 2` describes the
  data path, not how the camera was found.
- `IOTC_Session_Check`'s info carries the remote IP as a string at byte **23** and the remote port as a
  little-endian u16 at byte **70**.
- The search broadcast is link-local and is not forwarded by a router, so a cross-subnet client cannot
  locate the camera.
- To attach a test client at L2 to a **WiFi** camera, use an **ipvlan L2** virtual interface (it shares
  the parent's MAC). A **macvlan** interface has a distinct MAC that the access point does not deliver.
- The camera is **IPv4-only** (it has no IPv6 address). The LAN path to it, and the LAN-search broadcast,
  are IPv4. A client with no IPv4 address has no LAN path to the camera.
- When the session is **LAN-direct**, `IOTC_Session_Check` byte 0 is **2 (LAN)**, the remote address is
  the camera's LAN IP, and control, telemetry, and media flow directly between client and camera. A
  reachable camera does **not** guarantee this: the SDK races the LAN search against the cloud path, and a
  client with an internet route has been seen to come up in relay mode with the LAN path working (in a
  test network namespace); on a host on the camera's LAN the same setup gave LAN.
- With the search port at the default (32761) and **no route to the internet on any address family**, the
  connect **fails** (`-2`/`-13` after ~14 s): the default search gets no reply and the
  master is unreachable. With `IOTC_Set_LanSearchPort(32762)` the same offline connect **succeeds**,
  LAN-direct, in about 0.3 s, with only LAN packets on the wire. (When building
  an offline test client, note that an ipvlan interface with no configured IPv6 still takes SLAAC
  addresses and an IPv6 default route from the router's advertisements, giving a NAT64 path to the cloud;
  check `ip -6 route` as well as `ip route`.)
- **`IOTC_Set_LanSearchPort(32762)` does not prevent cloud traffic.** When the client has internet, the
  connect resolves a ThroughTek master domain by DNS (`*-master-*.iotcplatform.com`) and exchanges STUN
  with ThroughTek servers on **UDP 3478**, and contacts master/relay servers (ports 80/443/8080), **in
  parallel with the LAN search — even when the search port is 32762 and the session comes up LAN-direct**
  Setting the search port fixes which port discovery uses; it does not stop the
  cloud path. A LAN-direct **data path** (from discovery succeeding) and **no cloud contact** (from making
  ThroughTek unreachable) are independent: achieving both requires `IOTC_Set_LanSearchPort(32762)` **and**
  route isolation or the companion egress policy.
- When the **LAN path to the camera is unavailable** but the client has internet, the connect completes
  through a **ThroughTek relay**: `IOTC_Session_Check` byte 0 is **1 (relay)**, with traffic to a
  ThroughTek server (e.g. `51.158.202.75`). An established LAN session that loses the LAN path ends with
  **-20016**, and the reconnect then uses the relay. A client with internet but no IPv4 LAN path to the
  camera (e.g. an **IPv6-only client**, since the camera is IPv4-only) always connects by relay, never
  LAN-direct.
- `IOTC_Session_Check` byte 0 reports the mode: **0** P2P, **1** relay, **2** LAN.
- **There is no SDK setting that disables the cloud path**: the master-name
  lookup and the LAN search are started unconditionally on every connect, `IOTC_Initialize`'s master-name
  arguments are ignored, and no connection option prevents the lookup or STUN.
- **To force LAN-only (no ThroughTek contact), make every off-link destination unreachable to the SDK.**
  Either of these gives the SDK the same view:
  - **Route isolation** (standalone hosts): no IPv4 default route, no IPv6 default route (watch for one
    learned from router advertisements), hence no NAT64.
  - **The companion's egress policy** (what the HA integration uses, since HA needs internet for other
    integrations): `libtk.so` wraps the SDK's own `connect`, `sendto` and `getaddrinfo` (they are renamed
    in the patched libraries only; the rest of the process is unaffected). Destinations outside a
    directly attached IPv4 subnet (plus `255.255.255.255`) fail at once with `ENETUNREACH`, all IPv6
    (including NAT64 `64:ff9b::`) is refused, and `getaddrinfo` resolves numeric literals only, so no DNS
    query leaves. This is the same immediate failure route isolation produces, applied as an
    **allowlist**: address blocklists leak (the relay has been reached through an unlisted address).
    The SDK's complete network surface is `socket`/`bind`/`connect`/`sendto`/`send`/`write`/`recv*` and
    `getaddrinfo` (its two raw `syscall()` sites are `mlock2` and `getrandom`), so the wrappers cover
    every way out. Tested in a dual-stack client with internet: no DNS, no STUN and no off-link packet
    left.
  - Either way the client must also call **`IOTC_Set_LanSearchPort(32762)`** (above), or the camera is
    never found on the LAN and, with no cloud, the connect fails.
  - The client should still check `IOTC_Session_Check` and refuse any mode other than **2** as a
    backstop.
- **Blocking only ThroughTek's addresses at the firewall, while leaving the client a default route, is not
  a substitute.** These results were taken without the search port set; whichever case reached the
  master (through an unblocked address or NAT64) connected, and the rest timed out:
  - An **IPv4-only** client, with ThroughTek IPs blocked, falls back to the IPv4 LAN path and connects
    LAN-direct (`Mode = 2`).
  - A **dual-stack** client (IPv4 + IPv6/NAT64), with the ThroughTek IPs blocked on both families, does
    **not** fall back: `IOTC_Connect_ByUIDEx` hangs and returns **-13** (`IOTC_ER_TIMEOUT`) after about
    14 s, even though a working IPv4 LAN path to the camera exists. The block method (silent drop, ICMP
    reject, or an unreachable route) does not change this.
  So on a dual-stack host — the common case for Home Assistant — firewall-blocking ThroughTek breaks the
  connect rather than forcing it onto the LAN. Use route isolation or the companion's policy instead,
  with the search port set. `-13` is a generic connect
  timeout, not a specific "cloud required" indication.
- Supported hosts: 64-bit **x86_64** and **aarch64**, on **glibc or musl** (e.g. a standard Linux host,
  or an Alpine/musl container such as Home Assistant's). The patched set loads in-process by plain
  `dlopen` with no environment variables. An aarch64 host needs a **4K- or 16K-page kernel**, because the
  SDK libraries' segments are 16K-aligned and a 64K-page kernel cannot map them. **32-bit hosts (armv7,
  i386) are not supported.** The reference client's `preflight()` checks all of this before loading.
