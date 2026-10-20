/* The DTLS secure channel (kit: iotc/50-dtls.md, iotc/99-stubs.md). OpenSSL runs over an IOTC
 * channel through a custom BIO; the per-connection state hangs off the BIO's data pointer. */
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

#include "iotc.h"

#define SCHL_ER_RESOURCE (-50004)
#define SCHL_ER_TIMEOUT  (-50006)

#define OPENSSL_INIT_LOAD_SSL_STRINGS 0x00200000L
#define BIO_TYPE_SOURCE_SINK          0x0400
#define BIO_CTRL_FLUSH                11
#define BIO_CTRL_DGRAM_QUERY_MTU      40
#define SSL_ERROR_WANT_READ           2
#define SSL_ERROR_WANT_WRITE          3
#define HAS_DATA_POLL_MS              200
#define BIO_READ_TIMEOUT_MS           20
#define DTLS_TIMER_FIRST_US           500000

typedef struct {
    pthread_mutex_t lock;
    TList          *list;
} PskRegistry;

static BIO_METHOD *bio_method;
static PskRegistry psk_client, psk_server;

int IOTC_Set_Partial_Encryption(unsigned int session_id, char enable);

static uint32_t now_ms(void) {
    struct timeval tv;

    gettimeofday(&tv, NULL);
    return (uint32_t)((uint64_t)tv.tv_sec * 1000 + (uint64_t)tv.tv_usec / 1000);
}

static TSchlConn *conn_of(SSL *ssl) {
    BIO *b = ssl ? tutk_third_SSL_get_rbio(ssl) : NULL;

    return b ? tutk_third_BIO_get_data(b) : NULL;
}

/* ---- the custom BIO ---- */

static int bio_write(BIO *b, const char *data, int len) {
    TSchlConn *c = tutk_third_BIO_get_data(b);
    int rc = IOTC_Session_Write_DTLS(c->session_id, data, (unsigned int)len,
                                     (uint8_t)c->channel);

    return rc < 0 ? -1 : rc;
}

static int bio_read(BIO *b, char *buf, int len) {
    TSchlConn *c = tutk_third_BIO_get_data(b);
    unsigned int n = len < IOTC_MAX_PAYLOAD ? (unsigned)len : IOTC_MAX_PAYLOAD;
    int rc = IOTC_Session_Read_Check_Lost_Data_And_Datatype((unsigned)c->session_id, buf, n,
                                                            BIO_READ_TIMEOUT_MS, NULL, NULL,
                                                            (uint8_t)c->channel, NULL);

    return rc == 0 ? IOTC_ER_TIMEOUT : rc;
}

static int bio_puts(BIO *b, const char *s) { return bio_write(b, s, (int)strlen(s)); }
static int bio_gets(BIO *b, char *buf, int size) { return -1; }

static long bio_ctrl(BIO *b, int cmd, long num, void *ptr) {
    return cmd == BIO_CTRL_FLUSH ? 1 : cmd == BIO_CTRL_DGRAM_QUERY_MTU ? IOTC_MAX_PAYLOAD : 0;
}

static int bio_create(BIO *b) {
    tutk_third_BIO_set_init(b, 1);
    return 1;
}

static int bio_destroy(BIO *b) { return 1; }

static BIO_METHOD *BIO_custom_new(void) {
    BIO_METHOD *m = tutk_third_BIO_meth_new(tutk_third_BIO_get_new_index() | BIO_TYPE_SOURCE_SINK,
                                            "bio_method");

    if (m == NULL)
        return NULL;
    if (!tutk_third_BIO_meth_set_write(m, bio_write) || !tutk_third_BIO_meth_set_read(m, bio_read) ||
        !tutk_third_BIO_meth_set_puts(m, bio_puts) || !tutk_third_BIO_meth_set_gets(m, bio_gets) ||
        !tutk_third_BIO_meth_set_ctrl(m, bio_ctrl) || !tutk_third_BIO_meth_set_create(m, bio_create) ||
        !tutk_third_BIO_meth_set_destroy(m, bio_destroy)) {
        tutk_third_BIO_meth_free(m);
        return NULL;
    }
    return m;
}

/* The DTLS retransmit timer: 0.5 s, then double the previous interval. */
static unsigned int dtls_timer_cb(SSL *s, unsigned int us) {
    return us == 0 ? DTLS_TIMER_FIRST_US : us * 2;
}

/* ---- global setup ---- */

static int registry_init(PskRegistry *r) {
    tutk_platform_mutex_init(&r->lock);
    r->list = tlistNew();
    return r->list ? 0 : -1;
}

int IOTC_sCHL_initialize(void) {
    int ok;

    if (bio_method == NULL && (bio_method = BIO_custom_new()) == NULL)
        return SCHL_ER_RESOURCE;
    if (psk_client.list == NULL && registry_init(&psk_client) != 0)
        return SCHL_ER_RESOURCE;
    if (psk_server.list == NULL && registry_init(&psk_server) != 0)
        return SCHL_ER_RESOURCE;
    ok = tutk_third_OPENSSL_init_ssl(OPENSSL_INIT_LOAD_SSL_STRINGS, NULL);
    return translate_Error(ok ? 0 : 1, NULL);
}

/* ---- the PSK registries ---- */

static TSchlPskEntry *registry_find(PskRegistry *r, SSL_CTX *ctx) {
    for (TListNode *n = tlistHead(r->list); n; n = n->next)
        if (((TSchlPskEntry *)n->data)->ctx == ctx)
            return n->data;
    return NULL;
}

/* Record cb/arg for ctx; returns true when a new record was added. */
static bool registry_set(PskRegistry *r, SSL_CTX *ctx, void (*cb)(), void *arg) {
    TSchlPskEntry *e;
    bool added = false;

    pthread_mutex_lock(&r->lock);
    e = registry_find(r, ctx);
    if (e == NULL && (e = calloc(1, sizeof *e)) != NULL) {
        e->ctx = ctx;
        tlistAppend(r->list, e);
        added = true;
    }
    if (e) {
        e->callback = cb;
        e->arg = arg;
    }
    pthread_mutex_unlock(&r->lock);
    return added;
}

static void registry_remove(PskRegistry *r, SSL_CTX *ctx) {
    TSchlPskEntry *e;

    if (r->list == NULL)
        return;
    pthread_mutex_lock(&r->lock);
    e = registry_find(r, ctx);
    if (e)
        tlistRemove(r->list, e);
    pthread_mutex_unlock(&r->lock);
    free(e);
}

/* Look up ctx's record and copy it out under the lock. */
static bool registry_get(PskRegistry *r, SSL_CTX *ctx, TSchlPskEntry *out) {
    TSchlPskEntry *e;

    pthread_mutex_lock(&r->lock);
    e = registry_find(r, ctx);
    if (e)
        *out = *e;
    pthread_mutex_unlock(&r->lock);
    return e && out->callback;
}

typedef unsigned int (*user_psk_client_cb)(SSL *, const char *, char *, unsigned int, unsigned char *,
                                           unsigned int, void *);
typedef unsigned int (*user_psk_server_cb)(SSL *, const char *, unsigned char *, unsigned int, void *);

static unsigned int sCHL_psk_client_callback(SSL *ssl, const char *hint, char *identity,
                                             unsigned int max_identity_len, unsigned char *psk,
                                             unsigned int max_psk_len) {
    TSchlPskEntry e;

    if (!registry_get(&psk_client, tutk_third_SSL_get_SSL_CTX(ssl), &e))
        return 0;
    return ((user_psk_client_cb)e.callback)(ssl, hint, identity, max_identity_len, psk, max_psk_len,
                                            e.arg);
}

static unsigned int sCHL_psk_server_callback(SSL *ssl, const char *identity, unsigned char *psk,
                                             unsigned int max_psk_len) {
    TSchlPskEntry e;

    if (!registry_get(&psk_server, tutk_third_SSL_get_SSL_CTX(ssl), &e))
        return 0;
    return ((user_psk_server_cb)e.callback)(ssl, identity, psk, max_psk_len, e.arg);
}

TK_EXPORT void IOTC_sCHL_CTX_set_psk_client_callback(SSL_CTX *ctx, void (*cb)(), void *arg) {
    if (psk_client.list && registry_set(&psk_client, ctx, cb, arg))
        tutk_third_SSL_CTX_set_psk_client_callback(ctx, sCHL_psk_client_callback);
}

TK_EXPORT void IOTC_sCHL_CTX_remove_psk_client_callback(SSL_CTX *ctx) {
    registry_remove(&psk_client, ctx);
}

TK_EXPORT void IOTC_sCHL_CTX_set_psk_server_callback(SSL_CTX *ctx, void (*cb)(), void *arg) {
    if (psk_server.list && registry_set(&psk_server, ctx, cb, arg))
        tutk_third_SSL_CTX_set_psk_server_callback(ctx, sCHL_psk_server_callback);
}

TK_EXPORT void IOTC_sCHL_CTX_remove_psk_server_callback(SSL_CTX *ctx) {
    registry_remove(&psk_server, ctx);
}

/* ---- context ---- */

TK_EXPORT int IOTC_sCHL_CTX_new(void *unused, SSL_CTX **ctx_out) {
    *ctx_out = tutk_third_SSL_CTX_new(tutk_third_DTLS_method());
    return *ctx_out ? 0 : SCHL_ER_RESOURCE;
}

TK_EXPORT void IOTC_sCHL_CTX_free(SSL_CTX *ctx) { tutk_third_SSL_CTX_free(ctx); }

TK_EXPORT int IOTC_sCHL_CTX_set_cipher_list(SSL_CTX *ctx, const char *ciphers) {
    return tutk_third_SSL_CTX_set_cipher_list(ctx, ciphers);
}

/* ---- connection ---- */

TK_EXPORT SSL *IOTC_sCHL_new(SSL_CTX *ctx, int session_id, int channel) {
    TSchlConn *c = calloc(1, sizeof *c);
    BIO *b = NULL;
    SSL *ssl = NULL;

    if (c == NULL)
        return NULL;
    if (tutk_platform_mutex_init(&c->io_lock) != 0) {
        free(c);
        return NULL;
    }
    b = tutk_third_BIO_new(bio_method);
    if (b)
        ssl = tutk_third_SSL_new(ctx);
    if (ssl == NULL) {
        if (b)
            tutk_third_BIO_free(b);
        tutk_platform_mutex_destroy(&c->io_lock);
        free(c);
        return NULL;
    }
    tutk_third_SSL_set_quiet_shutdown(ssl, 1);
    tutk_third_DTLS_set_timer_cb(ssl, dtls_timer_cb);
    c->session_id = session_id;
    c->channel = channel;
    c->ssl = ssl;
    IOTC_Set_Partial_Encryption((unsigned)session_id, 1);
    tutk_third_BIO_set_data(b, c);
    tutk_third_SSL_set_bio(ssl, b, b);
    return ssl;
}

TK_EXPORT int IOTC_sCHL_set_dtls_connect_timeout(SSL *ssl, int seconds) {
    TSchlConn *c = conn_of(ssl);
    long long ms = (long long)seconds * 1000;

    if (c)
        c->timeout_ms = ms > INT_MAX ? INT_MAX : (uint32_t)ms;
    return 0;
}

/* Run SSL_connect or SSL_accept until it completes, fails or the stored timeout passes. */
static int handshake(SSL *ssl, int (*step)(SSL *)) {
    TSchlConn *c = conn_of(ssl);

    if (c == NULL)
        return SCHL_ER_RESOURCE;
    c->handshake_start_ms = (int32_t)now_ms();
    for (;;) {
        int rc = step(ssl), err;

        if (rc == 1) {
            c->connected = 1;
            return 0;
        }
        err = tutk_third_SSL_get_error(ssl, rc);
        if (err != SSL_ERROR_WANT_READ && err != SSL_ERROR_WANT_WRITE && err != 5)
            return translate_Error((unsigned)err, ssl);
        if (now_ms() - (uint32_t)c->handshake_start_ms >= c->timeout_ms)
            return SCHL_ER_TIMEOUT;
    }
}

TK_EXPORT int IOTC_sCHL_connect(SSL *ssl) { return handshake(ssl, tutk_third_SSL_connect); }
TK_EXPORT int IOTC_sCHL_accept(SSL *ssl) { return handshake(ssl, tutk_third_SSL_accept); }

TK_EXPORT int IOTC_sCHL_read(SSL *ssl, void *buf, int len) {
    TSchlConn *c = conn_of(ssl);
    int rc;

    if (c == NULL)
        return SCHL_ER_RESOURCE;
    if (!IOTC_Session_Has_Data(c->session_id, (unsigned)c->channel & 0xff, HAS_DATA_POLL_MS))
        return 0;
    pthread_mutex_lock(&c->io_lock);
    rc = tutk_third_SSL_read(ssl, buf, len);
    if (rc <= 0)
        rc = translate_Error((unsigned)tutk_third_SSL_get_error(ssl, rc), ssl);
    pthread_mutex_unlock(&c->io_lock);
    return rc;
}

TK_EXPORT int IOTC_sCHL_write(SSL *ssl, const void *buf, int len) {
    TSchlConn *c = conn_of(ssl);
    int rc;

    if (c == NULL)
        return SCHL_ER_RESOURCE;
    pthread_mutex_lock(&c->io_lock);
    rc = tutk_third_SSL_write(ssl, buf, len);
    if (rc < 0)
        rc = translate_Error((unsigned)tutk_third_SSL_get_error(ssl, rc), ssl);
    pthread_mutex_unlock(&c->io_lock);
    return rc;
}

TK_EXPORT void IOTC_sCHL_shutdown(SSL *ssl) {
    TSchlConn *c = conn_of(ssl);
    int rc;

    if (c == NULL)
        return;
    if (!c->connected) {
        c->shutdown_requested = 1;
        return;
    }
    rc = tutk_third_SSL_shutdown(ssl);
    c->shutdown_done = 1;
    if (rc < 0)
        translate_Error((unsigned)tutk_third_SSL_get_error(ssl, rc), ssl);
}

TK_EXPORT void IOTC_sCHL_free(SSL *ssl) {
    TSchlConn *c = conn_of(ssl);

    if (c) {
        tutk_platform_mutex_destroy(&c->io_lock);
        free(c);
    }
    tutk_third_SSL_free(ssl);
}
