/* Client session: start, login, the read task (shared with the server), stop (kit: av/20-session.md). */
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avClientStartEx
 *   avClientStop
 */

#define LOGIN_POLL_MS       50
#define LOGIN_RESEND_TICKS  2      /* resend every 100 ms: the keepalive interval / 50 ms */
#define CRED_MODE_SHA256    1
#define WAIT_STEP_MS        5

/* The opcode-support block every login advertises (GetOpCodeSupportCurrent). */
const uint8_t av_opcode_support[20] = {
    0x04, 0x00, 0x00, 0x00, 0xfb, 0x07, 0x1f, 0x00, 0, 0, 0, 0, 0, 0, 0, 0, 0x00, 0x00, 0x03, 0x00,
};

void av_put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
void av_put32(uint8_t *p, uint32_t v) { av_put16(p, (uint16_t)v); av_put16(p + 2, (uint16_t)(v >> 16)); }
uint16_t av_get16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
uint32_t av_get32(const uint8_t *p) { return av_get16(p) | (uint32_t)av_get16(p + 2) << 16; }

uint32_t av_now_ms(void) {
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

/* Stamp the packet counter and send one packet on the sub-channel. */
int av_send(AvObj *o, uint8_t *pkt, int len) {
    int rc;

    pthread_mutex_lock(&o->lock);
    if (pkt[0] == CLASS_ACK || pkt[0] == CLASS_REPORT || pkt[0] == CLASS_DATA)
        av_put16(pkt + 4, o->tx_counter++);
    pthread_mutex_unlock(&o->lock);
    rc = av_chan_write(&o->chan, pkt, len);
    tk_trace("av: send class 0x%02x len %d -> %d", pkt[0], len, rc);
    return rc;
}

/* The channel's error code, or 0 while it is up. Caller holds o->lock. */
int av_check(AvObj *o) {
    if (o->error)
        return o->error;
    switch (IOTC_Check_Session_Status((unsigned)o->session_id)) {
    case IOTC_ER_SESSION_CLOSE_BY_REMOTE:
        o->error = AV_ER_SESSION_CLOSE_BY_REMOTE;
        break;
    case IOTC_ER_REMOTE_TIMEOUT_DISCONNECT:
        o->error = AV_ER_REMOTE_TIMEOUT_DISCONNECT;
        break;
    case IOTC_ER_INVALID_SID:
        o->error = AV_ER_INVALID_SID;
        break;
    default:
        break;
    }
    return o->error;
}

/* Mark the channel down with `err` and wake every waiter. */
static void set_error(AvObj *o, int err) {
    pthread_mutex_lock(&o->lock);
    if (!o->error)
        o->error = err;
    pthread_mutex_unlock(&o->lock);
}

/* ---- login ---- */

/* Build login packet `n` (0 or 1) into `pkt`; returns its length. */
static int build_login(AvObj *o, int n, int w, const char *account, const char *password,
                       int cred_mode, uint8_t hs, uint8_t *pkt) {
    int len = 2 * w + (n ? 0x3a : 0x38);

    memset(pkt, 0, (size_t)len);
    pkt[1] = n ? 0x20 : 0x00;
    av_put16(pkt + 2, AV_VERSION);
    av_put16(pkt + 0x10, (uint16_t)(2 * w + (n ? 0x22 : 0x20)));
    pkt[0x12] = hs;
    av_put32(pkt + 0x14, o->nonce + (uint32_t)n);
    strncpy((char *)pkt + 0x18, account, (size_t)w - 1);
    if (cred_mode == CRED_MODE_SHA256)
        TUTK3rdSHA256(password, (int)strlen(password), pkt + 0x18 + w, 32);
    else
        strncpy((char *)pkt + 0x18 + w, password, (size_t)w - 1);
    pkt[0x18 + 2 * w] = hs;
    memcpy(pkt + 0x1c + 2 * w, av_opcode_support, sizeof av_opcode_support);
    pkt[0x30 + 2 * w] = (uint8_t)cred_mode;
    pkt[0x34 + 2 * w] = 1;
    return len;
}

/* The camera's 0x21 reply: login state at +0x18 for our nonce at +0x14. */
static void on_login_reply(AvObj *o, const uint8_t *p, int len) {
    uint32_t nonce;

    if (len < 0x1c)
        return;
    nonce = av_get32(p + 0x14);
    pthread_mutex_lock(&o->lock);
    if (o->login_state < 0 && (nonce == o->nonce || nonce == o->nonce + 1)) {
        o->login_state = p[0x18];
    }
    pthread_mutex_unlock(&o->lock);
}

/* Release o->lock for a short sleep, then retake it; caller holds o->lock. Returns 0 once
 * `deadline_ms` (av_now_ms clock) has passed. Condition variables are not used: the patched
 * library's mutexes live in the companion's side table, which pthread_cond_* would bypass. */
int av_wait(AvObj *o, uint32_t deadline_ms) {
    int32_t left = (int32_t)(deadline_ms - av_now_ms());

    if (left <= 0)
        return 0;
    pthread_mutex_unlock(&o->lock);
    usleep((useconds_t)(left < WAIT_STEP_MS ? left : WAIT_STEP_MS) * 1000);
    pthread_mutex_lock(&o->lock);
    return 1;
}

/* ---- the read task ---- */

void *av_read_task(void *arg) {
    AvObj *o = arg;
    uint8_t buf[AV_MAX_PACKET];

    tutk_platform_set_thread_name("avConnReadThread");
    for (;;) {
        unsigned int datatype = 0;
        int n, err;

        pthread_mutex_lock(&o->lock);
        err = o->stopping ? 1 : av_check(o);
        pthread_mutex_unlock(&o->lock);
        if (err)
            break;
        n = av_chan_read(&o->chan, buf, sizeof buf, &datatype);
        if (n == 0)
            continue;
        if (n < 0) {
            pthread_mutex_lock(&o->lock);
            err = av_check(o);
            pthread_mutex_unlock(&o->lock);
            if (err)
                break;
            usleep(10000);
            continue;
        }
        if (o->server && datatype == IOTC_DATATYPE_DTLS)
            av_serv_on_dtls(o);
        else if (o->server && n >= 2 && buf[0] == 0 && (buf[1] == 0x00 || buf[1] == 0x20))
            av_serv_on_login(o, buf, n);
        else if (o->server && o->login_state != 0)
            continue;                       /* left over from the channel's previous use */
        else if (n >= 2 && buf[0] == 0 && buf[1] == 0x21)
            on_login_reply(o, buf, n);
        else
            av_on_packet(o, buf, n);
    }
    pthread_mutex_lock(&o->lock);
    pthread_mutex_unlock(&o->lock);
    return NULL;
}

/* ---- start and stop ---- */

AvObj *av_obj_new(void) {
    AvObj *o = calloc(1, sizeof *o);

    if (!o)
        return NULL;
    pthread_mutex_init(&o->lock, NULL);
    o->login_state = -1;
    o->ctrl_acked = o->ctrl_in = o->pn_cum = o->pn_high = 0xffff;
    o->rtt_ms = 100;
    return o;
}

/* Stop the tasks and close the sub-channel; safe to call more than once. */
void av_obj_stop(AvObj *o) {
    pthread_mutex_lock(&o->lock);
    o->stopping = true;
    if (!o->error)
        o->error = AV_ER_CLIENT_EXIT;
    pthread_mutex_unlock(&o->lock);
    if (o->tx_started && !pthread_equal(o->tx_thread, pthread_self()))
        pthread_join(o->tx_thread, NULL);
    if (o->rx_started && !pthread_equal(o->rx_thread, pthread_self()))
        pthread_join(o->rx_thread, NULL);
    o->tx_started = o->rx_started = false;
}

void av_obj_free(AvObj *o) {
    av_obj_stop(o);
    av_chan_close(&o->chan);
    av_ctrl_free(o);
    av_media_free(o);
    pthread_mutex_destroy(&o->lock);
    free(o);
}

/* IOTC's close callback: the session went away under the channel. */
void av_on_session_closed(int sid, int channel, int reason, void *arg) {
    set_error(arg, reason == -23 ? AV_ER_REMOTE_TIMEOUT_DISCONNECT : AV_ER_SESSION_CLOSE_BY_REMOTE);
}

/* Map a sub-channel handshake failure into the AV space. */
static int handshake_error(AvObj *o, int rc) {
    int err;

    pthread_mutex_lock(&o->lock);
    err = av_check(o);
    pthread_mutex_unlock(&o->lock);
    if (err)
        return err;
    return rc == AV_ER_FAIL_CREATE_DTLS ? rc : AV_ER_TIMEOUT;
}

/* Send the login packets until the camera answers or `deadline` ticks pass. */
static int login(AvObj *o, const AvClientIn *in, int peer_version) {
    const uint8_t *f = (const uint8_t *)in;
    uint8_t hs = av_get32(f + 0x20) == 1;
    int cred_mode = (int)av_get32(f + 0x28);
    int w = peer_version < 9 ? 0x10 : 0x101;
    int deadline = peer_version ? peer_version * 1000 / LOGIN_POLL_MS + 2 : 1;
    uint8_t pkt[2][2 * 0x101 + 0x3a];
    int len[2];

    do
        o->nonce = (uint32_t)tutk_platform_rand();
    while (o->nonce == 0 || o->nonce == UINT32_MAX);
    for (int i = 0; i < 2; i++)
        len[i] = build_login(o, i, w, in->account, in->password, cred_mode, hs, pkt[i]);
    for (int tick = 0; tick < deadline; tick++) {
        int state, err;

        if (tick % LOGIN_RESEND_TICKS == 0) {
            av_chan_write(&o->chan, pkt[0], len[0]);
            if (hs)
                av_chan_write(&o->chan, pkt[1], len[1]);
        }
        pthread_mutex_lock(&o->lock);
        for (uint32_t until = av_now_ms() + LOGIN_POLL_MS;
             o->login_state < 0 && !o->error && !o->stopping && av_wait(o, until);)
            ;
        state = o->login_state;
        err = o->stopping ? AV_ER_CLIENT_EXIT : av_check(o);
        pthread_mutex_unlock(&o->lock);
        if (err)
            return err;
        if (state == 0)
            return 0;
        if (state > 0)
            return AV_ER_WRONG_VIEWACCorPWD;
    }
    return AV_ER_TIMEOUT;
}

TK_EXPORT int avClientStartEx(const AvClientIn *in, AvClientOut *out) {
    AvObj *o;
    int idx, rc, peer_version;
    bool dtls;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (!in || !out || in->size < 0x40 || out->size < 0x20 || in->channel >= 32)
        return AV_ER_INVALID_ARG;
    dtls = av_get32((const uint8_t *)in + 0x24) != 0;
    if (dtls && (!in->account || !in->password || !in->account[0] || !in->password[0] ||
                 strlen(in->account) > AV_ACCOUNT_MAX || strlen(in->password) > AV_PASSWORD_MAX))
        return AV_ER_INVALID_ARG;
    rc = IOTC_Check_Session_Status(in->iotc_session);
    if (rc == IOTC_ER_REMOTE_TIMEOUT_DISCONNECT)
        return AV_ER_REMOTE_TIMEOUT_DISCONNECT;
    if (rc == IOTC_ER_SESSION_CLOSE_BY_REMOTE)
        return AV_ER_SESSION_CLOSE_BY_REMOTE;
    if (rc < 0)
        return AV_ER_INVALID_SID;
    peer_version = IOTC_Get_Remote_ProtocolVersion((int)in->iotc_session);
    if (dtls && peer_version < 0x17)
        return AV_ER_REMOTE_NOT_SUPPORT_DTLS;

    o = av_obj_new();
    if (!o)
        return AV_ER_MEM_INSUFF;
    o->session_id = (int)in->iotc_session;
    o->channel = in->channel;
    rc = av_chan_open(&o->chan, o->session_id, o->channel, dtls, in->account, in->password);
    if (rc < 0) {
        av_obj_free(o);
        return rc;
    }
    idx = av_slot_new(o->session_id, o->channel, o);
    if (idx < 0) {
        av_obj_free(o);
        return idx;
    }
    rc = av_chan_connect(&o->chan, in->mode ? (int)in->mode : 10);
    if (rc < 0) {
        av_slot_release(idx);
        return handshake_error(o, rc);
    }
    if (pthread_create(&o->rx_thread, NULL, av_read_task, o) != 0) {
        av_slot_release(idx);
        return AV_ER_FAIL_CREATE_THREAD;
    }
    o->rx_started = true;
    rc = login(o, in, peer_version);
    if (rc == 0 && pthread_create(&o->tx_thread, NULL, av_tx_task, o) != 0)
        rc = AV_ER_FAIL_CREATE_THREAD;
    if (rc < 0) {
        av_slot_release(idx);
        return rc;
    }
    o->tx_started = true;
    IOTC_Session_Set_CloseCb_With_Arg(o->session_id, o->channel, av_on_session_closed, CLOSECB_MAGIC, o);
    memset(out->out, 0, sizeof out->out);
    out->out[0] = 0;                        /* server type */
    out->out[1] = av_get32((const uint8_t *)in + 0x20) == 1;
    out->out[2] = 1;                        /* video stream */
    out->out[4] = 1;                        /* audio stream */
    out->out[6] = dtls;
    return idx;
}

TK_EXPORT void avClientStop(int av_index) {
    AvObj *o = av_slot_acquire(av_index);

    if (!o)
        return;
    IOTC_Session_Set_CloseCb_With_Arg(o->session_id, o->channel, NULL, CLOSECB_MAGIC, NULL);
    av_obj_stop(o);
    av_slot_release(av_index);
    av_slot_release(av_index);
}
