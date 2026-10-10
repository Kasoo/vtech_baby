# 05 — Credential provisioning from the pairing QR

The per-camera credentials needed to connect (`00-prerequisites.md`) are **not independent secrets that
must be captured from the app or the cloud.** They are all recoverable offline from the one artifact the
owner physically has: the parent unit's **"Camera Pairing QR Code"** (its "Connect Mobile Phone" code).

- The **UID** and **AV password** are encoded in the QR.
- The **authKey** is *derived* from the AV password by a fixed construction — it is never transmitted or
  stored, so there is nothing to capture.

Everything the derivation needs beyond the QR is **app-wide static** (the same for every camera of this
app family, non-rotating — the same class of constant as the SDK license key). Those constants are given
below and are embedded in the reference implementation, so a user supplies **only their own pairing QR**.

> **What this means for an integration.** A configuration flow asks the user to scan / photograph the
> pairing QR and derives the full credential set from it. No cloud login, no 2FA, no rooted device, no
> live capture. Re-scanning a freshly rotated QR re-provisions — see *Rotation*, below.

The reference implementation of everything here is `../client/vtech_provision.py` (pure-Python crypto;
reading a QR out of a photo additionally needs `pyzbar` or `opencv-python`).

## App-wide constants

| Constant | Value | Role |
|---|---|---|
| QR AES-128 key | `853bc0248503eea465877351f3ae6fe1` | decrypts the pairing QR |
| QR AES-128 IV  | `384ce8a20d21a518997189cf62413a0f` | decrypts the pairing QR |
| authKey HKDF salt (master key) | `45c9d8785a856d4ee1247d91e094175c` | salts the authKey HKDF |

These are app-family constants, not per-camera and not rotating. (A newer "v3" QR cipher set and an
alternate "AddCamera" cipher set also exist in the same app family but are not used by this QR format.)

## Pairing-QR format

The QR encodes an ASCII **base64** string. Decoded:

```
[ ver : 1 ][ plaintext_len : 2 big-endian ][ AES-128-CBC ciphertext : N ]
```

- `ver` is `0x05` for this format.
- The ciphertext is **AES-128-CBC, no padding** (length is a multiple of 16), under the QR AES key + IV
  above. `plaintext_len` is the declared length of the decrypted content.

The decrypted plaintext is a sequence of **TLV** records, each:

```
00  <type:1>  <len:1>  <value:len>
```

(each record begins with a `0x00` lead byte). The types that appear:

| Type | Field |
|---|---|
| `2` | device name (e.g. `VTECH_5754_e147`) |
| `3` | **UID** (the Kalay device identifier, `VTECH_UID`) |
| `5` | **AV password** (`VTECH_PWD`) |
| `7` | MAC address (6 bytes) |

The UID (type 3) and AV password (type 5) are the two credentials carried by the QR.

## authKey derivation

The IOTC authKey is computed on the client from the AV password and the app-wide master key:

```
OKM     = HKDF-SHA256( salt = HKDF_MASTER_KEY, IKM = AV_password )      # RFC 5869, 8 bytes used
authKey = concat( BASE62[ OKM[i] % 62 ]  for i in 0..7 )               # 8 base62 characters
```

- **HKDF-SHA256** (RFC 5869): Extract `PRK = HMAC-SHA256(salt = HKDF_MASTER_KEY, IKM = AV_password)`,
  then Expand `OKM = HMAC-SHA256(PRK, 0x01)` (empty `info`); the first **8** bytes are used.
- **base62** over `0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz`; each of the 8 bytes
  is reduced `% 62` and mapped through the alphabet, giving an **8-character** authKey.

The authKey is therefore a deterministic function of `(master key, AV password)`. It is passed to
`IOTC_Connect_ByUIDEx` as specified in `10-transport-session.md`.

## Putting it together

```
qr_text  = base64 payload of the pairing QR         (scanned or read from a photo)
fields   = decode_pairing_qr(qr_text)               # -> uid, av_password, name, mac
authKey  = derive_authkey(fields.av_password)
=> VTECH_UID = fields.uid
   VTECH_PWD = fields.av_password
   VTECH_AUTHKEY = authKey
   VTECH_ACCOUNT = "admin"      (fixed for the RM series)
```

That is the full input set for `00-prerequisites.md` (minus the SDK license, which is app-wide and
handled separately there).

## Rotation / self-heal

The **UID is stable**. The **AV password rotates** on a Wi-Fi change or an "update security key" re-pair,
and because the authKey is a deterministic HKDF of the AV password it co-rotates. A stale AV
password/authKey makes the connect fail (`-68` wrong authKey, or a connect timeout). Recovery is simply
to **re-scan the current pairing QR**: the new AV password comes straight out of it and the new authKey
falls out of the HKDF. An integration can drive this from its re-authentication flow (`90-ha-runtime.md`)
by asking for a fresh QR rather than for typed credentials.

## Clean-room note

This document specifies the **format and construction** (QR framing, TLV layout, HKDF+base62) and embeds
the **app-wide constant values** so the kit is self-sufficient. It deliberately does **not** describe how
those app-wide constants were obtained from the vendor software; that is out of scope for this kit, as
with the SDK libraries and license key.
