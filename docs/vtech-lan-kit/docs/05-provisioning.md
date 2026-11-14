# 05 — Credential provisioning from the pairing QR

The per-camera credentials needed to connect (`00-prerequisites.md`) can all be recovered offline from
one thing the owner physically has: the parent unit's **"Camera Pairing QR Code"** (its "Connect Mobile
Phone" code).

- The **UID** and **AV password** are encoded in the QR.
- The **authKey** is *derived* from the AV password by a fixed construction; it is never transmitted.

Everything the derivation needs beyond the QR is **app-wide static**: the same for every camera of this
family, and non-rotating. Those values are embedded in the reference implementation, so a user supplies
**only their own pairing QR**.

> **What this means for an integration.** A configuration flow asks the user to scan / photograph the
> pairing QR and derives the full credential set from it, with no cloud account. Re-scanning a freshly rotated QR re-provisions — see *Rotation*, below.

The reference implementation of everything here is `../client/vtech_provision.py` (pure-Python crypto;
reading a QR out of a photo additionally needs `pyzbar` or `opencv-python`).

## App-wide constants

| Constant | Size | Role | In the reference client |
|---|---|---|---|
| QR AES-128 key | 16 bytes | decrypts the pairing QR | `QR_AES_KEY` |
| QR AES-128 IV  | 16 bytes | decrypts the pairing QR | `QR_AES_IV` |
| authKey HKDF salt (master key) | 16 bytes | salts the authKey HKDF | `HKDF_MASTER_KEY` |

They are the same for every camera and do not rotate. The values are in `../client/vtech_provision.py`.

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
| `2` | device name (e.g. `VTECH_5754_xxxx`) |
| `3` | **UID** (the Kalay device identifier, `VTECH_UID`) |
| `5` | **AV password** (`VTECH_PWD`) |
| `7` | MAC address (6 bytes) |

The UID (type 3) and AV password (type 5) are the two credentials carried by the QR.

## Other QR formats

The format above (version byte `0x05`) is the one this specification provisions from, and the one the
RM5754HD emits. Other models in the family emit other formats under the same base64 + AES-128-CBC
envelope (same app-wide key and IV), differing in the header and the payload shape:

- **Version `0x01`** — a 2-byte header `[0x01][plaintext_len:1]`, then the AES-128-CBC ciphertext. The
  plaintext is **ASCII, comma-separated**, four fields per camera and one or more cameras per code:
  `idx,name,uid,token[,idx,name,uid,token …]`. The per-camera `name` contains `VTECH_`, the `uid` is the
  20-character Kalay identifier, and the 8-character `token` is a **registration token, not the AV
  password**. A camera that emits this format **does not carry its AV password in the QR**: the AV
  password is held in the owner's account and is not recoverable from the QR, so the offline route above
  cannot produce a full credential set for it. The UID is still valid. (Seen on the RM5764HD / RM7766.)
- **Other version bytes** have not been characterised. The envelope is likely the same; the header and
  payload shape are unknown until one is seen.

The authKey derivation below is **not** format-specific: it is a function of the AV password alone, so for
any model whose AV password can be obtained, the authKey is derived the same way.

The reference client's `inspect_qr()` / `vtech_provision.py --inspect` classifies a QR by version byte
without assuming version 5, and recovers the UID from a version-1 QR.

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
password/authKey makes the connect fail at once: the camera answers the LAN search for its UID with a
wrong-authKey reject (`-68`). It does not go silent, so a connect that gets no search reply at all means
the camera is off the network or still booting, not that its credentials rotated. (That a key from before
a rotation gets the same reject as any other wrong key is assumed, not confirmed.) Recovery is simply
to **re-scan the current pairing QR**: the new AV password comes straight out of it and the new authKey
falls out of the HKDF. An integration can drive this from its re-authentication flow (`90-ha-runtime.md`)
by asking for a fresh QR rather than for typed credentials.
