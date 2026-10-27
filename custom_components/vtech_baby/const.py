"""Constants for the vtech_baby integration."""
from __future__ import annotations

DOMAIN = "vtech_baby"

# Config-entry data
CONF_UID = "uid"
CONF_ACCOUNT = "account"
CONF_AV_PASSWORD = "av_password"
CONF_AUTHKEY = "authkey"
CONF_MAC = "mac"
CONF_QR = "qr"                     # form field only; the QR text itself is never stored

# Version 1 entries ran on the vendor's SDK: the entry named its directory, and the HA config directory
# held the libraries extracted from the user's upload and their patched copies. The migration to
# version 2 removes all three.
CONF_SDK_DIR = "sdk_dir"
SDK_DIRS = (("vtech_baby", "sdk"), ("vtech_baby", "lib"))

# Session tuning. 20 s keepalive is the interval tested over an idle hour.
KEEPALIVE_S = 20.0
RECONNECT_MIN_S = 10.0
RECONNECT_MAX_S = 300.0
# The -13 trap (AGENTS.md, "Design notes"): after this many consecutive "unreachable" connects while
# the camera's MAC is present on the LAN, assume rotated credentials and ask for a fresh QR. The threshold is a guess.
STALE_CREDS_FAILURES = 4

# DEVINFO models this integration has been tested with: the RM5754HD reports "RM5754". Setup
# warns about any other (config_flow, step "untested_model").
TESTED_MODELS = frozenset({"RM5754"})
TESTED_PRODUCT = "VTech RM5754HD"

# Repair issue ids
ISSUE_RESTART_REQUIRED = "restart_required"   # no longer raised; deleted at setup
ISSUE_WORKER_CRASHED = "worker_crashed"

SIGNAL_EVENT = "vtech_baby_event_{}"   # .format(entry_id); payload: (ops.EventReport, self_triggered)

# Notification-interval index -> seconds; index 0 ("default") is unmeasured, 6 = never.
NOTIFY_INTERVAL_S = {1: 60.0, 2: 180.0, 3: 300.0, 4: 600.0, 5: 1800.0}
SELF_TRIGGER_S = 10.0                  # sound events this long after a lullaby or talk-back are self-triggered
# The parent handset caps the camera to 720p and lifts the cap within seconds, live, but resolution
# isn't pushed (kit 40): re-read it this long after each parent-unit change (cumulative waits).
QUALITY_REREAD_S = (3.0, 7.0)
