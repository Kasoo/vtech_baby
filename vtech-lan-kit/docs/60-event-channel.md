# 60 — Event channel (alerts and stored events)

The camera reports motion/sound alerts two ways: an asynchronous **push** for live alerts, and a
**query** for the stored recent-event list.

## Asynchronous alert push — `0x1fff`

`IOTYPE_USER_IPCAM_EVENT_REPORT` (`0x1fff`) is a camera-initiated push (no request) that arrives on the
AV channel whenever a detection event fires. It is 24 bytes:

```
[ datetime : 8 ][ unix_ts_LE : 4 ][ channel : 4 ][ event_code : 4 ][ reserved : 4 ]
```

| Field | Meaning |
|---|---|
| `datetime@0` (8B) | Packed local time: `year` (2B LE), `month`, `day`, `weekday`, `hour`, `minute`, `second`. |
| `unix_ts@8` (4B LE) | Event timestamp (Unix seconds). |
| `channel@12` (4B LE) | Channel, always `0`. |
| `event_code@16` (4B LE) | Event type: **`0x01` = motion, `0x15` = sound, `0x16` = temperature too high, `0x17` = temperature too low.** Sound events fire only while sound detection is enabled (`0x7b2`); temperature events only while temperature detection is enabled (`0x74d`). |
| `reserved@20` (4B) | Reserved; ignore it. It is `0` for motion and sound, and holds a fixed non-zero value for temperature events. |

Notes:
- Event pushes are **rate-limited by the notification interval** (`0x790`/`0x792`); with a 1-minute
  interval the camera emits at most roughly one event per minute, so a burst of activity yields a single
  push. A change to a shorter interval loosens this.
- A temperature event fires within a few seconds of the reading crossing a threshold, and repeats about
  every 100 s while it stays out of range.
- The push arrives interleaved with other IOCtrl traffic; dispatch on `ioType == 0x1fff` in the receive
  loop.

## Stored recent-event list — `0x788` / `0x789`

`IOTYPE_VTECH_IPCAM_LISTEVENT_REQ/RESP` (`0x788` → `0x789`) returns the recent-event history.

Both directions use an 8-byte **`STimeDay`**, always in **UTC**:

```
[ year : 2 LE ][ month 1-12 ][ day ][ wday ][ hour ][ minute ][ second ]
```

**Request `0x788`** (24 bytes):

```
[ channel : 4 = 0 ][ start : STimeDay ][ end : STimeDay ][ event : 1 = 0 (all) ][ status : 1 = 0 ][ 0 : 2 ]
```

In a request, `wday` is 1 = Sunday … 7 = Saturday. A range of one day is typical.

**Response `0x789`** is a sequence of pages, each at most 252 bytes: a 12-byte header, then `count`
**12-byte records**.

```
header: [ channel : 4 ][ total : 4 LE ][ index : 1 ][ endflag : 1 ][ count : 1 ][ - : 1 ]
record: [ time : STimeDay ][ event : 1 ][ status : 1 ][ - : 1 ][ - : 1 ]
```

| Field | Meaning |
|---|---|
| `total` | Records in the whole range, repeated on every page. |
| `index` | Page number, from 0. |
| `endflag` | `1` on the last page. Stop reading after it (a page with `count` = 0 also ends the list). |
| `count` | Records on this page (up to 20). |
| record `time` | Event time in UTC. For a motion event it equals the `0x1fff` push's `unix_ts` to the second. |
| record `event` | Stored-event type. **`4` = motion** (`1` is also treated as motion). |
| record `status` | `2` on the camera's motion records. |

In records, the `wday` byte and the bytes marked `-` carry no meaning: ignore them. They are not always
zero.

**Order:** records come back **newest first**, across pages (a 24 h query on 2026-09-23 returned 173
motion records, 12:14 down to 17:03 UTC the previous day).

**Mapping to the `0x1fff` push:** a motion push (`event_code` `0x01`) produces a stored record with
`event` = `4` at the same second. **Only motion is stored.** Sound (`0x15`) and temperature (`0x16`/`0x17`)
events arrive only as the live push, so an integration that wants a history of them must record the
pushes itself.

> **Out of scope:** retrieving or playing back the stored event *clip* (SD-card recording) is not
> characterised here — only the live push and the event *list* are.

## Related pushes

- `0x738` — temperature notify (°C/°F, plus the temperature state `0` in range / `1` too hot / `2` too cold; see `30`), pushed periodically.
- `0x785` — day/night state notify (see `30`).
- `0x7ac` — night-light / status-LED state-change notify (see `30`).
- `0x74f` — parent-unit active-state notify (see `30`).
