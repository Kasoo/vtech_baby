"""Config flow, setup/unload, pushes, reconnect and reauth, against the fake link.

The real `LinkSession` runs over `FakeLinkModule`; only module preparation (`_prepare_and_load`) is patched out.
"""
import asyncio
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtech_baby import manager as mgr
from custom_components.vtech_baby import native
from custom_components.vtech_baby.const import (
    CONF_ACCOUNT, CONF_AUTHKEY, CONF_AV_PASSWORD, CONF_MAC, CONF_QR, CONF_SDK_DIR, CONF_UID, DOMAIN,
)
from custom_components.vtech_baby.vtech_lan import provision

from fake_camera import i32
from fake_link import FakeLinkModule, LinkError, thread_client
from test_provision import make_qr

UID = "TESTUID0000000000001"
QR = make_qr([(2, b"VTECH_5754_test"), (3, UID.encode()), (5, b"Pw0rdXyz"), (7, bytes.fromhex("0011223344ff"))])
QR_ROTATED = make_qr([(3, UID.encode()), (5, b"NewPw123"), (7, bytes.fromhex("0011223344ff"))])
QR_OTHER = make_qr([(3, b"OTHERUID000000000002"), (5, b"Pw0rdXyz")])
TEMP = "sensor.vtech_5754_test_temperature"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, socket_enabled):
    """Custom integrations on; sockets on (the worker talks over a socket pair and a loopback port)."""
    yield


class Fake(FakeLinkModule):
    """The fake module, with shortcuts to the camera's current link (the newest one opened)."""

    @property
    def link(self):
        return self.opened[-1]

    def push(self, code, payload):
        self.link.push(code, payload)

    def kill(self):
        """The camera's link ends, as if it dropped off the network."""
        self.link.kill()

    def fail_connect(self, code):
        """New connects fail the way the camera would: -68 is the wrong auth key, anything else unreachable."""
        self.connect_error = (LinkError("iotc_rejected", "auth_key", code) if code == -68
                              else LinkError("unreachable", "", code))

    def heal(self):
        self.connect_error = None

    @property
    def sent(self):
        return self.link.sent

    def sent_codes(self):
        return self.link.sent_codes()

    @property
    def uplink(self):
        return self.link.uplink

    @property
    def talks_closed(self):
        return sum(t.closed_with is not None for t in self.link.talks)


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture(autouse=True)
def module_patch(fake):
    def prep(client):
        client.ensure("/pkg/_vtech_link.abi3.so", "fp")
        return client
    with patch.object(mgr, "_prepare_and_load", prep), \
         patch.object(mgr, "make_client", lambda on_crash: thread_client(fake, on_crash=on_crash)), \
         patch.object(native, "host_supported", lambda: "x86_64"), \
         patch.object(mgr, "RECONNECT_MIN_S", 0.05), patch.object(mgr, "RECONNECT_MAX_S", 0.1):
        yield


def entry_data(qr=QR):
    c = provision.provision(qr)
    return {CONF_UID: c.uid, CONF_ACCOUNT: c.account, CONF_AV_PASSWORD: c.av_password,
            CONF_AUTHKEY: c.authkey, CONF_MAC: c.mac}


async def setup_entry(hass, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="VTECH_5754_test", unique_id=UID, data=entry_data(), version=2, **kw)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def unload(hass, entry):
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def worker(hass):
    """The worker object behind the client (running on a thread in these tests)."""
    return hass.data[mgr.DATA_CLIENT]._handle.worker


def worker_session(hass, m):
    """The real Session in the worker behind the manager's proxy."""
    return worker(hass).sessions[m.session._key]


def live(hass):
    """The camera's LiveStream in the worker."""
    return worker(hass).streams[UID]


async def wait_until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.05)


# ── Config flow ──────────────────────────────────────────────────────────────
async def test_user_flow_creates_entry(hass: HomeAssistant, fake):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "camera"      # straight to the pairing QR
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: "garbage"})
    assert r["errors"] == {CONF_QR: "invalid_qr"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "VTECH_5754_test"
    assert r["data"] == entry_data()
    assert r["version"] == 2 and r["result"].unique_id == UID
    await hass.async_block_till_done()
    assert hass.states.get(TEMP).state == "21"
    assert [link.uid for link in fake.opened] == [UID, UID]               # the probe, then the entry
    await unload(hass, r["result"])


async def test_untested_model_warns_then_adds(hass: HomeAssistant, fake):
    fake.camera.model = b"BM9999"
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "untested_model"
    assert r["description_placeholders"] == {"model": "BM9999", "tested": "VTech RM5754HD"}
    assert not hass.config_entries.async_entries(DOMAIN)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"] == entry_data()
    await hass.async_block_till_done()
    await unload(hass, r["result"])


async def test_unsupported_host(hass: HomeAssistant):
    with patch.object(native, "host_supported", lambda: None):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "unsupported_host"
    assert r["description_placeholders"]["machine"]


async def test_module_load_failure_is_reported(hass: HomeAssistant):
    def broken(client):
        raise mgr.SetupError("can't load the module")
    with patch.object(mgr, "_prepare_and_load", broken):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "camera"
    assert r["errors"] == {"base": "module_unavailable"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(("rc", "error"), [(-13, "cannot_connect"), (-68, "invalid_auth")])
async def test_flow_connect_errors(hass: HomeAssistant, fake, rc, error):
    fake.fail_connect(rc)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["errors"] == {"base": error}


async def test_second_camera_dedupes(hass: HomeAssistant):
    entry = await setup_entry(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_QR: QR})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
    await unload(hass, entry)


async def test_reauth(hass: HomeAssistant, fake):
    fake.fail_connect(-68)             # rotated credentials: setup fails
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    fake.heal()
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
    fake.fail_connect(-90)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_wrong_auth_starts_reauth(hass: HomeAssistant, fake):
    fake.fail_connect(-68)
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
    fake.fail_connect(-90)
    fake.kill()
    await wait_until(lambda: hass.states.get(TEMP).state == STATE_UNAVAILABLE)
    await wait_until(lambda: entry.runtime_data.failures >= 1)
    fake.heal()
    await wait_until(lambda: hass.states.get(TEMP).state == "21")
    await unload(hass, entry)


async def test_minus13_trap_starts_reauth_when_camera_on_lan(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    with patch.object(mgr, "STALE_CREDS_FAILURES", 2), patch.object(mgr, "mac_present", lambda mac: True):
        fake.fail_connect(-13)
        fake.kill()
        await wait_until(lambda: any(f["context"]["source"] == "reauth"
                                     for f in hass.config_entries.flow.async_progress()))
    assert entry.runtime_data.failures >= 2
    fake.heal()           # it was only an outage: the prompt is withdrawn
    await wait_until(lambda: hass.states.get(TEMP).state == "21")
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    await unload(hass, entry)


async def test_minus13_without_camera_on_lan_just_retries(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    with patch.object(mgr, "STALE_CREDS_FAILURES", 1), patch.object(mgr, "mac_present", lambda mac: False):
        fake.fail_connect(-13)
        fake.kill()
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
    assert d["module"]["link"]["version"] == "0.1.0+fake" and d["link"]["info"]["mode"] == "lan"
    assert "stats" in d["link"] and "sdk" not in d and "backend" not in d
    await unload(hass, entry)


# ── Migration ────────────────────────────────────────────────────────────────
async def test_migrate_v1_entry_drops_the_sdk(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    root = tmp_path / "vtech_baby"
    for sub in ("sdk", "lib"):
        (root / sub).mkdir(parents=True)
        (root / sub / "libIOTCAPIs.so").write_bytes(b"x")
    (root / "notes.txt").write_text("not ours to delete")
    entry = MockConfigEntry(domain=DOMAIN, title="VTECH_5754_test", unique_id=UID, version=1,
                            data={**entry_data(), CONF_SDK_DIR: str(root / "sdk")})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2 and entry.data == entry_data()
    assert entry.state is ConfigEntryState.LOADED
    assert not (root / "sdk").exists() and not (root / "lib").exists()
    assert (root / "notes.txt").read_text() == "not ours to delete"       # the directory stays
    await unload(hass, entry)


async def test_migrate_removes_the_empty_directory(hass: HomeAssistant, tmp_path):
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "vtech_baby" / "sdk").mkdir(parents=True)
    (tmp_path / "vtech_baby" / "sdk" / "libIOTCAPIs.so").write_bytes(b"x")
    entry = MockConfigEntry(domain=DOMAIN, title="VTECH_5754_test", unique_id=UID, version=1,
                            data={**entry_data(), CONF_SDK_DIR: "/sdk"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2 and not (tmp_path / "vtech_baby").exists()
    await unload(hass, entry)


async def test_newer_entry_version_is_refused(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, title="VTECH_5754_test", unique_id=UID, version=3, data=entry_data())
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


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


# ── Entities ─────────────────────────────────────────────────────────────────
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
    worker_session(hass, m).request_timeout_s = 0.2
    fake.camera.silent |= {0x733, 0x7A0, 0x709, 0x713, 0x322}        # camera stops answering
    await m.async_refresh()
    assert hass.states.get(TEMP).state == "21"                        # kept, not unknown
    assert hass.states.get(f"select.{P}_resolution").state == "1080p"
    assert fake.sent_codes().count(0x7A0) == 2                        # setup read + one; then gave up
    assert 0x709 not in fake.sent_codes()[-3:]
    await unload(hass, entry)


async def test_worker_death_reconnects_on_a_new_worker(hass: HomeAssistant, fake):
    """The worker dying (here: its socket cut) loses the session, raises a Repairs issue, and the
    camera reconnects on a fresh worker, with Home Assistant untouched."""
    from homeassistant.helpers import issue_registry as ir
    entry = await setup_entry(hass)
    client = hass.data[mgr.DATA_CLIENT]
    first = client._handle
    first.kill()
    await wait_until(lambda: hass.states.get(TEMP).state == STATE_UNAVAILABLE)
    # The issue is raised by a separate loop callback, which can run after the state change.
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, "worker_crashed") is not None)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "worker_crashed")
    assert issue.translation_placeholders == {"reason": "exit status 0", "count": "1"}
    await wait_until(lambda: hass.states.get(TEMP).state == "21")
    assert client._handle is not first and client.gen == 2
    await unload(hass, entry)


# ── Camera ───────────────────────────────────────────────────────────────────
CAM = f"camera.{P}_live_view"


def camera_entity(hass):
    return hass.data["camera"].get_entity(CAM)


async def test_camera_entity_and_snapshot(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from fake_camera import clip, enable_live_view
    if not clip():
        pytest.skip("needs ffmpeg for the test clip")
    from homeassistant.components.camera import CameraEntityFeature, async_get_image

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    st = hass.states.get(CAM)
    assert st.state == "idle"
    assert st.attributes["supported_features"] == CameraEntityFeature.STREAM
    live(hass)._grace_s = 0.2
    image = await async_get_image(hass, CAM)
    assert image.content_type == "image/jpeg" and image.content[:2] == b"\xff\xd8"
    await wait_until(lambda: live(hass)._thread is None)
    assert not fake.camera.streaming                              # started for the snapshot, stopped after
    await unload(hass, entry)


async def test_camera_stream_over_loopback(hass: HomeAssistant, fake, socket_enabled):
    av = pytest.importorskip("av")
    import io

    import aiohttp
    from fake_camera import clip, enable_live_view
    if not clip():
        pytest.skip("needs ffmpeg for the test clip")

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    media = live(hass)
    media._grace_s = 0.2
    url = await camera_entity(hass).stream_source()
    client = hass.data[mgr.DATA_CLIENT]
    assert url == f"http://127.0.0.1:{client.port}/{client.token}/{UID}.ts"
    async with aiohttp.ClientSession() as http:
        async with http.get(url.replace(client.token, "wrong")) as r:
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
    await hass.config_entries.async_reload(entry.entry_id)          # reload keeps the same URL
    await hass.async_block_till_done()
    assert await camera_entity(hass).stream_source() == url
    await unload(hass, entry)
    async with aiohttp.ClientSession() as http:                     # unloaded camera: 404, server stays
        async with http.get(url) as r:
            assert r.status == 404


async def test_camera_without_session_has_no_source(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    fake.kill()
    await wait_until(lambda: hass.states.get(CAM).state == STATE_UNAVAILABLE)
    assert await camera_entity(hass).stream_source() is None
    await unload(hass, entry)


async def test_reload_while_streaming_is_prompt(hass: HomeAssistant, fake, socket_enabled):
    """A reload with a client mid-stream must not wait on that client (it hung ~2 min, 2026-10-06)."""
    pytest.importorskip("av")
    import time

    import aiohttp
    from fake_camera import clip, enable_live_view
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


# ── Speaker (talk-back) ──────────────────────────────────────────────────────
SPEAKER = f"media_player.{P}_speaker"


async def play(hass, url):
    await call(hass, "media_player", "play_media", SPEAKER, media_content_id=url, media_content_type="music")


async def test_speaker_plays_media_and_tags_its_own_sound(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from fake_camera import enable_live_view
    from test_talkback import serve, wav

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    assert hass.states.get(SPEAKER).state == "idle"
    with serve({"/tone.wav": wav(0.6, rate=8000, channels=1)}) as base:
        task = hass.async_create_task(play(hass, f"{base}/tone.wav"))
        await wait_until(lambda: hass.states.get(SPEAKER).state == "playing")
        assert fake.camera.speaker_ch == 1
        fake.push(0x1FFF, event_report(0x15))                      # the camera hears its own speaker
        await wait_until(lambda: hass.states.get(f"event.{P}_alert").attributes.get("event_type") == "sound")
        assert hass.states.get(f"event.{P}_alert").attributes["self_triggered"] is True
        assert hass.states.get(f"binary_sensor.{P}_sound").state == "off"
        await asyncio.wait_for(task, 10)                           # returns once it has played
    assert hass.states.get(SPEAKER).state == "idle"
    assert 29 <= len(fake.uplink) <= 31 and fake.camera.speaker_ch is None
    await unload(hass, entry)


async def test_speaker_stop_and_unload_while_playing(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from fake_camera import enable_live_view
    from test_talkback import serve, wav

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    with serve({"/long.wav": wav(30.0, rate=8000, channels=1)}) as base:
        task = hass.async_create_task(play(hass, f"{base}/long.wav"))
        await wait_until(lambda: hass.states.get(SPEAKER).state == "playing")
        await call(hass, "media_player", "media_stop", SPEAKER)
        await asyncio.wait_for(task, 10)                           # a stopped play_media just returns
        assert hass.states.get(SPEAKER).state == "idle" and fake.camera.speaker_ch is None

        hass.async_create_task(play(hass, f"{base}/long.wav"))
        await wait_until(lambda: hass.states.get(SPEAKER).state == "playing")
        closed = fake.talks_closed
        await unload(hass, entry)
    assert fake.talks_closed == closed + 1 and fake.camera.speaker_ch is None


async def test_speaker_bad_media(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from homeassistant.exceptions import HomeAssistantError
    from test_talkback import serve

    entry = await setup_entry(hass)
    sent = len(fake.sent)
    with serve({"/text": b"not audio " * 100}) as base:
        with pytest.raises(HomeAssistantError, match="can't play the media"):
            await play(hass, f"{base}/text")
    assert 0x350 not in [c for c, _ in fake.sent[sent:]] and hass.states.get(SPEAKER).state == "idle"
    await unload(hass, entry)


async def test_speaker_ends_when_the_worker_dies(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from homeassistant.exceptions import HomeAssistantError
    from fake_camera import enable_live_view
    from test_talkback import serve, wav

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    with serve({"/long.wav": wav(30.0, rate=8000, channels=1)}) as base:
        task = hass.async_create_task(play(hass, f"{base}/long.wav"))
        await wait_until(lambda: hass.states.get(SPEAKER).state == "playing")
        hass.data[mgr.DATA_CLIENT]._handle.kill()
        with pytest.raises(HomeAssistantError, match="talk-back stopped"):
            await asyncio.wait_for(task, 10)
    assert hass.states.get(SPEAKER).state in ("idle", STATE_UNAVAILABLE)
    await wait_until(lambda: fake.camera.speaker_ch is None)       # the worker's shutdown stopped it
    await unload(hass, entry)


def lullaby_sends(fake, since=0):
    return [struct.unpack("<ii", p)[1] for c, p in fake.sent[since:] if c == 0x735]


async def test_speaker_lullabies(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    st = hass.states.get(SPEAKER)
    assert st.state == "idle" and "Brahms' Lullaby" in st.attributes["source_list"]
    await call(hass, "media_player", "select_source", SPEAKER, source="Brahms' Lullaby")
    st = hass.states.get(SPEAKER)
    assert st.state == "playing" and st.attributes["source"] == "Brahms' Lullaby"
    assert st.attributes["media_title"] == "Brahms' Lullaby" and fake.camera.params[1] == 5
    fake.push(0x7AC, i32(1, 3))                                    # changed on the handset
    await wait_until(lambda: hass.states.get(SPEAKER).attributes.get("source") == "Hot Cross Buns")
    await call(hass, "media_player", "media_stop", SPEAKER)
    assert hass.states.get(SPEAKER).state == "idle" and fake.camera.params[1] == -1
    fake.push(0x7AC, i32(1, 0))                                    # Play All, from the handset
    await wait_until(lambda: hass.states.get(SPEAKER).attributes.get("source") == "Play All")
    fake.push(0x7AC, i32(1, -1))                                   # the lullaby timer ran out
    await wait_until(lambda: hass.states.get(SPEAKER).state == "idle")
    with pytest.raises(Exception, match="unknown lullaby"):
        await call(hass, "media_player", "select_source", SPEAKER, source="Baa Baa Black Sheep")
    await unload(hass, entry)


async def test_speaker_volume(hass: HomeAssistant, fake):
    entry = await setup_entry(hass)
    assert hass.states.get(SPEAKER).attributes["volume_level"] == 1.0  # 5 of 5
    await call(hass, "media_player", "volume_set", SPEAKER, volume_level=0.4)
    assert fake.camera.params[5] == 2 and hass.states.get(SPEAKER).attributes["volume_level"] == 0.4
    await call(hass, "media_player", "volume_down", SPEAKER)
    assert fake.camera.params[5] == 1
    await call(hass, "media_player", "volume_up", SPEAKER)
    await call(hass, "media_player", "volume_up", SPEAKER)
    assert fake.camera.params[5] == 3
    fake.push(0x7AC, i32(0x14, 4))                                 # changed on the handset
    await wait_until(lambda: hass.states.get(SPEAKER).attributes["volume_level"] == 0.8)
    assert fake.camera.params[3] == 1800 and fake.camera.params[7] == 1234     # the rest is kept
    await unload(hass, entry)


@pytest.mark.parametrize("announce", [False, True])
async def test_media_stops_a_lullaby_and_an_announcement_resumes_it(hass: HomeAssistant, fake, announce):
    pytest.importorskip("av")
    from fake_camera import enable_live_view
    from test_talkback import serve, wav

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    await call(hass, "media_player", "select_source", SPEAKER, source="Crickets")
    sent = len(fake.sent)
    with serve({"/tone.wav": wav(0.3, rate=8000, channels=1)}) as base:
        await call(hass, "media_player", "play_media", SPEAKER, media_content_id=f"{base}/tone.wav",
                   media_content_type="music", announce=announce)
    codes = [c for c, _ in fake.sent[sent:]]
    assert codes.index(0x735) < codes.index(0x350)                 # stopped before talking
    if announce:
        assert lullaby_sends(fake, sent) == [-1, 7]
        assert hass.states.get(SPEAKER).attributes["source"] == "Crickets"
    else:
        assert lullaby_sends(fake, sent) == [-1]
        assert hass.states.get(SPEAKER).state == "idle"
    await unload(hass, entry)


async def test_choosing_a_lullaby_stops_media(hass: HomeAssistant, fake):
    pytest.importorskip("av")
    from fake_camera import enable_live_view
    from test_talkback import serve, wav

    enable_live_view(fake.camera)
    entry = await setup_entry(hass)
    with serve({"/long.wav": wav(30.0, rate=8000, channels=1)}) as base:
        task = hass.async_create_task(play(hass, f"{base}/long.wav"))
        await wait_until(lambda: hass.states.get(SPEAKER).attributes.get("media_title") == "Talk-back")
        await call(hass, "media_player", "select_source", SPEAKER, source="Birds")
        await asyncio.wait_for(task, 10)
    st = hass.states.get(SPEAKER)
    assert st.attributes["source"] == "Birds" and fake.camera.speaker_ch is None and not entry.runtime_data.talking
    await unload(hass, entry)
