# ThroughTek/TUTK Kalay SDK libraries

The camera's LAN transport is ThroughTek's Kalay P2P SDK (TUTK). This kit drives the stock SDK shared
libraries directly. The SDK itself is not modified beyond the load-time ELF patch in `../tooling/`,
which only makes the Android/bionic ELFs loadable against a host libc and does not change SDK behaviour.

## We do not distribute the SDK binaries

The ThroughTek/TUTK Kalay SDK `.so` libraries are ThroughTek's compiled property. This kit does not ship,
bundle, download or redistribute them, and neither may anything built on it (a Home Assistant integration
included) — they are an **input the user provides**. `../docs/85-ha-deployment.md` describes how an
integration accepts them and prepares them at runtime.

The kit *may* point the user at a **public source** to obtain them: ThroughTek's own **Kalay Mobile** app
(`com.tutk.kalaymobile`, version 2.0.0, version code 87, which carries SDK 4.3.8.1), from a public app
mirror such as APKPure. The libraries are in the APK's `lib/<abi>/` (`x86_64`, `arm64-v8a`), which in a
split download (`.xapk`) is the ABI split, e.g. `config.x86_64.apk`. `../tooling/extract_sdk.py` takes
the four libraries out of such a download, or out of any zip of them. We only reference the source; we never host or re-serve the
binaries ourselves.

> The SDK **license key** is a different matter: it is a small static value (a pure numeric fact,
> shareable under interoperability exceptions), so it is **embedded in the kit**, not user-supplied. See
> `../docs/00-prerequisites.md`.

## What's here: local development copies only

`x86_64/` and `arm64-v8a/` each hold the four SDK libraries of the build this kit is characterised
against. They're provided **only so the kit can be developed and tested locally**. They are **not** part of
any distributable artifact and must not be shipped, committed to a public repository, or bundled into an
integration.

| File | Role |
|---|---|
| `libIOTCAPIs.so` | IOTC layer: session establishment (`IOTC_Initialize2`, `IOTC_Connect_ByUIDEx`, session id). |
| `libAVAPIs.so` | AV layer: authenticated AV channel, `avSendIOCtrl` / `avRecvIOCtrl`, frame/audio receive. |
| `libNebulaAPIs.so` | Nebula layer (sibling dependency). |
| `libTUTKGlobalAPIs.so` | Global/init layer, incl. `TUTK_SDK_Set_License_Key`. |

## Architecture selection

Use the set that matches the host: x86_64 libraries for an x86_64 host, arm64-v8a (aarch64) libraries for
an aarch64 host. The tooling (`patch.sh`, `patch_sdk.py`) reads the architecture from the libraries' own
ELF headers and patches accordingly, so it doesn't need to be told. Both architectures of the same SDK
build behave identically at the protocol level.

## Tested SDK build

The kit is characterised against Kalay SDK **4.3.8.1**, and `../tooling/patch_sdk.py` (and
`extract_sdk.py`) refuse any other version. The version comes from the string each library embeds,
`4.3.8.1-1-gc5f0edc-R2_openssl_android`; all four must agree. A version check rather than a hash check,
because struct layouts change between SDK versions (4.3.6.2 → 4.3.8.1 changed the `IOTC_Connect_ByUIDEx`
input) but builds of one version are expected to be interchangeable. For reference, the SHA-256 digests
of the libraries tested (Kalay Mobile 2.0.0; the APKPure arm64-v8a download is byte-identical):

| File | x86_64 | aarch64 (arm64-v8a) |
|---|---|---|
| `libTUTKGlobalAPIs.so` | `de59ccf3d543ac45211624bf63a7b98f9f1753ed95ecb27e8334a95bca8e3feb` | `0f367351372eafd5c4663226d6098e517d5cba34797a8e5e0d65e99b0003b1ad` |
| `libNebulaAPIs.so` | `d538aca357d501266362244a4a1444f07819fec09b003218f23e92620db098a7` | `a029e3935bf207379a31391c9c748cc6bcbaf31cc8b336a86408d845d7628aef` |
| `libIOTCAPIs.so` | `0ebf29b441d9bf52154ea5b9756391da8944c6c7f708ea8443681be2a078b193` | `6b4a2c3dd2ffc25617af837777c8360ee235d5b744775c446fda77a221e4fc02` |
| `libAVAPIs.so` | `43c69cf873e6aa462ac43f7095cc8a1d0edbf96e91fad17760e7bc7db1dc2b90` | `a1f8c268d8ae4ca7e75917841aa67332f13ea30e61ba94d9eb889acf27c94589` |

The opcodes this kit documents are implemented camera-side, so other Kalay 4.3.x builds are expected to
behave the same at the protocol level. Their ABI surface, however, can differ, which is why other
versions are refused rather than tried (`--any-version` on the tooling CLIs, for development only).

The APKs also carry `...T.so` builds of the same libraries (identical exports, plus logcat logging via
`liblog.so`). They are not used; the tooling takes the four plain libraries by exact name.
