/* Session lifecycle (kit: iotc/20-session.md, iotc/99-stubs.md). */
#include <ctype.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "iotc.h"

#define CLOSECB_MAGIC (-0x27955e4)
#define CLOSE_REASON  2
#define SINFO_CLASSIC  0x5c
#define SINFO_EXTENDED 0xa0

/* The connect input struct: its size, auth type, authKey, timeout and security mode. */
#define CIN_SIZE       0xa0
#define CIN_AUTH_TYPE  0x04
#define CIN_AUTHKEY    0x08
#define CIN_TIMEOUT    0x94
#define CIN_SECURITY   0x98
#define CONNECT_POLL_MS 10

TK_EXPORT int IOTC_Get_SessionID(void) {
    int sid = IOTC_ER_EXCEED_MAX_SESSION;

    if (iotc.init != INIT_UP)
        return IOTC_ER_NOT_INITIALIZED;
    iotc_lock();
    for (int i = 0; i < iotc.session_count; i++) {
        IotcSession *s = &iotc.sessions[i];

        if (s->state == SESSION_FREE) {
            iotc_session_defaults(s);
            s->state = SESSION_CONNECTING;
            s->session_id = i;
            sid = i;
            break;
        }
    }
    iotc_unlock();
    return sid;
}

TK_EXPORT int IOTC_Session_Check_Ex(int session_id, void *info) {
    uint32_t size;
    IotcSession *s;
    int rc;

    if (info == NULL)
        return IOTC_ER_INVALID_ARG;
    memcpy(&size, info, sizeof size);
    if (size != SINFO_CLASSIC && size != SINFO_EXTENDED)
        return IOTC_ER_INVALID_ARG;
    memset(info, 0, size);
    rc = iotc_session_enter(session_id, &s);
    if (rc != 0)
        return rc;
    ((st_IOTCSessInfo *)info)->mode = s->mode;
    iotc_unlock();
    return 0;
}

TK_EXPORT int IOTC_Session_Check(int session_id, void *info) {
    uint8_t block[SINFO_CLASSIC] = {SINFO_CLASSIC};
    int rc = IOTC_Session_Check_Ex(session_id, block);

    if (rc == 0 && info)
        memcpy(info, block, sizeof block);
    return rc;
}

TK_EXPORT int IOTC_Get_Remote_ProtocolVersion(int session_id) {
    IotcSession *s;
    int rc = iotc_session_enter(session_id, &s);

    if (rc != 0)
        return rc;
    rc = s->remote_proto_version;
    iotc_unlock();
    return rc;
}

TK_EXPORT int IOTC_GetAuthenticationType(unsigned int session_id) {
    IotcSession *s;
    int rc = iotc_session_enter((int)session_id, &s);

    if (rc != 0)
        return rc;
    rc = s->auth_type;
    iotc_unlock();
    return rc;
}

TK_EXPORT void IOTC_Session_Set_CloseCb_With_Arg(int session_id, uint8_t channel,
        void (*cb)(int, int, int, void *), int magic, void *arg) {
    IotcSession *s;

    if (magic != CLOSECB_MAGIC || channel >= IOTC_MAX_CHANNELS)
        return;
    if (iotc_session_enter(session_id, &s) != 0)
        return;
    s->close_cb_witharg[channel] = cb;
    s->close_cb_arg[channel] = arg;
    iotc_unlock();
}

TK_EXPORT void IOTC_Session_Lock(void) { iotc_lock(); }
TK_EXPORT void IOTC_Session_unLock(void) { iotc_unlock(); }

static uint32_t get_u32(const uint8_t *p) {
    uint32_t v;

    memcpy(&v, p, sizeof v);
    return v;
}

static int64_t now_ms(void) {
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static bool valid_input(const char *uid, const uint8_t *in) {
    if (uid == NULL || strlen(uid) == 0 || strlen(uid) > IOTC_UID_LEN || in == NULL)
        return false;
    if (get_u32(in) != CIN_SIZE || get_u32(in + CIN_AUTH_TYPE) != 0 || get_u32(in + CIN_SECURITY) >= 3)
        return false;
    for (int i = 0; i < IOTC_AUTHKEY_LEN; i++)
        if (!isalnum(in[CIN_AUTHKEY + i]))
            return false;
    return true;
}

/* A fresh token for each connect; the camera only echoes it back. */
static void new_token(uint8_t token[IOTC_TOKEN_LEN]) {
    for (int i = 0; i < IOTC_TOKEN_LEN; i += 2) {
        uint16_t r = (uint16_t)tutk_platform_rand();

        token[i] = (uint8_t)r;
        token[i + 1] = (uint8_t)(r >> 8);
    }
}

/* LAN-direct only: broadcast the search until the camera's connect response settles the session,
 * then return the session id, the camera's rejection, or IOTC_ER_TIMEOUT after the input's timeout.
 * The slot stays allocated on failure, for the caller's IOTC_Session_Close. */
TK_EXPORT int IOTC_Connect_ByUIDEx(const char *uid, int session_id, const uint8_t *input) {
    IotcSession *s;
    int64_t deadline;
    int rc;

    if (iotc.init != INIT_UP)
        return IOTC_ER_NOT_INITIALIZED;
    if (!valid_input(uid, input))
        return IOTC_ER_INVALID_ARG;
    if (session_id < 0 || session_id >= iotc.session_count)
        return IOTC_ER_INVALID_SID;
    rc = iotc_open_transport();
    if (rc != 0)
        return rc;

    iotc_lock();
    s = &iotc.sessions[session_id];
    if (s->state != SESSION_CONNECTING || s->lan_state != 0) {
        iotc_unlock();
        return IOTC_ER_INVALID_SID;
    }
    for (int i = 0; uid[i]; i++)
        s->uid[i] = (char)toupper((unsigned char)uid[i]);
    memcpy(s->authkey, input + CIN_AUTHKEY, IOTC_AUTHKEY_LEN);
    new_token(s->token);
    s->connect_error = 0;
    s->lan_state = LAN_SEARCH_RUNNING;
    iotc_unlock();
    if (AddLanSearchTask(s) != 0)
        return IOTC_ER_RESOURCE_ERROR;

    deadline = now_ms() + (int64_t)get_u32(input + CIN_TIMEOUT) * 1000;
    for (;;) {
        iotc_lock();
        rc = iotc.init != INIT_UP                ? IOTC_ER_NOT_INITIALIZED
           : s->state == SESSION_ESTABLISHED      ? session_id
           : s->connect_error                     ? s->connect_error
           : s->state != SESSION_CONNECTING       ? IOTC_ER_INVALID_SID
           : now_ms() >= deadline                 ? IOTC_ER_TIMEOUT
           : 1;
        iotc_unlock();
        if (rc != 1)
            break;
        usleep(CONNECT_POLL_MS * 1000);
    }
    SessionTaskCleanNodesWithType(&s->tasks, 0);
    tk_trace("iotc: connect %d -> %d", session_id, rc);
    return rc;
}

void iotc_fire_close_callbacks(int sid, int reason) {
    void (*cb[IOTC_MAX_CHANNELS])(int, int, int);
    void (*cb_arg[IOTC_MAX_CHANNELS])(int, int, int, void *);
    void *arg[IOTC_MAX_CHANNELS];
    IotcSession *s = &iotc.sessions[sid];

    iotc_lock();
    memcpy(cb, s->close_cb, sizeof cb);
    memcpy(cb_arg, s->close_cb_witharg, sizeof cb_arg);
    memcpy(arg, s->close_cb_arg, sizeof arg);
    iotc_unlock();
    for (int ch = 0; ch < IOTC_MAX_CHANNELS; ch++) {
        if (cb[ch])
            cb[ch](sid, ch, reason);
        if (cb_arg[ch])
            cb_arg[ch](sid, ch, reason, arg[ch]);
    }
}

TK_EXPORT void IOTC_Session_Close(int session_id) {
    IotcSession *s;

    if (iotc.init != INIT_UP || session_id < 0 || session_id >= iotc.session_count)
        return;
    s = &iotc.sessions[session_id];
    if (s->state == SESSION_FREE)
        return;
    /* Tasks first and without the table lock: a running task holds the task manager's lock while it
     * waits for the table lock. */
    SessionTaskCleanNodesWithType(&s->tasks, 0);
    SessionTaskCleanNodesWithType(&s->tasks, 0xd);
    iotc_lock();
    if (s->state == SESSION_ESTABLISHED)
        iotc_send_close(s);
    iotc_unlock();
    iotc_fire_close_callbacks(session_id, CLOSE_REASON);
    iotc_lock();
    iotc_session_free_queues(s);
    iotc_session_defaults(s);
    iotc_unlock();
}

/* Nebula is never attached to a session on these paths, so both report an invalid argument. */
TK_EXPORT int get_nebula_client_info(unsigned int session_id, char *out_a, unsigned int len_a,
                                     char *out_b, unsigned int len_b) {
    return IOTC_ER_INVALID_ARG;
}

TK_EXPORT int get_nebula_device_identity_auth(unsigned int session_id) { return IOTC_ER_INVALID_ARG; }

/* Device-side provisioning; a client never reaches it. */
TK_EXPORT int IOTC_Device_Update_Authkey(void *params) {
    tk_trace("IOTC_Device_Update_Authkey: not supported");
    return IOTC_ER_INVALID_ARG;
}
