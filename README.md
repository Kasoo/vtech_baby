# VTech Baby Monitor for Home Assistant

An **unofficial** Home Assistant integration for the **VTech RM5754HD** baby monitor camera.
It is not developed, endorsed or supported by VTech or ThroughTek, and is not affiliated with either.
VTech, ThroughTek, TUTK, Kalay and the product names here are trademarks of their owners, and are used
only to identify compatibility.

This integration communicates with the camera directly over your local network, with no VTech account
required and no cloud communication.

You get one device per camera with:

- live video;
- motion and sound alerts, as events and as binary sensors;
- the room temperature, and the too-hot/too-cold alert with its limits;
- the night-light (red, orange or yellow) and the status LED;
- night vision, resolution, motion sensitivity, sound detection, notification interval and lullaby timer.

**NOTE: Only the RM5754HD has been tested.** VTech sells many baby monitors. Although most are likely
built on the same platform, other models are likely to require different communication opcodes or
encryption secrets, and may not work at all, or only partly.

When you add a camera, the integration detects its model, and warns you if it's an untested model
before you choose whether to add it anyway.

If you try another model, please report how it went.

## What you need

- The camera already set up with its parent unit, on the same network broadcast domain as Home
  Assistant.
- Home Assistant running on x86_64 or arm64 Linux.
- ThroughTek's SDK libraries (see below). They can't be included in the integration, so you must upload
  them when you add the first camera.

### The SDK libraries

The easiest source is ThroughTek's **Kalay Mobile** app for Android.

You don't need to install it anywhere: obtain the APK and upload it to the integration during setup.
This might be found on APK download sites such as APKPure.

|              |                                                                               |
|--------------|-------------------------------------------------------------------------------|
| App          | Kalay Mobile, by ThroughTek Co., Ltd.                                         |
| Package Name | `com.tutk.kalaymobile`                                                        |
| Version      | `2.0.0` (versionCode `87`)                                                    |
| Variant      | `x86_64` or `arm64-v8a` (matching your Home Assistant's architecture)         |

You can upload the `config.x86_64` or `config.arm64_v8a` split APK, the entire `xapk`/`apks` bundle,
or a zip of the four SDK libraries.

The libraries must be SDK version `4.3.8.1` (the Android builds; the build suffix doesn't matter).
These are the ones tested:

| File                   | `x86_64`                                                           | `arm64-v8a`                                                        |
|------------------------|--------------------------------------------------------------------|--------------------------------------------------------------------|
| `libTUTKGlobalAPIs.so` | `de59ccf3d543ac45211624bf63a7b98f9f1753ed95ecb27e8334a95bca8e3feb` | `0f367351372eafd5c4663226d6098e517d5cba34797a8e5e0d65e99b0003b1ad` |
| `libNebulaAPIs.so`     | `d538aca357d501266362244a4a1444f07819fec09b003218f23e92620db098a7` | `a029e3935bf207379a31391c9c748cc6bcbaf31cc8b336a86408d845d7628aef` |
| `libIOTCAPIs.so`       | `0ebf29b441d9bf52154ea5b9756391da8944c6c7f708ea8443681be2a078b193` | `6b4a2c3dd2ffc25617af837777c8360ee235d5b744775c446fda77a221e4fc02` |
| `libAVAPIs.so`         | `43c69cf873e6aa462ac43f7095cc8a1d0edbf96e91fad17760e7bc7db1dc2b90` | `a1f8c268d8ae4ca7e75917841aa67332f13ea30e61ba94d9eb889acf27c94589` |

Other builds of version `4.3.8.1` should work too, but haven't been tested.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **VTech Baby Monitor**, and restart Home Assistant.

By hand: copy `custom_components/vtech_baby` into your Home Assistant `config/custom_components/` and
restart.

## Add a camera

1. Add the **VTech Baby Monitor** integration and upload the Kalay Mobile `.xapk` file.
   This is only needed once: further cameras, and reinstalls on the same Home Assistant, reuse the
   libraries already extracted.
2. On the parent unit, open the camera's **Connect Mobile Phone** QR code. Scan it with any QR reader
   on your phone and paste the text into the setup dialogue.

The camera's credentials may change when its Wi-Fi settings change or when the "Update security key"
option is used. Home Assistant then can't connect, and asks you to re-pair the camera: it shows
**Authentication expired for** *camera name* in **Settings → Repairs**, and the integration has a
**Reconfigure** button in **Settings → Devices & services**. Select it, scan the camera's current QR code
on the parent unit, and paste the text in.

## Notes

- The integration stops the libraries from contacting any internet servers: it only talks to the camera
  over the local network.
- While the parent unit is on, video quality is limited to 720p.
