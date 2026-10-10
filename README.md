# VTech Baby Monitor for Home Assistant

A Home Assistant integration for the **VTech RM5754HD** baby monitor camera. It talks to the camera
directly on your network, with no VTech account and no cloud.

You get one device per camera with:

- live video;
- motion and sound alerts, as events and as binary sensors;
- the room temperature, and the too-hot/too-cold alert with its limits;
- the night-light (red, orange or yellow) and the status LED;
- night vision, resolution, motion sensitivity, sound detection, notification interval and lullaby timer;
- whether the parent unit is on.

Only the RM5754HD has been tested. This is not made by or affiliated with VTech or ThroughTek.

## What you need

- The camera already set up with its parent unit, on the same network segment as Home Assistant.
- Home Assistant on x86_64 Linux (the standard container or OS on a PC). Raspberry Pi and other arm64
  machines aren't supported yet.
- The ThroughTek (TUTK) libraries the camera's protocol needs. They can't be included here, so you
  supply them: the `x86_64` builds of `libIOTCAPIs.so`, `libAVAPIs.so`, `libNebulaAPIs.so` and
  `libTUTKGlobalAPIs.so`. They are in the `lib/` folder of VTech's Android app;
  `vtech-lan-kit/sdk/README.md` has the details.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **VTech Baby Monitor**, and restart Home Assistant.

By hand: copy `custom_components/vtech_baby` into your Home Assistant `config/custom_components/` and
restart.

## Add a camera

1. Copy the four libraries into a folder under your Home Assistant config directory, for example
   `config/vtech_baby/sdk/`.
2. Add the **VTech Baby Monitor (RM5754HD)** integration and enter that folder.
3. On the parent unit, open the camera's **Connect Mobile Phone** QR code. Scan it with any QR reader
   on your phone and paste the text into Home Assistant.

The camera's login changes whenever its Wi-Fi settings change. Home Assistant then asks you to scan the
QR code again.

## Notes

- The integration stops the libraries from contacting ThroughTek's servers: it only talks to the camera
  over the local network.
- While the parent unit is on, it limits the video to 720p.
