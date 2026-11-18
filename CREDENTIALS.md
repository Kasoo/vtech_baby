# Getting a camera's credentials

The integration needs two things per camera: its **UID** (20 letters and digits) and its **AV password**.
There are three ways to give them, in order of preference.

## 1. The pairing QR (recommended)

On the parent unit, open the camera's **Connect Mobile Phone** QR code, scan it with any QR reader on your
phone, and paste the text into the setup dialogue (**Paste the pairing QR text**). Everything comes from
the QR, with no account and no internet. A QR that holds several cameras offers a choice of camera.

## 2. Typed UID and password

Choose **Type the camera's UID and password** when you add the camera, or later from the integration's
**Reconfigure** (or the re-pair prompt). The integration works out the camera's security mode itself.
Use this when you have the credentials from somewhere else, such as the helper below.

## 3. From your VTech cloud account (helper tool)

If the QR doesn't work for you and your cameras are set up in the **MyVTech Baby Pro** app, the helper
[`tools/vtech_cloud_credentials.py`](tools/vtech_cloud_credentials.py) logs in to your VTech account, reads
its list of cameras, and prints each one's UID and AV password. Then add the camera with option 2.

```
python3 tools/vtech_cloud_credentials.py --email you@example.com
```

It needs Python 3.10 or later and nothing else. Run it on any computer; it doesn't need to be on the
camera's network. It asks for your account password (or reads `VTECH_PASSWORD`), and writes nothing to
disk. `--format env` or `--format json` changes the output; `--trace` shows each request with the secrets
hidden.

**Before you use it**, know what it does to your account:

- **It adds a trusted device to your VTech account.** The first time, VTech emails you a verification code
  (two-factor sign-in), and the helper asks for it. After that the helper is one of your account's trusted
  devices, named `creds-helper`. Run it from a terminal the first time, so it can ask for the code.
- **The trusted device is tied to this computer.** The helper works out its device id from this computer's
  own machine id and your email, and stores nothing. Later runs on the same computer don't need a code;
  another computer, or a reinstall, asks for one again and adds another trusted device. Nobody can work out
  the id from your email alone, so the code still protects your account against someone who only has your
  password. If the computer has no machine id, the helper says so and asks for a code on every run.
- It sends your email and password to VTech's servers, as the app does, and nowhere else.
- It changes nothing else on the account, and nothing on the cameras.
- It relies on how the app talks to VTech's cloud today; a change on VTech's side can break it.
- The integration itself never uses the cloud. This helper is a separate tool you run once, by hand.

The cloud doesn't say which security mode a camera uses; the integration finds out when it connects. If a
camera's credentials change later (a Wi-Fi change or "Update security key"), run the helper again and use
**Reconfigure**.
