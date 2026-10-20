/* Talk-back server (kit: av/30-talkback.md). The camera connects to a channel we serve, logs in, and
 * receives our audio. On LAN-direct the channel is plain IOTC: the camera tries DTLS first and falls
 * back to the plain login when told so. */
#include <pthread.h>
#include <string.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avServStart3
 *   avServStop
 *   avServExit
 *   avServResetBuffer
 */

#define LOGIN_REPLY_LEN   0x3c
#define LOGIN_WAIT_MS     50

typedef int (*AuthFn)(const char *account, char *password);

/* A DTLS record on the plain channel: the camera's ClientHello. This answer makes it log in plainly. */
void av_serv_on_dtls(AvObj *o) {
    static const char refuse[] = "recv dtls data!";

    av_chan_write(&o->chan, refuse, sizeof refuse - 1);
}

/* The camera's login (sub-type 0x00, and 0x20 when the handshake-variant byte is set): check the
 * credentials and answer 0x21 for the packet that completes the pair. */
void av_serv_on_login(AvObj *o, const uint8_t *p, int len) {
    int w = IOTC_Get_Remote_ProtocolVersion(o->session_id) < 9 ? 0x10 : 0x101;
    char account[0x102], password[0x102];
    uint8_t reply[LOGIN_REPLY_LEN] = {0};
    int state;

    if (len < 2 * w + 0x38 || (p[1] == 0x00 && p[0x12] != 0))
        return;
    memcpy(account, p + 0x18, (size_t)w);
    memcpy(password, p + 0x18 + w, (size_t)w);
    account[w] = password[w] = 0;
    state = !o->auth || o->auth(account, password) ? 0 : 3;

    reply[1] = 0x21;
    av_put16(reply + 2, AV_VERSION);
    av_put16(reply + 0x10, 0x24);
    memcpy(reply + 0x14, p + 0x14, 4);                     /* the request's nonce */
    av_put32(reply + 0x18, (uint32_t)state);
    reply[0x1d] = reply[0x1f] = reply[0x20] = 1;
    memcpy(reply + 0x24, av_opcode_support, sizeof av_opcode_support);
    av_send(o, reply, sizeof reply);

    pthread_mutex_lock(&o->lock);
    if (o->login_state < 0)
        o->login_state = state;
    pthread_mutex_unlock(&o->lock);
}

/* Stop the tasks and drop the index: the table's reference and the caller's. */
static void serv_close(int av_index, AvObj *o) {
    IOTC_Session_Set_CloseCb_With_Arg(o->session_id, o->channel, NULL, CLOSECB_MAGIC, NULL);
    av_obj_stop(o);
    av_slot_release(av_index);
    av_slot_release(av_index);
}

TK_EXPORT int avServStart3(int session_id, AuthFn auth, unsigned int timeout_s, unsigned int serv_type,
                           uint8_t channel, int *resend) {
    static const uint8_t hello[4] = {0x00, 0x01, 0x00, 0x00};
    uint32_t deadline;
    AvObj *o;
    int idx, rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (session_id < 0 || channel >= 32)
        return AV_ER_INVALID_ARG;
    rc = IOTC_Check_Session_Status((unsigned)session_id);
    if (rc == IOTC_ER_REMOTE_TIMEOUT_DISCONNECT)
        return AV_ER_REMOTE_TIMEOUT_DISCONNECT;
    if (rc == IOTC_ER_SESSION_CLOSE_BY_REMOTE)
        return AV_ER_SESSION_CLOSE_BY_REMOTE;
    if (rc < 0)
        return AV_ER_INVALID_SID;

    o = av_obj_new();
    if (!o)
        return AV_ER_MEM_INSUFF;
    o->server = true;
    o->auth = auth;
    o->session_id = session_id;
    o->channel = channel;
    av_chan_open(&o->chan, session_id, channel, false, NULL, NULL);
    rc = av_chan_connect(&o->chan, 0);
    if (rc < 0) {
        av_obj_free(o);
        return AV_ER_INVALID_SID;
    }
    idx = av_slot_new(session_id, channel, o);
    if (idx < 0) {
        av_obj_free(o);
        return idx;
    }
    if (pthread_create(&o->rx_thread, NULL, av_read_task, o) != 0) {
        av_slot_release(idx);
        return AV_ER_FAIL_CREATE_THREAD;
    }
    o->rx_started = true;

    deadline = av_now_ms() + (timeout_s ? timeout_s : 1) * 1000;
    pthread_mutex_lock(&o->lock);
    while (o->login_state < 0 && !(rc = av_check(o)) && av_wait(o, deadline))
        ;
    if (!rc)
        rc = o->login_state == 0 ? 0 : o->login_state > 0 ? AV_ER_WRONG_VIEWACCorPWD : AV_ER_TIMEOUT;
    pthread_mutex_unlock(&o->lock);
    if (rc == 0)
        rc = av_ctrl_send(o, CTRL_INTERNAL, OP_HELLO, hello, sizeof hello, NULL);
    if (rc == 0 && pthread_create(&o->tx_thread, NULL, av_tx_task, o) != 0)
        rc = AV_ER_FAIL_CREATE_THREAD;
    if (rc < 0) {
        av_obj_stop(o);
        av_slot_release(idx);
        return rc;
    }
    o->tx_started = true;
    IOTC_Session_Set_CloseCb_With_Arg(session_id, channel, av_on_session_closed, CLOSECB_MAGIC, o);
    if (resend)
        *resend = 1;
    return idx;
}

TK_EXPORT void avServStop(int av_index) {
    AvObj *o = av_slot_acquire(av_index);

    if (o)
        serv_close(av_index, o);
}

/* Normally a no-op: avServStop has already freed the index. */
TK_EXPORT void avServExit(int session_id, uint8_t channel) {
    int idx = av_slot_find(session_id, channel);
    AvObj *o = idx >= 0 ? av_slot_acquire(idx) : NULL;

    if (o)
        serv_close(idx, o);
}

/* Audio is sent as it is given, so there is no queue to drop. */
TK_EXPORT int avServResetBuffer(int av_index, unsigned int sel, int arg) {
    AvObj *o;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    pthread_mutex_lock(&o->lock);
    rc = av_check(o);
    pthread_mutex_unlock(&o->lock);
    av_slot_release(av_index);
    return rc;
}
