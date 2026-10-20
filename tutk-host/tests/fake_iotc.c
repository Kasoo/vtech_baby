/* A stand-in libIOTCAPIs.so for testing AV without a camera: the AV channel's packets go to a queue the
 * test reads (fake_take), and the test's packets come back through another (fake_push). The sCHL calls
 * pass plaintext through the same queues, and connect runs the PSK callback so the test can read the
 * identity and key AV offers. Only the calls AV makes are here. */
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))
#define QUEUE  512
#define PKT    0x600
#define READ_WAIT_MS 20

typedef struct { int len; unsigned int type; uint8_t data[PKT]; } Packet;
typedef struct { Packet slot[QUEUE]; int head, tail; } Queue;

typedef unsigned int (*PskCb)(void *ssl, const char *hint, char *identity, unsigned int max_identity,
                              unsigned char *psk, unsigned int max_psk, void *arg);
typedef void (*CloseCb)(int, int, int, void *);

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static Queue to_peer, from_peer;
static int status, version = 0x1b;
static unsigned int channel_on;       /* a bit per channel */
static PskCb psk_cb;
static void *psk_arg;
static char psk_identity[128];
static unsigned char psk_key[64];
static int psk_len;
static CloseCb close_cb;
static int close_magic;
static void *close_arg;
static int ctx_token, ssl_token;

static int put(Queue *q, const void *data, int len, unsigned int type) {
    int next = (q->tail + 1) % QUEUE;

    if (len > PKT || next == q->head)
        return -1;
    q->slot[q->tail].len = len;
    q->slot[q->tail].type = type;
    memcpy(q->slot[q->tail].data, data, (size_t)len);
    q->tail = next;
    return len;
}

static int get(Queue *q, void *buf, int size, unsigned int *type) {
    int n;

    if (q->head == q->tail)
        return 0;
    if (type)
        *type = q->slot[q->head].type;
    n = q->slot[q->head].len < size ? q->slot[q->head].len : size;
    memcpy(buf, q->slot[q->head].data, (size_t)n);
    q->head = (q->head + 1) % QUEUE;
    return n;
}

/* ---- the test's side ---- */

/* A packet from the camera; `type` is the IOTC datatype the reader sees (1 = DTLS record). */
EXPORT int fake_push(const void *data, int len, unsigned int type) {
    int rc;

    pthread_mutex_lock(&lock);
    rc = put(&from_peer, data, len, type);
    pthread_mutex_unlock(&lock);
    return rc;
}

EXPORT int fake_take(void *buf, int size) {
    int rc;

    pthread_mutex_lock(&lock);
    rc = get(&to_peer, buf, size, NULL);
    pthread_mutex_unlock(&lock);
    return rc;
}

EXPORT void fake_set_status(int s) { status = s; }
EXPORT void fake_set_version(int v) { version = v; }
EXPORT unsigned int fake_channel_on(void) { return channel_on; }
EXPORT int fake_close_magic(void) { return close_magic; }
EXPORT const char *fake_psk_identity(void) { return psk_identity; }
EXPORT int fake_psk_key(unsigned char *out) { memcpy(out, psk_key, sizeof psk_key); return psk_len; }

/* Runs the close callback AV registered, as IOTC does when the session closes. */
EXPORT int fake_close(int reason) {
    if (!close_cb)
        return -1;
    close_cb(0, 0, reason, close_arg);
    return 0;
}

/* ---- what AV imports ---- */

EXPORT int IOTC_Check_Session_Status(unsigned int sid) { return status; }
EXPORT int IOTC_Get_Remote_ProtocolVersion(int sid) { return version; }
EXPORT int IOTC_Session_Channel_ON(unsigned int sid, unsigned char ch) { channel_on |= 1 << ch; return 0; }
EXPORT int IOTC_Session_Channel_OFF(unsigned int sid, unsigned char ch) { channel_on &= ~(1 << ch); return 0; }

EXPORT void IOTC_Session_Set_CloseCb_With_Arg(int sid, uint8_t ch, CloseCb cb, int magic, void *arg) {
    close_cb = cb;
    close_magic = magic;
    close_arg = arg;
}

static int read_packet(void *buf, int size, unsigned int *type) {
    for (int waited = 0; waited < READ_WAIT_MS; waited++) {
        int n;

        pthread_mutex_lock(&lock);
        n = get(&from_peer, buf, size, type);
        pthread_mutex_unlock(&lock);
        if (n || status < 0)
            return n ? n : status;
        usleep(1000);
    }
    return 0;
}

static int write_packet(const void *data, int len) {
    int rc;

    if (status < 0)
        return status;
    pthread_mutex_lock(&lock);
    rc = put(&to_peer, data, len, 0);
    pthread_mutex_unlock(&lock);
    return rc;
}

EXPORT int IOTC_Session_Read_Data_And_Datatype(unsigned int sid, void *buf, unsigned int size,
                                               unsigned int timeout_ms, uint16_t *seq, char *lost,
                                               uint8_t ch, unsigned int *flags) {
    return read_packet(buf, (int)size, flags);
}

EXPORT int IOTC_Session_Write(int sid, const void *data, unsigned int len, uint8_t ch) {
    return write_packet(data, (int)len);
}

EXPORT int IOTC_sCHL_CTX_new(void *unused, void **ctx) { *ctx = &ctx_token; return 0; }
EXPORT void IOTC_sCHL_CTX_free(void *ctx) {}
EXPORT void IOTC_sCHL_CTX_set_psk_client_callback(void *ctx, PskCb cb, void *arg) { psk_cb = cb; psk_arg = arg; }
EXPORT void IOTC_sCHL_CTX_remove_psk_client_callback(void *ctx) { psk_cb = NULL; }
EXPORT void *IOTC_sCHL_new(void *ctx, int sid, int ch) { return &ssl_token; }
EXPORT int IOTC_sCHL_set_dtls_connect_timeout(void *ssl, int seconds) { return 0; }

EXPORT int IOTC_sCHL_connect(void *ssl) {
    if (psk_cb)
        psk_len = (int)psk_cb(ssl, NULL, psk_identity, sizeof psk_identity, psk_key, sizeof psk_key,
                              psk_arg);
    return 0;
}

EXPORT int IOTC_sCHL_read(void *ssl, void *buf, int len) { return read_packet(buf, len, NULL); }
EXPORT int IOTC_sCHL_write(void *ssl, const void *buf, int len) { return write_packet(buf, len); }
EXPORT void IOTC_sCHL_shutdown(void *ssl) {}
EXPORT void IOTC_sCHL_free(void *ssl) {}
