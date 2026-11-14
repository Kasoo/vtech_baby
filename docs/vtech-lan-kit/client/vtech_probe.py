#!/usr/bin/env python3
"""vtech_probe.py — does this camera accept these credentials, and over what path?

A connect-only probe for checking whether a VTech camera (any model) is reachable on the LAN and
whether a given credential set is accepted. It runs the documented connect sequence
(`../docs/10-transport-session.md`) and nothing else: it does **not** start video or audio, change
any setting, or make the camera emit light or sound. It is safe to run against a live baby monitor.

It reports, in plain language, what each step's return code means — so a user trying a model other
than the documented one can tell apart "wrong credentials" from "not reachable" from "works".

Inputs (same as the reference client):
  VTECH_LIBDIR   directory with the patched SDK .so set + companion (see ../docs/00-prerequisites.md)
  VTECH_UID VTECH_PWD VTECH_AUTHKEY   the credential set to test   VTECH_ACCOUNT (default "admin")
or pass them on the command line (--uid/--pwd/--authkey/--account), or derive them from a version-5
pairing QR with --qr TEXT.

Run it on a host sharing the camera's L2 LAN segment (the connect is by UID; see
`../docs/00-prerequisites.md`, "Host / networking").
"""
from __future__ import annotations

import argparse
import ctypes as C
import os
import struct
import sys

import vtech_camera_client as vc
import vtech_provision as vp


# IOTC return codes this probe interprets. Others are printed with their raw number.
RC_SUCCESS_MIN = 0            # a session id (>= 0) means the connect resolved
RC_WRONG_AUTHKEY = -68        # IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY
RC_TIMEOUT = -13              # IOTC_ER_TIMEOUT
RC_INVALID = -2               # generic failure / unreachable


def _interpret_connect(rc: int) -> tuple[bool, str]:
    """(ok, explanation) for an IOTC_Connect_ByUIDEx return code."""
    if rc >= RC_SUCCESS_MIN:
        return True, (f"session {rc}: the camera answered and accepted the authKey. The UID resolved "
                      "on the LAN and enhanced-security auth passed.")
    if rc == RC_WRONG_AUTHKEY:
        return False, ("-68 (wrong/absent authKey): the camera is reachable and resolved the UID, but "
                       "it rejected the authKey. The transport is compatible; the credentials are the "
                       "problem — a stale AV password/authKey (re-provision from a fresh QR), or, for a "
                       "model whose QR has no AV password, credentials that have to come from the "
                       "account rather than the QR. See COMPATIBILITY.md.")
    if rc == RC_TIMEOUT:
        return False, ("-13 (timeout): no usable answer in time. Most often the camera is not on this "
                       "host's L2 LAN segment, or is still booting, or the LAN search did not reach it. "
                       "Check the host shares the camera's subnet, that the camera is powered and "
                       "connected, and see COMPATIBILITY.md ('reachability'). Not a credentials result.")
    if rc == RC_INVALID:
        return False, ("-2: the connect failed before reaching the camera (commonly no network path, or "
                       "the master/discovery path unavailable with no LAN answer). Not a credentials "
                       "result; check the host is on the camera's LAN.")
    return False, (f"{rc}: connect failed (see the ThroughTek IOTC error list). This is a transport/"
                   "reachability result, not an AV-credentials one.")


def _interpret_av(rc: int) -> tuple[bool, str]:
    """(ok, explanation) for an avClientStartEx return code."""
    if rc >= 0:
        return True, (f"AV channel {rc}: the AV layer authenticated with the account and AV password. "
                      "The full credential set works on this camera.")
    return False, (f"{rc}: the IOTC session came up but the AV layer refused. The authKey was accepted "
                   "but the AV account/password was not — check VTECH_ACCOUNT (default 'admin') and "
                   "VTECH_PWD. For an untested model the AV account may differ.")


def _session_mode(t: vc.TUTK, ses: int) -> str:
    """LAN / relay / P2P, read from IOTC_Session_Check byte 0 (0 P2P, 1 relay, 2 LAN)."""
    try:
        check = t._fn(t.IO, "IOTC_Session_Check", C.c_int, C.c_int, C.c_void_p)
    except AttributeError:
        return "unknown (IOTC_Session_Check not available)"
    buf = (C.c_ubyte * 256)()
    rc = check(ses, C.cast(buf, C.c_void_p))
    if rc < 0:
        return f"unknown (IOTC_Session_Check rc={rc})"
    return {0: "P2P", 1: "relay (via ThroughTek, not LAN-direct)", 2: "LAN-direct"}.get(
        buf[0], f"mode {buf[0]}")


def probe(libdir: str, uid: str, pwd: str, authkey: str, account: str) -> int:
    """Run the connect-only probe. Returns a process exit code (0 = full success)."""
    print(f"probing UID {uid} (account={account})")
    t = vc.TUTK(libdir)                                   # loads + preflights the SDK libs
    t.set_license(vc._read_license(libdir).encode())
    t.iotc_init(0)
    t.set_search_port(32762)                              # the camera's LAN-search port
    t.av_init(16)
    sid = t.get_sid()

    inbuf = (C.c_ubyte * 200)()
    struct.pack_into("<I", inbuf, 0x00, 160)              # struct size
    struct.pack_into("<I", inbuf, 0x04, 0)               # auth type (password)
    ak = authkey.encode()
    inbuf[0x08:0x08 + len(ak)] = ak                       # authKey (enhanced security)
    struct.pack_into("<I", inbuf, 0x94, 15)              # connect timeout (s)
    ses = t.connect(uid.encode(), sid, C.cast(inbuf, C.c_void_p))
    ok, msg = _interpret_connect(ses)
    print(f"\nIOTC_Connect_ByUIDEx -> {msg}")
    if not ok:
        return 1

    print(f"session path: {_session_mode(t, ses)}")

    ai = vc.AvIn(size=C.sizeof(vc.AvIn), sid=ses, ch=0, tmo=30,
                 acct=account.encode(), pwd=pwd.encode(), resend=1, sec=1, auth=0)
    ao = vc.AvOut(size=C.sizeof(vc.AvOut))
    av = t.av_start(C.byref(ai), C.byref(ao))
    ok, msg = _interpret_av(av)
    print(f"avClientStartEx -> {msg}")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Connect-only credential/reachability probe for a VTech camera")
    ap.add_argument("--uid", default=os.environ.get("VTECH_UID"))
    ap.add_argument("--pwd", default=os.environ.get("VTECH_PWD"))
    ap.add_argument("--authkey", default=os.environ.get("VTECH_AUTHKEY"))
    ap.add_argument("--account", default=os.environ.get("VTECH_ACCOUNT", "admin"))
    ap.add_argument("--qr", help="derive the credential set from a version-5 pairing QR instead")
    ap.add_argument("--libdir", default=os.environ.get("VTECH_LIBDIR"),
                    help="SDK library directory (or set VTECH_LIBDIR)")
    a = ap.parse_args()

    if not a.libdir:
        ap.error("set VTECH_LIBDIR or pass --libdir (the patched SDK .so set; see ../docs/00-prerequisites.md)")

    if a.qr:
        info = vp.inspect_qr(a.qr)
        if not info["provisionable"]:
            print(vp.format_inspection(info))
            print("\nThis QR cannot supply a full credential set, so there is nothing to probe with "
                  "--qr. Supply --uid/--pwd/--authkey directly if you have them.")
            return 3
        creds = vp.provision_from_qr(a.qr)
        a.uid, a.pwd, a.authkey, a.account = creds.uid, creds.av_password, creds.authkey, creds.account

    missing = [n for n in ("uid", "pwd", "authkey") if not getattr(a, n)]
    if missing:
        ap.error("missing credentials: " + ", ".join(f"--{m}/VTECH_{m.upper()}" for m in missing)
                 + " (or pass --qr for a version-5 QR)")

    return probe(a.libdir, a.uid, a.pwd, a.authkey, a.account)


if __name__ == "__main__":
    sys.exit(main())
