"""Config flow, setup/unload, pushes, reconnect and reauth, against the fake SDK (HA_INTEGRATION.md §8.1).

The real `Session` runs over `FakeSdk`; only SDK preparation (`_prepare_and_load`) is patched out.
"""
import asyncio
import os
import uuid
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtech_baby import config_flow as cf
from custom_components.vtech_baby import manager as mgr
from custom_components.vtech_baby import native
from custom_components.vtech_baby.const import (
    CONF_ACCOUNT, CONF_APP_FILE, CONF_AUTHKEY, CONF_AV_PASSWORD, CONF_MAC, CONF_QR, CONF_SDK_DIR, CONF_UID, DOMAIN,
)
from custom_components.vtech_baby.native import Prepared
from custom_components.vtech_baby.vtech_lan import provision

from fake_sdk import FakeSdk, i32
from test_extract import apk, bundle
from test_provision import make_qr

UID = "TESTUID0000000000001"
QR = make_qr([(2, b"VTECH_5754_test"), (3, UID.encode()), (5, b"Pw0rdXyz"), (7, bytes.fromhex("0011223344ff"))])
QR_ROTATED = make_qr([(3, UID.encode()), (5, b"NewPw123"), (7, bytes.fromhex("0011223344ff"))])
QR_OTHER = make_qr([(3, b"OTHERUID000000000002"), (5, b"Pw0rdXyz")])
TEMP = "sensor.vtech_5754_test_temperature"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake():
    return FakeSdk()


@pytest.fixture
def sdk():
    """SDK state on the fake host: whether libraries are already there, which dirs fail to load, uploads."""
    return {"present": False, "bad": set(), "installed": []}


REAL_INSTALL = cf._install


def upload(name):
    """The upload id HA's file selector would give the file `name`."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


# Uploads by name: what installing each one does.
UPLOADS = {
    "kalay.xapk": None,
    "arm-only.xapk": native.WrongArch("x86_64", ["arm64-v8a", "armeabi-v7a"]),
    "newer.xapk": native.WrongVersion("4.4.0.0", "4.3.8.1"),
    "photo.jpg": native.NotAPackage("not an APK or app bundle"),
    "other.apk": native.MissingLibs(["libIOTCAPIs.so"]),
    "expired": ValueError("Invalid file_id expired"),
}


@pytest.fixture(autouse=True)
def sdk_patch(fake, sdk):
    def prep(sdk_dir, lib_root):
        if sdk_dir in sdk["bad"]:
            raise mgr.SdkSetupError(f"cannot load {sdk_dir}")
        return mgr.SdkHandle(fake, Prepared(lib_root + "/x86_64", "x86_64", "4.3.8.1", False))

    def install(hass, file_id, sdk_dir):
        if (err := {upload(n): e for n, e in UPLOADS.items()}[file_id]) is not None:
            raise err
        sdk["installed"].append(sdk_dir)
        sdk["present"] = True
        return "4.3.8.1"
    with patch.object(mgr, "_prepare_and_load", prep), patch.object(cf, "_install", install), \
         patch.object(native, "host_supported", lambda: "x86_64"), \
         patch.object(native, "sdk_present", lambda d: sdk["present"]), \
         patch.object(mgr, "RECONNECT_MIN_S", 0.05), patch.object(mgr, "RECONNECT_MAX_S", 0.1):
        yield


def entry_data(qr=QR):
    c = provision.provision(qr)
    return {CONF_SDK_DIR: "/sdk", CONF_UID: c.uid, CONF_ACCOUNT: c.account, CONF_AV_PASSWORD: c.av_password,
            CONF_AUTHKEY: c.authkey, CONF_MAC: c.mac}


async def setup_entry(hass, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="VTECH_5754_test", unique_id=UID, data=entry_data(), **kw)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def unload(hass, entry):
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def wait_until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.05)


# ── Config flow ──────────────────────────────────────────────────────────────
async def test_user_flow_creates_entry(hass: HomeAssistant, sdk):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    assert r["description_placeholders"]["abi"] == "x86_64"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_APP_FILE: upload("kalay.xapk")})
    assert r["step_id"] == "camera"
    sdk_dir = hass.config.path("vtech_baby", "sdk")
    assert sdk["installed"] == [sdk_dir]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: "garbage"})
    assert r["errors"] == {CONF_QR: "invalid_qr"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "VTECH_5754_test"
    assert r["data"] == {**entry_data(), CONF_SDK_DIR: sdk_dir}
    assert r["result"].unique_id == UID
    await hass.async_block_till_done()
    assert hass.states.get(TEMP).state == "21"
    await unload(hass, r["result"])


@pytest.mark.parametrize(("name", "error", "placeholders"), [
    ("arm-only.xapk", "wrong_arch", {"found": "arm64-v8a, armeabi-v7a", "arch": "x86_64"}),
    ("newer.xapk", "wrong_version", {"version": "4.4.0.0", "required": "4.3.8.1"}),
    ("photo.jpg", "not_a_package", {}),
    ("other.apk", "missing_libraries", {"missing": "libIOTCAPIs.so"}),
    ("expired", "sdk_error", {"error": "Invalid file_id expired"}),
])
async def test_upload_errors(hass: HomeAssistant, sdk, name, error, placeholders):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_APP_FILE: upload(name)})
    assert r["step_id"] == "user" and r["errors"] == {"base": error}
    assert placeholders.items() <= r["description_placeholders"].items()
    assert sdk["installed"] == []
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_APP_FILE: upload("kalay.xapk")})
    assert r["step_id"] == "camera"


async def test_real_upload(hass: HomeAssistant, hass_client, tmp_path):
    """Through HA's upload API: the SDK is extracted into the config dir and the upload is deleted."""
    hass.config.config_dir = str(tmp_path / "config")
    assert await async_setup_component(hass, "file_upload", {})
    pkg = bundle(tmp_path / "Kalay Mobile.xapk", {"config.x86_64.apk": apk(["x86_64"])})
    client = await hass_client()
    with open(pkg, "rb") as f:
        resp = await client.post("/api/file_upload", data={"file": f})
    assert resp.status == 200
    file_id = (await resp.json())["file_id"]
    with patch.object(cf, "_install", REAL_INSTALL), patch.object(native._sdk, "host_arch", lambda: "x86_64"):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_APP_FILE: file_id})
    assert r["step_id"] == "camera"
    sdk_dir = str(tmp_path / "config" / "vtech_baby" / "sdk")
    assert sorted(os.listdir(sdk_dir)) == sorted(native._patch.LIBS)
    assert native._patch.sdk_version(sdk_dir) == "4.3.8.1"
    with pytest.raises(ValueError):             # the upload is gone
        await hass.async_add_executor_job(REAL_INSTALL, hass, file_id, sdk_dir)


async def test_untested_model_warns_then_adds(hass: HomeAssistant, fake, sdk):
    fake.camera.model = b"BM9999"
    sdk["present"] = True
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "untested_model"
    assert r["description_placeholders"] == {"model": "BM9999", "tested": "VTech RM5754HD"}
    assert not hass.config_entries.async_entries(DOMAIN)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"] == {**entry_data(), CONF_SDK_DIR: hass.config.path("vtech_baby", "sdk")}
    await hass.async_block_till_done()
    await unload(hass, r["result"])


async def test_sdk_already_uploaded_skips_upload(hass: HomeAssistant, sdk):
    sdk["present"] = True
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["step_id"] == "camera"


async def test_unusable_sdk_asks_for_the_app_again(hass: HomeAssistant, sdk):
    sdk["present"] = True
    sdk["bad"].add(hass.config.path("vtech_baby", "sdk"))
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["step_id"] == "user"


async def test_unsupported_host(hass: HomeAssistant):
    with patch.object(native, "host_supported", lambda: None):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "unsupported_host"
    assert r["description_placeholders"]["machine"]


@pytest.mark.parametrize(("rc", "error"), [(-13, "cannot_connect"), (-68, "invalid_auth"), (-10, "invalid_license")])
async def test_flow_connect_errors(hass: HomeAssistant, fake, rc, error):
    fake.connect_rc = rc
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_APP_FILE: upload("kalay.xapk")})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["errors"] == {"base": error}


async def test_second_camera_skips_sdk_step_and_dedupes(hass: HomeAssistant):
    entry = await setup_entry(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["step_id"] == "camera"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    await unload(hass, entry)


async def test_reauth(hass: HomeAssistant, fake):
    fake.connect_rc = -68              # rotated credentials: setup fails (the fake serves one session at a time)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    fake.connect_rc = 0
    r = await entry.start_reauth_flow(hass)
    assert r["step_id"] == "reauth_confirm"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR_OTHER})
    assert r["errors"] == {CONF_QR: "wrong_camera"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR_ROTATED})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_AV_PASSWORD] == "NewPw123"
    assert entry.data[CONF_AUTHKEY] == provision.derive_authkey("NewPw123")
    assert entry.state is ConfigEntryState.LOADED
    await unload(hass, entry)


# ── Setup and runtime ────────────────────────────────────────────────────────
async def test_setup_offline_retries(hass: HomeAssistant, fake):
    fake.connect_rc = -90
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_wrong_auth_starts_reauth(hass: HomeAssistant, fake):
    fake.connect_rc = -68
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_pushes_update_state(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    assert hass.states.get("sensor.vtech_5754_test_temperature_alert").state == "ok"
    fake.push(0x738, i32(30, 86, 1))
    await wait_until(lambda: hass.states.get(TEMP).state == "30")
    assert hass.states.get("sensor.vtech_5754_test_temperature_alert").state == "too_hot"
    fake.push(0x7AC, i32(0x14, 3))
    await wait_until(lambda: entry.runtime_data.data.params.volume == 3)
    await unload(hass, entry)


async def test_lost_session_goes_unavailable_then_reconnects(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    fake.connect_rc = -90
    fake.dead_rc = -20015                                  # AV_ER_SESSION_CLOSE_BY_REMOTE
    await wait_until(lambda: hass.states.get(TEMP).state == STATE_UNAVAILABLE)
    await wait_until(lambda: entry.runtime_data.failures >= 1)
    fake.dead_rc, fake.connect_rc = None, 0
    await wait_until(lambda: hass.states.get(TEMP).state == "21")
    await unload(hass, entry)


async def test_minus13_trap_starts_reauth_when_camera_on_lan(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    with patch.object(mgr, "STALE_CREDS_FAILURES", 2), patch.object(mgr, "mac_present", lambda mac: True):
        fake.connect_rc = -13
        fake.dead_rc = -20015
        await wait_until(lambda: any(f["context"]["source"] == "reauth"
                                     for f in hass.config_entries.flow.async_progress()))
    assert entry.runtime_data.failures >= 2
    fake.dead_rc, fake.connect_rc = None, 0           # it was only an outage: the prompt is withdrawn
    await wait_until(lambda: hass.states.get(TEMP).state == "21")
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    await unload(hass, entry)


async def test_relay_session_refused_without_reauth(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    with patch.object(mgr, "STALE_CREDS_FAILURES", 2), patch.object(mgr, "mac_present", lambda mac: True):
        fake.mode = 1                                      # the SDK would connect via the relay
        fake.dead_rc = -20015
        await wait_until(lambda: entry.runtime_data.failures >= 3)
        assert hass.states.get(TEMP).state == STATE_UNAVAILABLE
        assert "LAN-direct" in entry.runtime_data.last_error
        assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
        fake.dead_rc, fake.mode = None, 2
        await wait_until(lambda: hass.states.get(TEMP).state == "21")
    await unload(hass, entry)


async def test_minus13_without_camera_on_lan_just_retries(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    with patch.object(mgr, "STALE_CREDS_FAILURES", 1), patch.object(mgr, "mac_present", lambda mac: False):
        fake.connect_rc = -13
        fake.dead_rc = -20015
        await wait_until(lambda: entry.runtime_data.failures >= 3)
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    await unload(hass, entry)


async def test_diagnostics_redacts(hass: HomeAssistant):
    from custom_components.vtech_baby.diagnostics import async_get_config_entry_diagnostics
    entry = await setup_entry(hass)
    d = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(d)
    for secret in (UID, "Pw0rdXyz", entry.data[CONF_AUTHKEY]):
        assert secret not in text
    assert d["connected"] and d["state"]["params"]["temp_c"] == 21
    await unload(hass, entry)


# ── Pure helpers ─────────────────────────────────────────────────────────────
def test_mac_present(tmp_path):
    arp = tmp_path / "arp"
    arp.write_text("IP address  HW type  Flags  HW address  Mask  Device\n"
                   "192.168.1.9  0x1  0x2  00:11:22:33:44:ff  *  eth0\n"
                   "192.168.1.8  0x1  0x0  00:11:22:33:44:ee  *  eth0\n")
    assert mgr.mac_present("00:11:22:33:44:FF", str(arp))
    assert not mgr.mac_present("00:11:22:33:44:ee", str(arp))     # incomplete entry
    assert not mgr.mac_present("", str(arp))
    assert not mgr.mac_present("00:11:22:33:44:ff", str(tmp_path / "missing"))


# ── Phase 3/4 entities ───────────────────────────────────────────────────────
import datetime as dt
import struct

from custom_components.vtech_baby.vtech_lan import ops

P = "vtech_5754_test"


def event_report(code, when=None):
    when = when or dt.datetime.now(dt.UTC)
    return ops.stimeday(when) + struct.pack("<I", int(when.timestamp())) + bytes(4) + i32(code)


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def test_entities_initial_state(hass: HomeAssistant):
    entry = await setup_entry(hass)
    expect = {
        f"light.{P}_night_light": "off", f"select.{P}_night_vision": "auto", f"select.{P}_resolution": "1080p",
        f"select.{P}_motion_sensitivity": "medium", f"select.{P}_alert_interval": "1_min",
        f"select.{P}_lullaby_timer": "30_min", f"switch.{P}_status_led": "on",
        f"switch.{P}_temperature_alerts": "off", f"switch.{P}_sound_detection": "on",
        f"number.{P}_temp_alert_above": "25.0", f"number.{P}_temp_alert_below": "16.0",
        f"binary_sensor.{P}_parent_unit": "on", f"binary_sensor.{P}_motion": "off",
        f"sensor.{P}_sound_sensitivity": "medium", f"sensor.{P}_connection": "lan",
    }
    assert {k: hass.states.get(k).state for k in expect} == expect
    await unload(hass, entry)


async def test_writes_round_trip(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    cam = fake.camera
    await call(hass, "light", "turn_on", f"light.{P}_night_light", color_temp_kelvin=1000)
    assert cam.night_light == 3 and hass.states.get(f"light.{P}_night_light").state == "on"
    await call(hass, "select", "select_option", f"select.{P}_motion_sensitivity", option="high")
    assert cam.motion == [75, 1]                                   # record flag kept
    await call(hass, "select", "select_option", f"select.{P}_resolution", option="720p")
    assert cam.quality == 1 and hass.states.get(f"select.{P}_resolution").state == "720p"
    await call(hass, "select", "select_option", f"select.{P}_alert_interval", option="never")
    assert cam.notify == 6 and hass.states.get(f"select.{P}_alert_interval").state == "never"
    await call(hass, "select", "select_option", f"select.{P}_lullaby_timer", option="non_stop")
    assert cam.params[3] == 0 and cam.params[7] == 1234          # p28 echoed unchanged
    await call(hass, "switch", "turn_off", f"switch.{P}_status_led")
    assert cam.led_off == 1 and hass.states.get(f"switch.{P}_status_led").state == "off"
    await call(hass, "switch", "turn_off", f"switch.{P}_sound_detection")
    assert cam.sound == 0
    await call(hass, "switch", "turn_on", f"switch.{P}_temperature_alerts")
    assert cam.temp_alert == [1, 250, 160]
    await call(hass, "number", "set_value", f"number.{P}_temp_alert_above", value=23.8)
    assert cam.temp_alert == [1, 238, 160]
    assert hass.states.get(f"number.{P}_temp_alert_above").state == "23.8"  # fake: no quantising
    await unload(hass, entry)


async def test_write_failure_raises(hass: HomeAssistant, fake):
    from homeassistant.exceptions import HomeAssistantError
    entry = await setup_entry(hass)
    fake.camera.fail_acks.add(0x7A2)
    with pytest.raises(HomeAssistantError):
        await call(hass, "light", "turn_on", f"light.{P}_night_light", color_temp_kelvin=1000)
    await unload(hass, entry)


async def test_night_light(hass: HomeAssistant, fake):
    """A warm-only colour-temperature light: requests snap to the presets, brightness is fixed."""
    entry = await setup_entry(hass)
    cam, eid = fake.camera, f"light.{P}_night_light"
    attrs = hass.states.get(eid).attributes
    assert attrs["supported_color_modes"] == ["color_temp"]
    assert (attrs["min_color_temp_kelvin"], attrs["max_color_temp_kelvin"]) == (500, 2900)
    assert hass.states.get(f"select.{P}_night_light") is None          # the old select is gone

    await call(hass, "light", "turn_on", eid)                            # first on: yellow
    st = hass.states.get(eid)
    assert cam.night_light == 1 and st.state == "on"
    assert (st.attributes["color_temp_kelvin"], st.attributes["brightness"]) == (2900, 255)
    # The frontend's default presets (500, 1300, 2100, 2900) give red, orange, orange, yellow.
    for kelvin, preset, shown in ((500, 3, 500), (1300, 2, 1700), (2100, 2, 1700), (2900, 1, 2900),
                                  (1000, 3, 500), (2400, 1, 2900), (100, 3, 500), (6500, 1, 2900)):
        await call(hass, "light", "turn_on", eid, color_temp_kelvin=kelvin)
        assert cam.night_light == preset, kelvin
        assert hass.states.get(eid).attributes["color_temp_kelvin"] == shown

    await call(hass, "light", "turn_on", eid, color_temp_kelvin=1700)
    await asyncio.sleep(0.3)                                             # let the fake's 0x7ac pushes land
    await hass.async_block_till_done()
    changes = []
    hass.bus.async_listen("state_changed", lambda e: changes.append(e) if e.data["entity_id"] == eid else None)
    await call(hass, "light", "turn_on", eid, brightness=10)            # can't dim: no change, but a
    await call(hass, "light", "turn_on", eid, color_temp_kelvin=1500)   # new state resets the sliders
    await hass.async_block_till_done()
    assert [e.data["new_state"].attributes["color_temp_kelvin"] for e in changes] == [1700, 1700]
    assert cam.night_light == 2 and hass.states.get(eid).attributes["brightness"] == 255
    await call(hass, "light", "turn_off", eid)
    assert cam.night_light == 0 and hass.states.get(eid).state == "off"
    await call(hass, "light", "turn_on", eid)                            # restores the last colour
    assert cam.night_light == 2

    fake.push(0x7AC, i32(0, 3) + bytes(16))                              # set to red from the handset
    await wait_until(lambda: hass.states.get(eid).attributes.get("color_temp_kelvin") == 500)
    await call(hass, "light", "turn_off", eid)
    await call(hass, "light", "turn_on", eid)
    assert cam.night_light == 3
    await unload(hass, entry)


async def test_old_night_light_select_removed(hass: HomeAssistant):
    from homeassistant.helpers import entity_registry as er
    reg = er.async_get(hass)
    old = reg.async_get_or_create("select", DOMAIN, f"{UID}_night_light")
    entry = await setup_entry(hass)
    assert reg.async_get(old.entity_id) is None
    await unload(hass, entry)


async def test_param_and_state_pushes(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    fake.push(0x7AC, i32(8, 1))                                    # LED disabled from the handset
    await wait_until(lambda: hass.states.get(f"switch.{P}_status_led").state == "off")
    fake.push(0x785, i32(1))
    await wait_until(lambda: hass.states.get(f"binary_sensor.{P}_night_vision_active").state == "on")
    fake.push(0x74F, i32(0))
    await wait_until(lambda: hass.states.get(f"binary_sensor.{P}_parent_unit").state == "off")
    await unload(hass, entry)


async def test_alerts_drive_event_and_auto_off(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    m = entry.runtime_data
    assert m.event_timeout_s() == 75.0                             # interval 1 min + 15 s
    with patch.object(type(m), "event_timeout_s", lambda self: 0.2):
        fake.push(0x1FFF, event_report(0x01))
        await wait_until(lambda: hass.states.get(f"binary_sensor.{P}_motion").state == "on")
        ev = hass.states.get(f"event.{P}_alert")
        assert ev.attributes["event_type"] == "motion" and ev.attributes["self_triggered"] is False
        await wait_until(lambda: hass.states.get(f"binary_sensor.{P}_motion").state == "off")
        fake.push(0x1FFF, event_report(0x16))
        await wait_until(lambda: hass.states.get(f"event.{P}_alert").attributes["event_type"] == "too_hot")
        # Sound while a lullaby plays is self-triggered: event tagged, sound sensor stays off.
        fake.push(0x7AC, i32(1, 5))
        await wait_until(lambda: m.lullaby_playing)
        fake.push(0x1FFF, event_report(0x15))
        await wait_until(lambda: hass.states.get(f"event.{P}_alert").attributes["event_type"] == "sound")
        assert hass.states.get(f"event.{P}_alert").attributes["self_triggered"] is True
        assert hass.states.get(f"binary_sensor.{P}_sound").state == "off"
    await unload(hass, entry)


async def test_get_motion_events_service(hass: HomeAssistant, fake):
    from test_ops import event_page
    entry = await setup_entry(hass)
    t = dt.datetime(2026, 10, 5, 6, 0, 0, tzinfo=dt.UTC)
    fake.camera.event_pages = [event_page(1, 0, 1, [t])]
    r = await hass.services.async_call(DOMAIN, "get_motion_events", {"config_entry_id": entry.entry_id},
                                       blocking=True, return_response=True)
    assert r == {"events": [{"time": "2026-10-05T06:00:00+00:00", "event": 4, "status": 2}]}
    await unload(hass, entry)


async def test_reread_keeps_values_and_gives_up_on_stall(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    m = entry.runtime_data
    m.session.request_timeout_s = 0.2
    fake.camera.silent |= {0x733, 0x7A0, 0x709, 0x713, 0x322}        # camera stops answering
    await m.async_refresh()
    assert hass.states.get(TEMP).state == "21"                        # kept, not unknown
    assert hass.states.get(f"select.{P}_resolution").state == "1080p"
    assert fake.sent_codes().count(0x7A0) == 2                        # setup read + one; then gave up
    assert 0x709 not in fake.sent_codes()[-3:]
    await unload(hass, entry)


# ── Phase 5: camera ──────────────────────────────────────────────────────────
CAM = f"camera.{P}_live_view"


def camera_entity(hass):
    return hass.data["camera"].get_entity(CAM)


async def test_camera_entity_and_snapshot(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from fake_sdk import clip, enable_live_view
    if not clip():
        pytest.skip("needs ffmpeg for the test clip")
    from homeassistant.components.camera import CameraEntityFeature, async_get_image

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    st = hass.states.get(CAM)
    assert st.state == "idle"
    assert st.attributes["supported_features"] == CameraEntityFeature.STREAM
    entry.runtime_data.media._grace_s = 0.2
    image = await async_get_image(hass, CAM)
    assert image.content_type == "image/jpeg" and image.content[:2] == b"\xff\xd8"
    await wait_until(lambda: entry.runtime_data.media._thread is None)
    assert not fake.camera.streaming                              # started for the snapshot, stopped after
    await unload(hass, entry)


async def test_camera_stream_over_loopback(hass: HomeAssistant, fake, socket_enabled):
    av = pytest.importorskip("av")
    import io

    import aiohttp
    from fake_sdk import clip, enable_live_view
    from custom_components.vtech_baby.media_server import DATA_SERVER
    if not clip():
        pytest.skip("needs ffmpeg for the test clip")

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    media = entry.runtime_data.media
    media._grace_s = 0.2
    url = await camera_entity(hass).stream_source()
    server = hass.data[DATA_SERVER]
    assert url == f"http://127.0.0.1:{server.port}/{server.token}/{UID}.ts"
    async with aiohttp.ClientSession() as http:
        async with http.get(url.replace(server.token, "wrong")) as r:
            assert r.status == 404
        data = b""
        async with http.get(url) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "video/mp2t"
            async for chunk in r.content.iter_chunked(65536):
                data += chunk
                if len(data) > 60_000:
                    break
            await wait_until(lambda: hass.states.get(CAM).state == "streaming")
    with av.open(io.BytesIO(data), format="mpegts") as c:
        assert sorted(s.type for s in c.streams) == ["audio", "video"]
    await wait_until(lambda: media._thread is None)                 # client gone → stopped after the grace
    assert not fake.camera.streaming and media.subscribers == 0
    await wait_until(lambda: hass.states.get(CAM).state == "idle")
    port = server.port
    await hass.config_entries.async_reload(entry.entry_id)          # reload keeps the same URL
    await hass.async_block_till_done()
    assert await camera_entity(hass).stream_source() == url and server.port == port
    await unload(hass, entry)
    async with aiohttp.ClientSession() as http:                     # unloaded camera: 404, server stays
        async with http.get(url) as r:
            assert r.status == 404
    await server.async_stop()


async def test_camera_without_session_has_no_source(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    fake.dead_rc = -20015
    await wait_until(lambda: hass.states.get(CAM).state == STATE_UNAVAILABLE)
    assert await camera_entity(hass).stream_source() is None
    fake.dead_rc = None
    await unload(hass, entry)


async def test_reload_while_streaming_is_prompt(hass: HomeAssistant, fake, socket_enabled):
    """A reload with a client mid-stream must not wait on that client (it hung ~2 min, 2026-10-06)."""
    pytest.importorskip("av")
    import time

    import aiohttp
    from fake_sdk import clip, enable_live_view
    if not clip():
        pytest.skip("needs ffmpeg for the test clip")
    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    url = await camera_entity(hass).stream_source()
    got = asyncio.Event()

    async def client():
        async with aiohttp.ClientSession() as http, http.get(url) as r:
            async for _chunk in r.content.iter_chunked(65536):
                got.set()
    task = asyncio.create_task(client())
    await asyncio.wait_for(got.wait(), 10)
    t = time.monotonic()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert time.monotonic() - t < 10
    await asyncio.wait_for(task, 10)                                 # the old stream ended for the client
    assert await camera_entity(hass).stream_source() == url
    await unload(hass, entry)
    from custom_components.vtech_baby.media_server import DATA_SERVER
    await hass.data[DATA_SERVER].async_stop()


async def test_parent_unit_rereads_resolution_and_blocks_1080p(hass: HomeAssistant, fake):
    """The handset caps the camera to 720p and lifts it, live but unpushed: the select follows by
    re-reading after each parent-unit change, and refuses 1080p while the handset is on (kit 40)."""
    from homeassistant.exceptions import HomeAssistantError
    sel, cam = f"select.{P}_resolution", fake.camera
    with patch.object(mgr, "QUALITY_REREAD_S", (0.05, 0.05)):
        entry = await setup_entry(hass)
        fake.push(0x74F, i32(0))                                   # handset off (the fake starts on)
        await wait_until(lambda: hass.states.get(f"binary_sensor.{P}_parent_unit").state == "off")
        cam.quality = 1                                            # handset on: the camera caps itself
        fake.push(0x74F, i32(1))
        await wait_until(lambda: hass.states.get(sel).state == "720p")
        sent = len(fake.sent)
        with pytest.raises(HomeAssistantError, match="parent unit is on"):
            await call(hass, "select", "select_option", sel, option="1080p")
        assert 0x320 not in [c for c, _ in fake.sent[sent:]]       # nothing reached the camera
        cam.quality = 2                                            # handset off: the camera restores 1080p
        fake.push(0x74F, i32(0))
        await wait_until(lambda: hass.states.get(sel).state == "1080p")
        await unload(hass, entry)
