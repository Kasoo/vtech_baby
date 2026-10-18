"""Offline checks of our libIOTCAPIs.so: the scramble against the kit's worked example, and the
session, channel and stub behaviour that needs no camera."""
import ctypes as C
import subprocess

import pytest
from conftest import HERE, TUTK

ER_NOT_INITIALIZED = -12
ER_INVALID_SID = -14
ER_ALREADY_INITIALIZED = -3
ER_INVALID_ARG = -46


def test_scramble_matches_kit_example(tmp_path):
    exe = tmp_path / "wire_check"
    subprocess.run(["cc", "-O1", "-I", str(TUTK / "iotc"), "-I", str(TUTK / "include"),
                    "-I", str(TUTK / "common"), str(HERE / "wire_check.c"), str(TUTK / "iotc/wire.c"),
                    "-o", str(exe), "-lpthread"], check=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    scrambled, key = r.stdout.splitlines()
    assert scrambled == "c327b9f45de1dc2ee6c878542b229c60"
    assert key == "Charlie is the d"        # no licence: the bare constant


@pytest.fixture(scope="module")
def up(iotc):
    assert iotc.IOTC_Get_SessionID() == ER_NOT_INITIALIZED
    assert iotc.IOTC_Initialize2(0) == 0
    return iotc


def test_lan_search_port(iotc):
    assert iotc.IOTC_Set_LanSearchPort(0x10000) == ER_INVALID_ARG
    assert iotc.IOTC_Set_LanSearchPort(32762) == 0


def test_initialize_twice(up):
    assert up.IOTC_Initialize2(0) == ER_ALREADY_INITIALIZED


def test_unconnected_session(up):
    sid = up.IOTC_Get_SessionID()
    assert sid >= 0
    assert up.IOTC_Get_SessionID() == sid + 1
    info = (C.c_uint8 * 256)()
    assert up.IOTC_Session_Check(sid, info) == ER_INVALID_SID
    assert up.IOTC_Check_Session_Status(sid) == ER_INVALID_SID
    assert up.IOTC_Session_Get_Free_Channel(sid) == ER_INVALID_SID
    assert up.IOTC_Check_Session_Status(-1) == ER_INVALID_SID
    up.IOTC_Session_Close(sid)
    up.IOTC_Session_Close(sid + 1)
    assert up.IOTC_Get_SessionID() == sid


def test_check_ex_size(up):
    info = (C.c_uint32 * 40)(0x40)
    assert up.IOTC_Session_Check_Ex(0, info) == ER_INVALID_ARG


def test_nebula_and_authkey_stubs(up):
    assert up.get_nebula_client_info(0, None, 0, None, 0) == ER_INVALID_ARG
    assert up.get_nebula_device_identity_auth(0) == ER_INVALID_ARG
    assert up.IOTC_Device_Update_Authkey(None) == ER_INVALID_ARG


def test_lock_is_recursive(up):
    up.IOTC_Session_Lock()
    assert up.IOTC_Get_SessionID() >= 0     # takes the same lock
    up.IOTC_Session_unLock()


def test_dtls_context(up):
    up.IOTC_sCHL_CTX_new.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    ctx = C.c_void_p()
    assert up.IOTC_sCHL_CTX_new(None, C.byref(ctx)) == 0 and ctx.value
    up.IOTC_sCHL_CTX_set_cipher_list.argtypes = [C.c_void_p, C.c_char_p]
    assert up.IOTC_sCHL_CTX_set_cipher_list(ctx, b"PSK-AES128-CBC-SHA256") == 1
    up.IOTC_sCHL_new.restype = C.c_void_p
    up.IOTC_sCHL_new.argtypes = [C.c_void_p, C.c_int, C.c_int]
    ssl = up.IOTC_sCHL_new(ctx, 0, 1)
    assert ssl
    up.IOTC_sCHL_free.argtypes = [C.c_void_p]
    up.IOTC_sCHL_free(ssl)
    up.IOTC_sCHL_CTX_free.argtypes = [C.c_void_p]
    up.IOTC_sCHL_CTX_free(ctx)
