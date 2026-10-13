# 70 — Device information

`IOTYPE_USER_IPCAM_DEVINFO_REQ` (`0x330` → `0x331`) returns device identity. It doubles as the required
prep before the VTech vendor GETs will reply (see `20-ioctrl-framework.md`): send it once after the AV
channel is up.

## Request

20 bytes. A 4-byte header `{0, 1, ...}` followed by 16 bytes. A zero-filled 16-byte tail is accepted.

## Response (`0x331`)

56 bytes:

| Offset | Size | Field | Notes |
|---|---|---|---|
| `@0` | 16 | model | NUL-terminated ASCII, e.g. `"RM5754"`. |
| `@16` | 16 | vendor | NUL-terminated ASCII, e.g. `"vtech"`. |
| `@32` | 4 | firmware version | Four version digits, little-endian order: `byte[35].byte[34].byte[33].byte[32]` — e.g. bytes `09 05 02 04` → **`4.2.5.9`**. |
| `@36` | 1 | flag | |
| `@40` | 4 | value | |
| `@44` | 4 | value | |
| `@48` | 8 | flags / handles | |

The model, vendor, and firmware fields correspond to what a management UI would display as Model /
Firmware.
