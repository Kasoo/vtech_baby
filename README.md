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

Only the RM5754HD has been tested.

## What you need

- The camera already set up with its parent unit, on the same network segment as Home Assistant.
- Home Assistant on x86_64 Linux (the standard container or OS on a PC). Raspberry Pi and other arm64
  machines aren't supported yet.
- ThroughTek's **Kalay Mobile** app for Android, as a file. The camera's protocol needs ThroughTek's
  SDK libraries, which can't be included here, and this app contains them. You don't install the app
  anywhere: you upload the file to Home Assistant, which keeps only the four libraries it needs.

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
  supported. Other app versions may carry a different SDK version, which won't work: the integration
  needs SDK version 4.3.8.1, and checks the libraries' version and architecture when you upload them.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **VTech Baby Monitor**, and restart Home Assistant.

By hand: copy `custom_components/vtech_baby` into your Home Assistant `config/custom_components/` and
restart.

## Add a camera

1. Add the **VTech Baby Monitor (RM5754HD)** integration and upload the Kalay Mobile `.xapk` file.
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
