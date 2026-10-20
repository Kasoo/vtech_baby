"""Offline checks of Global's task manager and connection manager: their threads, callbacks and real
UDP traffic over loopback, through Global's LAN-only egress check."""
import ctypes as C
import socket
import threading
import time

import pytest
from test_global_offline import TOS_ER_INVALID_ARG, TOS_ER_RESOLVE_FAILED, fn

TASK_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.c_uint16, C.c_void_p, C.c_int)
keep = []          # ctypes callbacks must outlive the C side's use of them


def wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ---- task manager ----

@pytest.fixture(scope="module")
def tasks(g):
    assert fn(g, "tutk_TaskMng_Init", C.c_int)() == 0
    create = fn(g, "tutk_TaskMng_Create", C.c_void_p, C.c_int, C.c_int, C.c_uint16, TASK_CB, C.c_void_p)
    delete = fn(g, "tutk_TaskMng_Delete", C.c_int, C.c_void_p)
    return create, delete


def recorder():
    calls = []
    cb = TASK_CB(lambda task, interval, tag, arg, final: calls.append((task, interval, tag, arg, final)))
    keep.append(cb)
    return calls, cb


def test_periodic_task(tasks):
    create, delete = tasks
    calls, cb = recorder()
    t = create(50, 0, 7, cb, 99)
    assert t
    time.sleep(0.6)
    assert delete(t) == 1
    n = len(calls)
    assert 6 <= n <= 20
    assert all(c == (t, 50, 7, 99, 0) for c in calls)
    time.sleep(0.2)
    assert len(calls) == n                       # nothing runs after Delete


def test_task_lifetime_ends_with_final_call(tasks):
    create, _ = tasks
    calls, cb = recorder()
    create(50, 300, 1, cb, None)
    assert wait_for(lambda: calls and calls[-1][4] == 1)
    n = len(calls)
    assert [c[4] for c in calls] == [0] * (n - 1) + [1]
    assert 4 <= n <= 9
    time.sleep(0.2)
    assert len(calls) == n


def test_callback_may_delete_its_own_task(tasks):
    create, delete = tasks
    calls, results = [], []

    def body(task, interval, tag, arg, final):
        calls.append(task)
        results.append(delete(task))             # the manager's lock is recursive

    cb = TASK_CB(body)
    keep.append(cb)
    create(20, 0, 0, cb, None)
    assert wait_for(lambda: calls)
    time.sleep(0.2)
    assert len(calls) == 1 and results == [1]


# ---- connection manager ----

class MgrVtbl(C.Structure):
    _fields_ = [(n, C.c_void_p) for n in ("retain", "release", "createConnection", "resetListen",
                                          "removeConnection", "setPollIntervalMs", "setTickCallback",
                                          "callOnThread")]


class ConnVtbl(C.Structure):
    _fields_ = [(n, C.c_void_p) for n in ("retain", "release", "open", "setAcceptCallback", "sendToPeer",
                                          "send", "setReceiveCallback", "setErrorCallback", "getStage",
                                          "getLocalEndpoint", "setUnicastHops", "setOption", "getOption",
                                          "setReassemble", "clearCallbacks")]


def vtbl(obj, cls):
    return C.cast(C.c_void_p(obj), C.POINTER(C.POINTER(cls))).contents.contents


def method(addr, restype, *argtypes):
    return C.CFUNCTYPE(restype, *argtypes)(addr)


TICK_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
READY_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
ERROR_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.c_void_p)
RECV_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_char_p, C.c_uint16, C.c_void_p, C.c_long, C.c_void_p)


@pytest.fixture(scope="module")
def manager(g):
    create = fn(g, "TConnManager_create", C.c_int, C.POINTER(C.c_void_p))
    m1, m2 = C.c_void_p(), C.c_void_p()
    assert create(C.byref(m1)) == 0 and create(C.byref(m2)) == 0
    assert m1.value == m2.value                  # one manager per process
    v = vtbl(m1.value, MgrVtbl)
    method(v.setPollIntervalMs, C.c_int, C.c_void_p, C.c_int)(m1, 100)
    return m1.value, v


def test_tick_callback(manager):
    m, v = manager
    ticks = []
    cb = TICK_CB(lambda mgr, arg: ticks.append((mgr, arg)))
    keep.append(cb)
    assert method(v.setTickCallback, C.c_int, C.c_void_p, TICK_CB, C.c_void_p)(m, cb, 5) == 0
    assert wait_for(lambda: len(ticks) >= 2)
    assert ticks[0] == (m, 5)
    method(v.setTickCallback, C.c_int, C.c_void_p, TICK_CB, C.c_void_p)(m, TICK_CB(), None)


def test_bound_udp_connection(manager, g):
    m, mv = manager
    conn = C.c_void_p()
    create_conn = method(mv.createConnection, C.c_int, C.c_void_p, C.c_int, C.c_int, C.c_uint32,
                         C.POINTER(C.c_void_p))
    assert create_conn(m, 1, 1, 0, C.byref(conn)) == 0 and conn.value
    cv = vtbl(conn.value, ConnVtbl)
    open_ = method(cv.open, C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_uint16), C.c_int,
                   READY_CB, C.c_void_p, ERROR_CB, C.c_void_p)
    send = method(cv.send, C.c_int, C.c_void_p, C.c_char_p, C.c_uint16, C.c_char_p, C.POINTER(C.c_int))
    set_recv = method(cv.setReceiveCallback, C.c_int, C.c_void_p, RECV_CB, C.c_void_p)
    set_err = method(cv.setErrorCallback, C.c_int, C.c_void_p, ERROR_CB, C.c_void_p)
    set_opt = method(cv.setOption, C.c_int, C.c_void_p, C.c_int, C.c_void_p)

    received, ready, failed = [], [], []
    lock = threading.Lock()

    def on_recv(c, host, port, data, n, arg):
        with lock:
            received.append((c, host.decode(), port, C.string_at(data, n), arg))

    cbs = [RECV_CB(on_recv), READY_CB(lambda c, a: ready.append((c, a))),
           ERROR_CB(lambda c, e, a: failed.append((c, e, a)))]
    keep.extend(cbs)
    on_recv_cb, ready_cb, failed_cb = cbs
    assert set_recv(conn, on_recv_cb, 11) == 0
    assert set_err(conn, failed_cb, 12) == 0

    port = C.c_uint16(0)                         # the system chooses, as IOTC asks
    assert open_(conn, None, C.byref(port), 0, ready_cb, 13, failed_cb, 14) == 0
    assert ready == [(conn.value, 13)] and failed == []
    ours = socket.ntohs(port.value)
    assert ours != 0
    assert method(cv.setUnicastHops, C.c_int, C.c_void_p, C.c_int)(conn, 1) == 0

    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.settimeout(2)
    peer_port = peer.getsockname()[1]
    try:
        # peer -> us: delivered on the manager thread, sender as an IPv4-mapped literal
        peer.sendto(b"hello", ("127.0.0.1", ours))
        assert wait_for(lambda: received)
        assert received[0] == (conn.value, "::ffff:127.0.0.1", socket.htons(peer_port), b"hello", 11)

        # us -> peer, to the address exactly as from_host reported it, and as plain IPv4
        for host in (b"::ffff:127.0.0.1", b"127.0.0.1"):
            n = C.c_int(4)
            assert send(conn, host, socket.htons(peer_port), b"ping", C.byref(n)) == 0 and n.value == 4
            data, src = peer.recvfrom(64)
            assert data == b"ping" and src[1] == ours

        # off-link: refused by Global's egress check; host names are never resolved
        before, lookups = C.c_ulong(), C.c_ulong()
        g.tk_lan_only_stats(C.byref(before), C.byref(lookups))
        n = C.c_int(4)
        assert send(conn, b"::1", socket.htons(peer_port), b"ping", C.byref(n)) == -0x1100162   # ENETUNREACH
        after = C.c_ulong()
        g.tk_lan_only_stats(C.byref(after), C.byref(lookups))
        assert after.value == before.value + 1
        assert send(conn, b"localhost", socket.htons(peer_port), b"ping", C.byref(n)) == -TOS_ER_RESOLVE_FAILED

        # a smaller receive buffer truncates datagrams; several queue up and all arrive in order
        size = C.c_uint32(8)
        assert set_opt(conn, 0, C.byref(size)) == 0
        with lock:
            received.clear()
        for i in range(5):
            peer.sendto(b"%d-0123456789" % i, ("127.0.0.1", ours))
        assert wait_for(lambda: len(received) == 5)
        assert [r[3] for r in received] == [b"%d-012345" % i for i in range(5)]
    finally:
        peer.close()
    assert failed == []


def test_remove_connection(manager):
    m, mv = manager
    conn = C.c_void_p()
    create_conn = method(mv.createConnection, C.c_int, C.c_void_p, C.c_int, C.c_int, C.c_uint32,
                         C.POINTER(C.c_void_p))
    remove = method(mv.removeConnection, C.c_int, C.c_void_p, C.c_void_p)
    assert create_conn(m, 1, 1, 0, C.byref(conn)) == 0
    cv = vtbl(conn.value, ConnVtbl)
    retain = method(cv.retain, C.c_int, C.c_void_p)
    release = method(cv.release, C.c_int, C.c_void_p)
    port = C.c_uint16(0)
    open_ = method(cv.open, C.c_int, C.c_void_p, C.c_char_p, C.POINTER(C.c_uint16), C.c_int,
                   READY_CB, C.c_void_p, ERROR_CB, C.c_void_p)
    assert open_(conn, None, C.byref(port), 0, READY_CB(), None, ERROR_CB(), None) == 0
    assert remove(m, None) == -TOS_ER_INVALID_ARG
    assert remove(m, conn) == 0
    # The manager drops its reference on its next pass: the creator's is then the only one left.
    assert wait_for(lambda: (retain(conn), release(conn))[0] == 2)
    assert release(conn) == 0
