# VTech Baby Monitor for Home Assistant

An **unofficial** Home Assistant integration for the **VTech RM5754HD** baby monitor camera. It is not
made, endorsed or supported by VTech or ThroughTek, and is not affiliated with either. VTech,
ThroughTek, TUTK, Kalay and the product names here are trademarks of their owners, used only to say
which camera this works with. The icon is original artwork, not VTech's.

It talks to the camera directly on your network, with no VTech account and no cloud.
You get one device per camera with:

- live video;
- motion and sound alerts, as events and as binary sensors;
- the room temperature, and the too-hot/too-cold alert with its limits;
- the night-light (red, orange or yellow) and the status LED;
- night vision, resolution, motion sensitivity, sound detection, notification interval and lullaby timer;
- whether the parent unit is on.

**Only the RM5754HD has been tested.** VTech sells many baby monitors, and most of them aren't built on
the same platform: another model may not work at all, or only partly. If the camera reports a different
model when you add it, Home Assistant warns you before adding it anyway. If you try another model,
please report how it went.

## What you need

- The camera already set up with its parent unit, on the same network segment as Home Assistant.
- Home Assistant on x86_64 Linux (the standard container or OS on a PC). Raspberry Pi and other arm64
  machines aren't supported yet.
- ThroughTek's SDK libraries (below). They can't be included here, so you upload them to Home
  Assistant when you add the first camera. The easiest source is ThroughTek's **Kalay Mobile** app for
  Android: you don't install it anywhere, you upload the downloaded file and Home Assistant keeps only the
  four libraries.

  Download exactly this from [APKPure](https://apkpure.com/kalay-mobile/com.tutk.kalaymobile/download):

  | | |
  |---|---|
  | App | Kalay Mobile, by ThroughTek Co., Ltd. (`com.tutk.kalaymobile`) |
  | Version | 2.0.0 (version code 87) |
  | Variant | The one for your Home Assistant host's processor, as an XAPK file (see below) |

  | Home Assistant runs on | Variant |
  |---|---|
  | A PC, NUC or other x86_64 machine | **x86_64** |
  | A Raspberry Pi 4 or 5, or another 64-bit ARM machine | **arm64-v8a** (not supported yet, see above) |

  The setup dialog names the variant your host needs. 32-bit systems (armeabi-v7a, x86) can't be
  supported. Other app versions may carry a different SDK version, which won't work.

### The SDK libraries

If you have the libraries from somewhere else, put these four files in a zip (in any folder, or inside
one zip within it) and upload that instead:

- `libTUTKGlobalAPIs.so`
- `libNebulaAPIs.so`
- `libIOTCAPIs.so`
- `libAVAPIs.so`

All four must be:

- ThroughTek Kalay SDK **version 4.3.8.1**. Each library contains its version string; in the tested
  builds that is `4.3.8.1-1-gc5f0edc-R2_openssl_android`. Only the `4.3.8.1` is checked, so a different
  build suffix is fine;
- the **Android** build (the ones in Android apps' `lib/` folders);
- for your host: **x86_64** (`lib/x86_64/`), or **arm64-v8a** (`lib/arm64-v8a/`) for 64-bit ARM.

The upload is checked for exactly this (file names, ELF shared objects, architecture and version), and
anything else in it is ignored. These are the SHA-256 digests of the builds tested, which Kalay Mobile
2.0.0 carries:

| File | x86_64 | arm64-v8a |
|---|---|---|
| `libTUTKGlobalAPIs.so` | `de59ccf3d543ac45211624bf63a7b98f9f1753ed95ecb27e8334a95bca8e3feb` | `0f367351372eafd5c4663226d6098e517d5cba34797a8e5e0d65e99b0003b1ad` |
| `libNebulaAPIs.so` | `d538aca357d501266362244a4a1444f07819fec09b003218f23e92620db098a7` | `a029e3935bf207379a31391c9c748cc6bcbaf31cc8b336a86408d845d7628aef` |
| `libIOTCAPIs.so` | `0ebf29b441d9bf52154ea5b9756391da8944c6c7f708ea8443681be2a078b193` | `6b4a2c3dd2ffc25617af837777c8360ee235d5b744775c446fda77a221e4fc02` |
| `libAVAPIs.so` | `43c69cf873e6aa462ac43f7095cc8a1d0edbf96e91fad17760e7bc7db1dc2b90` | `a1f8c268d8ae4ca7e75917841aa67332f13ea30e61ba94d9eb889acf27c94589` |

Other builds of version 4.3.8.1 should work too, but haven't been tested.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **VTech Baby Monitor**, and restart Home Assistant.

By hand: copy `custom_components/vtech_baby` into your Home Assistant `config/custom_components/` and
restart.

## Add a camera

1. Add the **VTech Baby Monitor** integration and upload the Kalay Mobile `.xapk` file (or
   your zip of the SDK libraries).
   This is only needed once: further cameras, and reinstalls on the same Home Assistant, reuse the
   libraries already extracted (into `config/vtech_baby/sdk/`).
2. On the parent unit, open the camera's **Connect Mobile Phone** QR code. Scan it with any QR reader
   on your phone and paste the text into Home Assistant.

The camera's login changes whenever its Wi-Fi settings change. Home Assistant then asks you to scan the
QR code again.

## Notes

- The integration stops the libraries from contacting ThroughTek's servers: it only talks to the camera
  over the local network.
- While the parent unit is on, it limits the video to 720p.
