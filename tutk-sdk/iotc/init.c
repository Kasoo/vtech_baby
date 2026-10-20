/* Initialisation and the session-table helpers (kit: iotc/10-init.md, iotc/01-types.md). */
#include <stdlib.h>
#include <string.h>

#include "iotc.h"

#define POLL_INTERVAL_MS 500
#define ALIVE_INTERVAL_MS 1000
#define ALIVE_SILENCE_TICKS 60                      /* ticks of no inbound traffic before -23 */
#define ALIVE_QUIET_TICKS   5                       /* silent ticks before a keepalive every tick */
#define LAN_SEARCH_PORT_DEFAULT 32761

IotcState iotc = {.lan_search_port = LAN_SEARCH_PORT_DEFAULT};

static pthread_once_t locks_once = PTHREAD_ONCE_INIT;

/* The mutexes outlive IOTC_DeInitialize, so they are created once per process. */
static void create_locks(void) {
    tutk_platform_mutex_init(&iotc.table_lock);
    tutk_platform_mutex_init(&iotc.task_lock);
}

void iotc_lock(void) { pthread_mutex_lock(&iotc.table_lock); }
void iotc_unlock(void) { pthread_mutex_unlock(&iotc.table_lock); }

static int state_error(uint8_t state) {
    switch (state) {
    case SESSION_ESTABLISHED:
        return 0;
    case SESSION_CLOSED_BY_REMOTE:
        return IOTC_ER_SESSION_CLOSE_BY_REMOTE;
    case SESSION_REMOTE_TIMEOUT:
        return IOTC_ER_REMOTE_TIMEOUT_DISCONNECT;
    default:
        return IOTC_ER_INVALID_SID;
    }
}

int iotc_session_enter(int sid, IotcSession **out) {
    int rc;

    if (iotc.init != INIT_UP)
        return IOTC_ER_NOT_INITIALIZED;
    iotc_lock();
    if (iotc.init != INIT_UP) {
        iotc_unlock();
        return IOTC_ER_NOT_INITIALIZED;
    }
    if (sid < 0 || sid >= iotc.session_count) {
        iotc_unlock();
        return IOTC_ER_INVALID_SID;
    }
    rc = state_error(iotc.sessions[sid].state);
    if (rc != 0) {
        iotc_unlock();
        return rc;
    }
    *out = &iotc.sessions[sid];
    return 0;
}

void iotc_session_free_queues(IotcSession *s) {
    for (int ch = 0; ch < IOTC_MAX_CHANNELS; ch++) {
        TRxPacket *p = s->rx_queue[ch].head;

        while (p) {
            TRxPacket *next = p->next;

            free(p->data);
            free(p);
            p = next;
        }
        s->rx_queue[ch].head = s->rx_queue[ch].tail = NULL;
    }
}

/* A slot's state after init and after release: everything clear, channel 0 on. */
void iotc_session_defaults(IotcSession *s) {
    memset(s, 0, sizeof *s);
    s->chan_on[0] = true;
    s->session_id = -1;
    s->auth_type = -1;
}

static void conn_mgr_tick_cb(TConnManager *m, void *arg) {}

/* Each tick: time out any established session silent for too long, and send the rest a keepalive,
 * every other tick while the camera is talking and every tick once it has gone quiet. */
static void session_alive_task_cb(TTask *task, int interval_ms, uint16_t tag, void *arg, int final) {
    int timed_out[IOTC_DEFAULT_SESSIONS], n = 0;

    if (iotc.init != INIT_UP)
        return;
    iotc_lock();
    for (int i = 0; i < iotc.session_count; i++) {
        IotcSession *s = &iotc.sessions[i];

        if (s->state != SESSION_ESTABLISHED)
            continue;
        if (++s->silence_ticks > ALIVE_SILENCE_TICKS) {
            s->state = SESSION_REMOTE_TIMEOUT;
            if (n < IOTC_DEFAULT_SESSIONS)
                timed_out[n++] = i;
        } else if (s->silence_ticks >= ALIVE_QUIET_TICKS || ++s->alive_ticks % 2 == 0) {
            iotc_send_keepalive(s);
        }
    }
    iotc_unlock();
    for (int i = 0; i < n; i++)
        iotc_fire_close_callbacks(timed_out[i], IOTC_ER_REMOTE_TIMEOUT_DISCONNECT);
}

TK_EXPORT int IOTC_Initialize2(uint16_t listen_port) {
    uint16_t nonce;

    iotc_build_key();
    if (iotc.init != INIT_DOWN)
        return IOTC_ER_ALREADY_INITIALIZED;
    pthread_once(&locks_once, create_locks);
    if (iotc.session_count == 0)
        iotc.session_count = IOTC_DEFAULT_SESSIONS;
    iotc.sessions = tutk_platform_malloc(iotc.session_count * (int)sizeof(IotcSession));
    if (iotc.sessions == NULL)
        return IOTC_ER_NOT_ENOUGH_MEMORY;
    for (int i = 0; i < iotc.session_count; i++)
        iotc_session_defaults(&iotc.sessions[i]);
    iotc.listen_port = listen_port;
    tutk_platform_srand();
    do
        nonce = (uint16_t)tutk_platform_rand();
    while (nonce == 0);
    iotc.nonce_base = nonce;
    UpdateLocalNetworkInfo();

    if (TConnManager_create(&iotc.conn_mgr) != 0 || iotc.conn_mgr == NULL)
        goto fail;
    if (iotc.conn_mgr->vtbl->setTickCallback(iotc.conn_mgr, conn_mgr_tick_cb, NULL) != 0 ||
        iotc.conn_mgr->vtbl->setPollIntervalMs(iotc.conn_mgr, POLL_INTERVAL_MS) != 0)
        goto fail;
    tutk_TaskMng_Init();
    iotc.alive_task = tutk_TaskMng_Create(ALIVE_INTERVAL_MS, 0, 0, session_alive_task_cb, NULL);
    if (iotc.alive_task == NULL)
        goto fail;
    initNatTrack(&iotc_nat);
    if (IOTC_sCHL_initialize() != 0)
        goto fail;
    IOTC_TryPortInitialize(iotc.conn_mgr, &iotc);
    iotc.init = INIT_UP;
    return 0;

fail:
    if (iotc.alive_task) {
        tutk_TaskMng_Delete(iotc.alive_task);
        iotc.alive_task = NULL;
    }
    tutk_platform_free(iotc.sessions);
    iotc.sessions = NULL;
    return IOTC_ER_RESOURCE_ERROR;
}

TK_EXPORT int IOTC_Set_LanSearchPort(unsigned int port) {
    if (port >= 0x10000)
        return IOTC_ER_INVALID_ARG;
    iotc.lan_search_port = (uint16_t)port;
    return 0;
}

int IOTC_Set_Partial_Encryption(unsigned int session_id, char enable) {
    IotcSession *s;
    int rc = iotc_session_enter((int)session_id, &s);

    if (rc != 0)
        return rc;
    if (!s->pe_capable)
        rc = IOTC_ER_NOT_SUPPORT_PE;
    else
        s->pe_enabled = enable != 0;
    iotc_unlock();
    return rc;
}
