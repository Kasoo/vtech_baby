/* Initialisation, the AV index table and the transport sub-channel (kit: av/10-init.md). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avInitialize
 */

typedef struct Slot {
    int      refcount;
    int      session_id;
    uint8_t  channel;
    AvObj   *obj;            /* NULL marks the slot free */
} Slot;

static pthread_mutex_t table_lock = PTHREAD_MUTEX_INITIALIZER;
static Slot *slots;
static int slot_count;
static volatile int initialized;

TK_EXPORT int avInitialize(unsigned int max_num_sessions) {
    int n = max_num_sessions ? (int)max_num_sessions : 1;

    pthread_mutex_lock(&table_lock);
    if (initialized) {
        pthread_mutex_unlock(&table_lock);
        return AV_ER_NOT_INITIALIZED;
    }
    slots = calloc((size_t)n, sizeof *slots);
    if (!slots) {
        pthread_mutex_unlock(&table_lock);
        return AV_ER_MEM_INSUFF;
    }
    for (int i = 0; i < n; i++) {
        slots[i].session_id = -1;
        slots[i].channel = 0xff;
    }
    slot_count = n;
    initialized = 1;
    pthread_mutex_unlock(&table_lock);
    return n;
}

bool av_initialized(void) { return initialized == 1; }

/* Claim a free slot for (session, channel) holding `obj`, with the start-time reference. */
int av_slot_new(int session_id, uint8_t channel, AvObj *obj) {
    int idx = AV_ER_EXCEED_MAX_CHANNEL;

    pthread_mutex_lock(&table_lock);
    for (int i = 0; i < slot_count; i++)
        if (slots[i].obj && slots[i].session_id == session_id && slots[i].channel == channel) {
            pthread_mutex_unlock(&table_lock);
            return AV_ER_IOTC_CHANNEL_IN_USED;
        }
    for (int i = 0; i < slot_count; i++)
        if (!slots[i].obj) {
            slots[i] = (Slot){.refcount = 1, .session_id = session_id, .channel = channel, .obj = obj};
            idx = i;
            break;
        }
    pthread_mutex_unlock(&table_lock);
    return idx;
}

AvObj *av_slot_acquire(int av_index) {
    AvObj *o = NULL;

    pthread_mutex_lock(&table_lock);
    if (initialized && av_index >= 0 && av_index < slot_count && slots[av_index].obj) {
        slots[av_index].refcount++;
        o = slots[av_index].obj;
    }
    pthread_mutex_unlock(&table_lock);
    return o;
}

/* Drop one reference; the last one turns the IOTC channel off and frees the object. */
void av_slot_release(int av_index) {
    AvObj *o = NULL;
    int sid = -1;
    uint8_t ch = 0;

    pthread_mutex_lock(&table_lock);
    if (av_index >= 0 && av_index < slot_count && slots[av_index].obj &&
        --slots[av_index].refcount <= 0) {
        o = slots[av_index].obj;
        sid = slots[av_index].session_id;
        ch = slots[av_index].channel;
        slots[av_index] = (Slot){.session_id = -1, .channel = 0xff};
    }
    pthread_mutex_unlock(&table_lock);
    if (o) {
        IOTC_Session_Channel_OFF((unsigned)sid, ch);
        av_obj_free(o);
    }
}

int av_slot_find(int session_id, uint8_t channel) {
    int idx = -1;

    pthread_mutex_lock(&table_lock);
    for (int i = 0; i < slot_count; i++)
        if (slots[i].obj && slots[i].session_id == session_id && slots[i].channel == channel)
            idx = i;
    pthread_mutex_unlock(&table_lock);
    return idx;
}

/* ---- The transport sub-channel ---- */

static unsigned int psk_client_cb(SSL *ssl, const char *hint, char *identity,
                                  unsigned int max_identity_len, unsigned char *psk,
                                  unsigned int max_psk_len, void *arg) {
    AvChan *c = arg;
    size_t n = strlen(c->identity);

    if (n + 1 > max_identity_len || sizeof c->key > max_psk_len)
        return 0;
    memcpy(identity, c->identity, n + 1);
    memcpy(psk, c->key, sizeof c->key);
    return sizeof c->key;
}

/* Set up the sub-channel. For DTLS the PSK identity is "AUTHPWD_" + account and the key is
 * SHA-256(password). */
int av_chan_open(AvChan *c, int session_id, uint8_t channel, bool dtls, const char *account,
                 const char *password) {
    int rc;

    memset(c, 0, sizeof *c);
    c->session_id = session_id;
    c->channel = channel;
    c->dtls = dtls;
    if (!dtls)
        return 0;
    if (!account || !password)
        return AV_ER_INVALID_ARG;
    snprintf(c->identity, sizeof c->identity, "AUTHPWD_%s", account);
    TUTK3rdSHA256(password, (int)strlen(password), c->key, sizeof c->key);
    rc = IOTC_sCHL_CTX_new((void *)1, &c->ctx);
    if (rc < 0 || !c->ctx)
        return AV_ER_SDK_NOT_SUPPORT_DTLS;
    IOTC_sCHL_CTX_set_psk_client_callback(c->ctx, (void (*)())psk_client_cb, c);
    return 0;
}

/* Turn the IOTC channel on and, for DTLS, run the client handshake. */
int av_chan_connect(AvChan *c, int timeout_s) {
    int rc = IOTC_Session_Channel_ON((unsigned)c->session_id, c->channel);

    if (rc < 0 || !c->dtls)
        return rc < 0 ? rc : 0;
    c->ssl = IOTC_sCHL_new(c->ctx, c->session_id, c->channel);
    if (!c->ssl)
        return AV_ER_FAIL_CREATE_DTLS;
    IOTC_sCHL_set_dtls_connect_timeout(c->ssl, timeout_s);
    return IOTC_sCHL_connect(c->ssl);
}

/* One packet, or 0 when none arrived within the poll interval. On a plain channel `datatype` is
 * IOTC's: 1 for a DTLS record. */
int av_chan_read(AvChan *c, void *buf, int len, unsigned int *datatype) {
    if (c->dtls)
        return c->ssl ? IOTC_sCHL_read(c->ssl, buf, len) : AV_ER_INVALID_ARG;
    return IOTC_Session_Read_Data_And_Datatype((unsigned)c->session_id, buf, (unsigned)len, 20, NULL,
                                               NULL, c->channel, datatype);
}

int av_chan_write(AvChan *c, const void *buf, int len) {
    if (c->dtls)
        return c->ssl ? IOTC_sCHL_write(c->ssl, buf, len) : AV_ER_INVALID_ARG;
    return IOTC_Session_Write(c->session_id, buf, (unsigned)len, c->channel);
}

void av_chan_close(AvChan *c) {
    if (c->ssl) {
        IOTC_sCHL_shutdown(c->ssl);
        IOTC_sCHL_free(c->ssl);
        c->ssl = NULL;
    }
    if (c->ctx) {
        IOTC_sCHL_CTX_remove_psk_client_callback(c->ctx);
        IOTC_sCHL_CTX_free(c->ctx);
        c->ctx = NULL;
    }
}
