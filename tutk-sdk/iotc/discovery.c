/* LAN discovery: the shared socket, the subnet broadcast and the LAN-search task
 * (kit: iotc/40-discovery.md, iotc/16-handshake.md). */
#include <arpa/inet.h>
#include <string.h>

#include "iotc.h"

#define LAN_SEARCH_INTERVAL_MS 100

static void conn_ready(TConnection *c, void *arg) {}
static void conn_failed(TConnection *c, int error, void *arg) { tk_trace("iotc: socket error %d", error); }

/* Open the bound UDP socket on first use; every session sends and receives through it. */
int iotc_open_transport(void) {
    TConnection *c = NULL;
    uint16_t port = 0;
    int rc = 0;

    iotc_lock();
    if (iotc.conn)
        goto out;
    rc = iotc.conn_mgr->vtbl->createConnection(iotc.conn_mgr, TCONN_UDP, TCONN_ROLE_BOUND, 0, &c);
    if (rc != 0 || c == NULL) {
        rc = IOTC_ER_FAIL_CREATE_SOCKET;
        goto out;
    }
    c->vtbl->setReceiveCallback(c, iotc_on_datagram, NULL);
    rc = c->vtbl->open(c, NULL, &port, 0, conn_ready, NULL, conn_failed, NULL);
    if (rc != 0) {
        iotc.conn_mgr->vtbl->removeConnection(iotc.conn_mgr, c);
        rc = IOTC_ER_FAIL_CREATE_SOCKET;
        goto out;
    }
    iotc.conn = c;
    tk_trace("iotc: socket bound to port %u", ntohs(port));
out:
    iotc_unlock();
    return rc;
}

/* Send `packet` as it is to the directed broadcast address of every IPv4 interface. */
void Broadcast_Message_By_Netmask(TConnection *conn, const void *packet, unsigned int len,
                                  uint16_t port) {
    TNetInterfaceInfo info;

    memset(&info, 0, sizeof info);
    if (conn == NULL || tutk_Net_GetInterfaceInfo(&info) < 0)
        return;
    for (int i = 0; i < info.count && i < 10; i++) {
        struct in_addr bcast = {.s_addr = info.addr[i] | ~info.netmask[i]};
        char host[INET_ADDRSTRLEN];
        int n = (int)len;

        if (info.addr[i] == 0 || info.netmask[i] == 0 ||
            (ntohl(info.addr[i]) >> 24) == IN_LOOPBACKNET)
            continue;
        inet_ntop(AF_INET, &bcast, host, sizeof host);
        conn->vtbl->send(conn, host, htons(port), packet, &n);
    }
}

/* Each run broadcasts the credentialed record and the connect-path probe, until the session binds. */
static void lan_search_task_cb(TTask *task, int interval_ms, uint16_t tag, void *arg, int final) {
    int sid = (int)(intptr_t)arg;
    uint8_t record[IOTC_RECORD_LEN], probe[IOTC_PROBE_LEN];
    IotcSession *s;
    bool search;

    iotc_lock();
    s = &iotc.sessions[sid];
    search = s->state == SESSION_CONNECTING && s->lan_state == LAN_SEARCH_RUNNING &&
             iotc_build_lan_record(s, record) > 0;
    if (search) {
        iotc_build_connect_probe(s, probe);
        probe[3] |= IOTC_FLAG_SCRAMBLED;
        iotc_scramble(iotc.key, probe, probe, sizeof probe, sizeof probe);
    }
    iotc_unlock();
    if (!search)
        return;
    Broadcast_Message_By_Netmask(iotc.conn, record, sizeof record, iotc.lan_search_port);
    Broadcast_Message_By_Netmask(iotc.conn, probe, sizeof probe, iotc.lan_search_port);
}

/* Start the session's LAN-search task; call without the table lock (the task manager's lock is taken
 * inside, and the task itself takes the table lock under it). */
int AddLanSearchTask(IotcSession *s) {
    TTask *task;

    task = tutk_TaskMng_Create(LAN_SEARCH_INTERVAL_MS, 0, 0, lan_search_task_cb,
                               (void *)(intptr_t)s->session_id);
    if (task == NULL)
        return -1;
    if (SessionTaskAdd(&s->tasks, task, 0) != 0) {
        tutk_TaskMng_Delete(task);
        return -1;
    }
    return 0;
}
