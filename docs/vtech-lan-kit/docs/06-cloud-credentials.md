# 06 — Credential provisioning from the cloud account

`05-provisioning.md` recovers the credential set offline from the pairing QR, in both its formats. The
cloud account is the other source: for any camera already paired to an account, the account's device list
carries the same UID / AV account / AV password.

The credential set pulled from the owner's **cloud account** is the same UID / AV account / AV password that
`05-provisioning.md` yields from the QR, after which the **authKey derivation is identical**
(`06` differs only in *where the AV password comes from*). A standalone "credentials helper" that logs in
and prints the per-camera credential set is enough; the LAN protocol (`10`+) is unchanged.

> **Precondition.** The camera must already be **added to the account in the official app** at least once.
> This spec covers *reading* the account's device list, not first-time pairing (which happens over the
> vendor P2P layer, not an HTTPS endpoint — see *First-time pairing*, below).

The reference implementation of this flow is `tools/vtech_cloud_credentials.py` at the repository root
(stdlib-only; it runs the sequence below and derives each authKey). Provenance: every step was captured live
from the official app (okhttp over TLS) and decoded; marked **[live]** below. The derivation is **[live]**
+ cross-checked byte-for-byte against the on-camera authKey.

## App-wide constants

All non-rotating and the same for every account/camera of this family (same class of fact as the QR keys
in `05`).

| Constant | Value / source | Role |
|---|---|---|
| Account host (**AM1**) | `https://myvtechbaby-am1.vtech.com` | email/password login → OAuth Bearer token |
| Merchant host (**IRM**) | `https://myvt1080p-api.vtech.com` | email → converted-email lookup |
| VSaaS gateway | `https://vsaas-gateway.kalayservice.com` | the device list (and account endpoints) |
| `x-merchant-id` | `vt1080p` | merchant header |
| Merchant `authorization` | the app-static **IRM header** (`getIrmHdr`, an AWS-KMS-wrapped blob, base64, 248 chars) | merchant auth; same for every install |
| 2FA public key | app-static **RSA-2048 public key** (SubjectPublicKeyInfo, base64), embedded in the reference client | encrypts the device-trust `Custom` header (see *Completing the device-trust challenge*) |
| User-Agent | `okhttp/3.12.13` | sent on every request |
| authKey HKDF salt (master key) | as in `05` (`HKDF_MASTER_KEY`) | salts the authKey HKDF |

The merchant `authorization` is **app-static** — verified byte-identical to the embedded `getIrmHdr`
constant across a live login. It is not per-install and does not need to be captured per user.

## The login + fetch sequence [live]

Transport: AM1 and the merchant are HTTP/2; the VSaaS gateway is HTTP/1.1. All over TLS; no certificate
pinning on these paths. The merchant headers (`x-merchant-id`, `x-mobile-notification-id`, `x-mobile-os`,
`authorization`) accompany the two merchant calls (steps 1–2); the VSaaS calls carry only
`Authorization: Bearer <token>`.

```
1. POST {merchant}/api/account/search   body {"email": <plain email>}              -> 200 (existence probe)
2. POST {merchant}/api/account          body {"email": <plain email>}
      -> { "converted_data": { "email": "<64-hex>@vtechinterimeu.com", "name": "<64-hex>" }, ... }
3. POST {AM1}/api/v2/account/login
      body { "email": <converted_data.email>, "password": <plain password>,
             "uuid": <device id>, "deviceName": <device model>, "platform": "android" }
      -> { "token_type": "Bearer", "access_token": <tok>, "refresh_token": <rt>,
           "scope": ["read","read:email"], "expires_in": 10800, ... }
4. GET {vsaas}/api/v1/devices/   header  Authorization: Bearer <access_token>
      -> { "result": [ <device>, ... ], "code": ..., "message": "" }
```

Key points, each a correction of the natural guess:

- **The AM1 `access_token` is the VSaaS Bearer, used directly.** There is **no** `grant_type=refresh_token`
  exchange against `{vsaas}/vsaas/token/` — the token minted in step 3 is sent verbatim in step 4. (A
  `/vsaas/token/` exchange exists in the SDK but was not used by this app build; keep it only as a fallback
  if a future build rejects the AM1 token on VSaaS.)
- **Step 3 logs in with the *converted* email** (`converted_data.email`, a `<hash>@vtechinterimeu.com`
  from step 2), **not** the user's plain address. Steps 1–2 exist to produce that converted email, so they
  are required, not cosmetic.
- The `refresh_token` from step 3 can be persisted to skip the password on later runs (single-use; rotate
  on each exchange if the `/vsaas/token/` fallback is ever used).

### Device record → credential set [live]

`result[]` entries (schema confirmed on the wire; unused fields omitted):

```json
{ "uuid": "<20-char UID>", "uid": "<same UID>", "account": "admin",
  "password": "<8-char AV password>", "nickname": "<name>", "channel": "0",
  "info": { "username": "admin", "password": "<same 8-char AV password>", "nickname": "<name>", ... } }
```

| Credential | Field |
|---|---|
| **UID** | `result[i].uuid` (duplicated at `result[i].uid`) |
| **AV account** | `result[i].account` (= `"admin"`; also `info.username`) |
| **AV password** | `result[i].info.password` (duplicated at `result[i].password`) |
| **authKey** | `derive_authkey(AV password)` — the HKDF construction in `05-provisioning.md` (`base62_8(HKDF-SHA256(salt=master key, ikm=AV password))`) |

The top-level `uid`/`account`/`password` duplicates make a one-level read sufficient.

## The device identifier (`uuid` / `x-mobile-notification-id`) and 2FA

Step 3 sends a `uuid`, reused as the merchant `x-mobile-notification-id`. It is the app's
**trusted-device id**: a login from a **known** id succeeds immediately (200 + tokens); a login from an
**unknown** id triggers VTech's **device-trust challenge** (an emailed verification code) once, after
which that id is trusted.

### Completing the device-trust challenge (2FA) [live/static]

When step 3's `uuid` is not yet trusted, `POST {AM1}/api/v2/account/login` returns **`HTTP 204 No Content`
with an empty body** (the credentials are accepted; only the device is untrusted) and VTech emails a
**numeric verification code**. The 204 response carries **no body and no challenge token** — the material
needed to redeem the code is computed on the client. Redeem it with:

```
3a. POST {AM1}/api/v2/account/authorize
       header  Authorization: Custom <blob>
       body    {"code": "<emailed code>"}
       -> 200  { "token_type":"Bearer", "access_token":<tok>, "refresh_token":<rt>,
                 "scope":["read","read:email"], "expires_in":10800, ... }
```

- The `200` body is the **same token set as a trusted login** (step 3's success shape); feed `access_token`
  straight into step 4. After this succeeds the `uuid` is **trusted**, so later logins with it skip the
  challenge and return `200` directly.
- **`Authorization: Custom <blob>`** is built entirely on the client — it is not returned by the 204:

  ```
  plaintext = f"{converted_email}:{base64(password)}:{uuid}"
                   # converted_email = step-2 converted_data.email; password = the account password (UTF-8,
                   # base64-encoded); uuid = the same device id sent in step 3
  blob      = base64( RSA_encrypt( plaintext_utf8 ) )
  header    = "Custom " + blob
  ```

  `RSA_encrypt` is **RSA with `OAEP` padding, SHA-1 digest and MGF1(SHA-1)** under an **app-wide RSA-2048
  public key** (embedded in the reference client; one constant, non-rotating, the same for every install).
  The output is a fixed 256 bytes. The padding is randomised, so the blob differs on every call for the same
  inputs. The blob carries the account email, password and device id; the body carries the emailed code.
- **Headers on 3a**, exactly: `Authorization: Custom <blob>`, `Accept: application/json`,
  `Content-Type: application/json; charset=utf-8`, `Accept-Encoding: gzip`, `User-Agent: okhttp/3.12.13`.
  Nothing else: like every `{AM1}` call it carries **no merchant headers** (no `x-merchant-id`,
  `x-mobile-notification-id`, `x-mobile-os`, and no IRM `authorization`; those go only to the merchant host
  in steps 1–2). The `uuid` is not a header here; it travels only inside the blob.
- **Unknown:** the response to a wrong code and to an expired code (status and body), how long a code stays
  valid, and whether a repeated step-3 login with the same untrusted `uuid` sends a new code. A client
  should report the server's response as it is rather than assume a shape.
- After a successful `authorize` the app issues `PUT {AM1}/api/v1/region/` (form `region=<CC>`, Bearer) to
  record the account region; it is not required for credential retrieval.

So a client enrolling a fresh device runs 1 → 2 → 3 (gets 204) → prompts the user for the emailed code →
3a → 4. A client reusing a trusted `uuid` goes 1 → 2 → 3 (gets 200) → 4.

**How the official app derives it** (`o/DoubleValueOrBuilder.write(context)`, key `device_imei` in
`shared_prefs/setting.xml`) — despite the name it is **not** an IMEI:

1. `rand` — an 8-digit number: `random4 = Σ (int)(Math.random()·10^k + 1)` for `k = 0..7`, then
   `+= 10^7` if `< 10^7`. Result is in `~[10^7, 2.1·10^7]` (so it starts with `1` or `2`), as a string.
2. `src` — the Wi-Fi MAC (`WifiManager.getConnectionInfo().getMacAddress()`) with `:` removed, **if**
   available and not the `02:00:00:00:00:00` placeholder — **else** a timestamp
   `yyyy:MM:dd:HH:mm:ss` with `:` removed (`yyyyMMddHHmmss`, 14 chars).
3. Interleave: `id = "AN" + src[0:6] + rand[4:8] + src[6:] + rand[0:4]`.
4. **Persist** under `device_imei` with a flag `use_2020jun15_tutk_formula_already`; every later call
   returns the stored value unchanged.

Because the two parts are **interleaved**, the id is not readable left-to-right. The **length tells which
`src` was used**: timestamp `src` (14 chars) → `"AN"` + **22** digits; MAC `src` (12 chars) → `"AN"` + **20**.
Worked example (a synthetic value):
`AN2024083117172059251446` → `src = 20240817205925` (2024-08-17 20:59:25) + `rand = 14463117`.

Since **Android 6** `getMacAddress()` returns `02:00:00:00:00:00` to all apps, the MAC branch is dead on
any current phone, so in practice the id is **`"AN"` + first-run timestamp + `Math.random()` digits, minted
once and persisted**. `Math.random()` is Java's shared **non-cryptographic 48-bit LCG** (seeded once at
first use), so the random part is unpredictable in practice but not crypto-grade; combined with the
second-precision first-run time it is effectively a **one-time value**. Critically it is **not** an IMEI,
MAC, serial, ANDROID\_ID or advertising id, and **not recomputable** from any stable device property — the
only copy is in the app's stored prefs. (If you ever observe an `"AN"`+20-digit id, that device *did* expose
a real MAC and part of the id is MAC-derived — still not something a helper can reproduce without the MAC.)

### Two strategies for a helper

| Strategy | What the helper uses as `uuid` | 2FA | Caveat |
|---|---|---|---|
| **Emulate the existing device** | the phone's stored `device_imei` | **None** — already trusted | Requires extracting `device_imei` from `shared_prefs/setting.xml` on the phone (root / platform backup). Not possible on a stock non-rooted phone. |
| **Enroll a new device** | a freshly minted stable id (same `"AN"+…` shape), persisted by the helper | **One-time** emailed code on first login | First login returns `204`; redeem the emailed code via the `authorize` call above. After the first success the id is trusted and later logins skip 2FA. |

A helper should **mint its id once and persist it** (mirroring the app), not regenerate per run — a new id
each run would re-challenge every time.

**The `uuid` is opaque to the server — don't over-think its format.** The server never validates a `uuid`'s
internal structure, OS, or that the generator produced it; it only remembers which *values* it has already
trusted. So a minted id can be any stable string of the `"AN"+…` shape — the MAC-style form (`AN`+20, with
hex letters, the Android ≤ 5 branch) and the timestamp form (`AN`+22, all digits) are interchangeable as far
as login is concerned. Choosing the MAC/Android-5 form buys **nothing**: it is neither more *deterministic*
(the interleaved `Math.random()` half is still unreproducible, so you can't recompute an existing phone's id
even from its known MAC) nor more *trusted* (a freshly minted value is unknown → 2FA regardless). Skipping
2FA requires reusing the phone's **exact stored** `device_imei`, nothing else.

### Choosing the device id — keyed by the machine, no stored state

The reference helper stores nothing and still uses one id per computer. It computes the id as an HMAC of the
normalised account email, keyed by the computer's own install id: the systemd `machine-id` on Linux, the
`IOPlatformUUID` on macOS, the `MachineGuid` on Windows. The result is laid out in the app's
`"AN"` + 22-digit shape (a valid past timestamp and an in-range `rand`, interleaved as above).

- The same computer and email give the same id, so the emailed code is needed only on the first run.
- The id can't be computed without reading the machine id on that computer. The machine id is used only as
  the HMAC key and is never sent.
- Another computer, or a reinstall, gives a new id and so one more emailed code. Each such id stays on the
  account's trusted-device list.
- Without a usable machine id (none present, or all zeros), the helper uses a random id for that run, and
  every run asks for a code.

An explicit override (`VTECH_UUID`) lets a user supply an id instead: their phone's real `device_imei`
(the no-2FA *emulate* path), or a fixed id of their own that they keep secret.

## Open gaps (not yet captured)

- **First-time pairing** (adding a *new* camera to the account) is **not** an HTTPS endpoint: the official
  app performs it over the vendor **P2P layer** (a Kalay/TUTK connect using the QR security key), gated on
  reaching the physical camera; any cloud bind is behind that connect. A credentials helper therefore
  cannot pair a brand-new camera — the camera must already be on the account. (Confirmed live: the "register
  to your account" action emits no HTTPS and engages the P2P connection manager.)

## Confidence

Steps 1–4, the headers, the token model, the device-list schema and the credential map are **[live]**
(captured from the official app and decoded). The authKey derivation is **[live]** and cross-checked
byte-for-byte against the on-camera authKey. The `device_imei` construction is **[static]** from the
decompiled generator. The 2FA challenge + `authorize` redemption are **[live]** on the wire with the
`Custom`-blob construction **[static]** from the client; only the first-time-pairing cloud step is **open**
(above).
