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
- night vision, resolution, motion sensitivity, sound detection, notification interval and lullaby timer;
- a speaker for the camera's lullabies, its volume, and text-to-speech or other media.

![The camera's device page in Home Assistant](https://raw.githubusercontent.com/Kasoo/vtech_baby/refs/heads/main/images/device.png)

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

The integration includes its own implementation of the camera's network libraries (ThroughTek's Kalay
SDK), for x86_64 and arm64 on both Home Assistant OS/Container (musl) and other Linux installs (glibc
2.17 or later, with OpenSSL 3). Nothing needs downloading.

On a host where it doesn't recognise the C library, setup instead asks for ThroughTek's **Kalay Mobile**
app for Android (`com.tutk.kalaymobile`, version `2.0.0`, the `x86_64` or `arm64-v8a` variant), whose SDK
libraries (version `4.3.8.1`) it extracts and adapts.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **VTech Baby Monitor**, and restart Home Assistant.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Kasoo&repository=vtech_baby&category=integration)

By hand: copy `custom_components/vtech_baby` into your Home Assistant `config/custom_components/` and
restart.

## Add a camera

1. Add the **VTech Baby Monitor** integration.
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
