"""The worker as a real process (with the fake link module): requests, pushes, crashes, restarts, isolation.

The integration tests run the same worker code on a thread; these cover what only a process shows.
"""
import functools
import os
import subprocess
import sys
import threading
import time

import pytest

from custom_components.vtech_baby.vtech_lan import errors, ops
from custom_components.vtech_baby.vtech_lan import worker_client as wc
from custom_components.vtech_baby.vtech_lan.session import PARAMS_UPDATE

TESTS = os.path.dirname(os.path.abspath(__file__))
UID, CRASH = "TESTUID0000000000001", "CRASHUID000000000001"


@pytest.fixture
def client(socket_enabled):
    crashes = []
    spawn = functools.partial(wc.spawn_process, extra_args=("--link-loader", "worker_fake:make", "--path", TESTS))
    c = wc.WorkerClient(spawn=spawn, on_crash=lambda reason, n: crashes.append(reason))
    c.crash_log = crashes
    c.ensure("/x/_vtech_link.abi3.so", "one")
    yield c
    c.stop()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_session_calls_and_pushes(client):
    pushes, got = [], threading.Event()

    def on_push(p):
        pushes.append(p)
        got.set()
    s = client.session(UID, "k3yAuth8", "pw", name="cam", keepalive_s=0.2, on_push=on_push)
    dev = s.connect()
    assert isinstance(dev, ops.DevInfo) and dev.model == "RM5754" and s.devinfo == dev
    assert type(s.get_params()) is ops.Params
    assert s.connection_mode() == "lan"
    assert got.wait(5) and pushes[0].code == PARAMS_UPDATE and type(pushes[0].value) is ops.Params
    with pytest.raises(AttributeError):
        s.no_such_method()
    s.close()
    with pytest.raises(errors.SessionGone):
        s.get_params()
    assert client.running


def test_worker_has_no_home_assistant(client):
    assert "homeassistant" in sys.modules           # the test process has it ...
    info = client.module_info()
    assert info["link"]["lan_only"] == {"homeassistant_modules": []}
    assert info["pid"] == client.pid != os.getpid()


def test_native_crash_is_contained(client):
    lost = []
    s = client.session(UID, "k3yAuth8", "pw", name="cam", keepalive_s=0, on_lost=lost.append)
    s.connect()
    pid = client.pid
    with pytest.raises(errors.WorkerDied, match="SIGSEGV"):
        client.session(CRASH, "k3yAuth8", "p", name="crash", keepalive_s=0).connect()
    deadline = time.monotonic() + 5
    while not (lost and client.crash_log) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [type(e) for e in lost] == [errors.WorkerDied]
    assert client.crash_log == ["SIGSEGV"] and client.crashes == 1 and not alive(pid)
    with pytest.raises(errors.WorkerDied):
        s.get_params()                              # the old session belongs to the dead worker
    s2 = client.session(UID, "k3yAuth8", "pw", name="cam", keepalive_s=0)
    assert s2.connect().model == "RM5754"           # a fresh worker, same module
    assert client.pid != pid and client.gen == 2
    s2.close()


def test_new_module_restarts_the_worker(client):
    lost = []
    s = client.session(UID, "k3yAuth8", "pw", name="cam", keepalive_s=0, on_lost=lost.append)
    s.connect()
    pid = client.pid
    assert client.ensure("/x/_vtech_link.abi3.so", "one")["pid"] == pid   # same module: nothing happens
    client.ensure("/x/_vtech_link.abi3.so", "two")                  # a rebuilt module: new worker
    pid2 = client.pid
    assert pid2 != pid and not alive(pid)
    client.ensure("/y/_vtech_link.abi3.so", "two")                  # a different path: new worker too
    assert client.pid != pid2 and not alive(pid2)
    deadline = time.monotonic() + 5
    while not lost and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [type(e) for e in lost] == [errors.WorkerDied] and client.crash_log == []


def test_stop_ends_the_process(client):
    pid = client.pid
    client.stop()
    assert not alive(pid) and not client.running
    with pytest.raises(errors.SessionGone):
        client.session(UID, "k3yAuth8", "pw", keepalive_s=0).connect()


def test_refuses_the_package_dir_on_sys_path():
    main = os.path.join(os.path.dirname(wc.__file__), "_worker_main.py")
    pkg = os.path.dirname(os.path.dirname(main))
    r = subprocess.run([sys.executable, main, "custom_components.vtech_baby", "--fd", "0"],
                       env={**os.environ, "PYTHONPATH": pkg}, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "would shadow" in r.stderr
