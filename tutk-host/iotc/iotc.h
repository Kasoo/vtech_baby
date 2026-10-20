/* Internal declarations shared by IOTC's source files. */
#ifndef IOTC_H
#define IOTC_H

#include <netinet/in.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "iotc_types.h"
#include "tk_common.h"
#include "tutk_shared.h"

/* ---- Global's exports IOTC calls ---- */
void    *tutk_platform_malloc(int size);
void    *tutk_platform_calloc(int count, int size);
void     tutk_platform_free(void *p);
int      tutk_platform_mutex_init(pthread_mutex_t *m);
int      tutk_platform_mutex_destroy(pthread_mutex_t *m);
void     tutk_platform_srand(void);
void     tk_lan_only_count_lookup(void);
long     tutk_platform_rand(void);
const char *tutk_platform_inet_ntop(int af, const void *src, char *dst, socklen_t size);
int      tutk_Net_GetInterfaceInfo(TNetInterfaceInfo *info);
int      GetLicenseKeyMask(uint8_t *out, int size);
int      TUTK3rdSHA1HMAC(const void *key, int key_len, const void *data, int data_len, uint8_t *out,
                         unsigned int out_size);
int      TConnManager_create(TConnManager **out);
int      tutk_TaskMng_Init(void);
TTask   *tutk_TaskMng_Create(int interval_ms, int lifetime_ms, uint16_t tag, TTaskCb cb, void *arg);
int      tutk_TaskMng_Delete(TTask *task);
TList   *tlistNew(void);
int      tlistAppend(TList *list, void *data);
TListNode *tlistHead(TList *list);
int      tlistRemove(TList *list, void *data);

/* ---- OpenSSL, through Global's tutk_third_ forwarders; every handle is opaque ---- */
typedef struct bio_st BIO;
typedef struct bio_method_st BIO_METHOD;
typedef struct ssl_method_st SSL_METHOD;
typedef struct ssl_cipher_st SSL_CIPHER;
typedef unsigned int (*tk_psk_client_cb)(SSL *ssl, const char *hint, char *identity,
                                         unsigned int max_identity_len, unsigned char *psk,
                                         unsigned int max_psk_len);
typedef unsigned int (*tk_psk_server_cb)(SSL *ssl, const char *identity, unsigned char *psk,
                                         unsigned int max_psk_len);

int   tutk_third_OPENSSL_init_ssl(uint64_t opts, const void *settings);
int   tutk_third_BIO_get_new_index(void);
BIO_METHOD *tutk_third_BIO_meth_new(int type, const char *name);
void  tutk_third_BIO_meth_free(BIO_METHOD *m);
int   tutk_third_BIO_meth_set_write(BIO_METHOD *m, int (*fn)(BIO *, const char *, int));
int   tutk_third_BIO_meth_set_read(BIO_METHOD *m, int (*fn)(BIO *, char *, int));
int   tutk_third_BIO_meth_set_puts(BIO_METHOD *m, int (*fn)(BIO *, const char *));
int   tutk_third_BIO_meth_set_gets(BIO_METHOD *m, int (*fn)(BIO *, char *, int));
int   tutk_third_BIO_meth_set_ctrl(BIO_METHOD *m, long (*fn)(BIO *, int, long, void *));
int   tutk_third_BIO_meth_set_create(BIO_METHOD *m, int (*fn)(BIO *));
int   tutk_third_BIO_meth_set_destroy(BIO_METHOD *m, int (*fn)(BIO *));
BIO  *tutk_third_BIO_new(const BIO_METHOD *m);
int   tutk_third_BIO_free(BIO *b);
void *tutk_third_BIO_get_data(BIO *b);
void  tutk_third_BIO_set_data(BIO *b, void *data);
void  tutk_third_BIO_set_init(BIO *b, int init);
const SSL_METHOD *tutk_third_DTLS_method(void);
void  tutk_third_DTLS_set_timer_cb(SSL *s, unsigned int (*cb)(SSL *s, unsigned int us));
unsigned long tutk_third_ERR_get_error(void);
void  tutk_third_ERR_error_string_n(unsigned long e, char *buf, size_t len);
SSL_CTX *tutk_third_SSL_CTX_new(const SSL_METHOD *m);
void  tutk_third_SSL_CTX_free(SSL_CTX *ctx);
int   tutk_third_SSL_CTX_set_cipher_list(SSL_CTX *ctx, const char *s);
void  tutk_third_SSL_CTX_set_psk_client_callback(SSL_CTX *ctx, tk_psk_client_cb cb);
void  tutk_third_SSL_CTX_set_psk_server_callback(SSL_CTX *ctx, tk_psk_server_cb cb);
SSL  *tutk_third_SSL_new(SSL_CTX *ctx);
void  tutk_third_SSL_free(SSL *s);
SSL_CTX *tutk_third_SSL_get_SSL_CTX(const SSL *s);
BIO  *tutk_third_SSL_get_rbio(const SSL *s);
void  tutk_third_SSL_set_bio(SSL *s, BIO *rbio, BIO *wbio);
void  tutk_third_SSL_set_quiet_shutdown(SSL *s, int mode);
int   tutk_third_SSL_connect(SSL *s);
int   tutk_third_SSL_accept(SSL *s);
int   tutk_third_SSL_read(SSL *s, void *buf, int num);
int   tutk_third_SSL_write(SSL *s, const void *buf, int num);
int   tutk_third_SSL_shutdown(SSL *s);
int   tutk_third_SSL_get_error(const SSL *s, int ret);

/* ---- library state ---- */
#define IOTC_MAX_CHANNELS   32
#define IOTC_DEFAULT_SESSIONS 128
#define IOTC_MAX_PAYLOAD    0x56c
#define IOTC_UID_LEN        20
#define IOTC_AUTHKEY_LEN    8
#define IOTC_TOKEN_LEN      8
#define IOTC_HOST_LEN       128

enum { INIT_DOWN = 0, INIT_UP = 2, INIT_STOPPING = 3 };
/* The LAN-search state of a connecting session. */
enum { LAN_SEARCH_RUNNING = 1, LAN_SEARCH_BOUND = 6 };

enum { SESSION_FREE = 0, SESSION_CONNECTING = 1, SESSION_ESTABLISHED = 2, SESSION_CLOSED_BY_REMOTE = 3,
       SESSION_REMOTE_TIMEOUT = 4 };

typedef struct IotcSession {
    uint8_t      state;
    int          session_id;
    int          auth_type;                         /* returned by IOTC_GetAuthenticationType */
    uint8_t      mode;                              /* st_IOTCSessInfo.mode: 2 = LAN-direct */
    uint8_t      remote_proto_version;
    uint32_t     remote_iotc_version;              /* the camera's long LAN reply +0x30; St_SInfoEx +0x54 */
    bool         pe_capable;
    bool         pe_enabled;
    bool         chan_on[IOTC_MAX_CHANNELS];
    uint16_t     chan_rx[IOTC_MAX_CHANNELS];        /* next expected receive sequence */
    uint32_t     send_seq[IOTC_MAX_CHANNELS];
    uint32_t     rx_index[IOTC_MAX_CHANNELS];
    uint16_t     rx_count[IOTC_MAX_CHANNELS];
    TChanRxQueue rx_queue[IOTC_MAX_CHANNELS];
    bool         read_abort;
    void       (*close_cb[IOTC_MAX_CHANNELS])(int sid, int channel, int reason);
    void       (*close_cb_witharg[IOTC_MAX_CHANNELS])(int sid, int channel, int reason, void *arg);
    void        *close_cb_arg[IOTC_MAX_CHANNELS];
    TIotcNetAddr peer_addr;
    TConnection *transport;
    uint16_t     nonce_a, nonce_b;
    TSessionTaskList tasks;
    /* connect */
    char         uid[IOTC_UID_LEN + 1];             /* uppercased */
    char         authkey[IOTC_AUTHKEY_LEN];
    uint8_t      token[IOTC_TOKEN_LEN];             /* our session token; the camera echoes it */
    uint8_t      lan_state;                         /* LAN_SEARCH_* */
    int          connect_error;                     /* a terminal IOTC_ER code raised by the 0x602 handler */
    char         peer_host[IOTC_HOST_LEN];          /* the camera, as the transport reports it */
    uint16_t     peer_port;                         /* network byte order */
    uint8_t      silence_ticks;                     /* alive-task ticks since inbound traffic */
    uint8_t      alive_ticks;                       /* alive-task ticks, for the keepalive cadence */
} IotcSession;

typedef struct IotcState {
    volatile uint8_t init;
    int              session_count;
    IotcSession     *sessions;
    pthread_mutex_t  table_lock;                    /* recursive */
    pthread_mutex_t  task_lock;
    TConnManager    *conn_mgr;
    TConnection     *conn;                          /* the one bound UDP socket every session uses */
    TTask           *alive_task;
    uint16_t         listen_port;
    uint16_t         lan_search_port;
    uint8_t          key[16];                       /* the scramble key */
    uint16_t         nonce_base;
} IotcState;

extern IotcState iotc TK_HIDDEN;

/* Lock the table and check the library, the id and the slot state. On 0 the lock is held and
 * *out is the established session; otherwise the lock is released and the IOTC_ER code returned. */
int  iotc_session_enter(int sid, IotcSession **out) TK_HIDDEN;
void iotc_lock(void) TK_HIDDEN;
void iotc_unlock(void) TK_HIDDEN;
void iotc_session_defaults(IotcSession *s) TK_HIDDEN;
void iotc_session_free_queues(IotcSession *s) TK_HIDDEN;

/* ---- wire.c ---- */
#define IOTC_HEADER_LEN     16
#define IOTC_SUBHEADER_LEN  12
#define IOTC_PE_PREFIX      0x40                    /* bytes scrambled under partial encryption */
#define IOTC_PROBE_LEN      0x58
#define IOTC_VERSION        0x1d                    /* header +0x02; the camera sends 0x1b */
#define IOTC_SDK_VERSION    TK_SDK_VERSION

enum {
    IOTC_FLAG_PE        = 0x01,
    IOTC_FLAG_SCRAMBLED = 0x02,
    IOTC_FLAG_SUBHEADER = 0x08,
};

enum {
    OP_PUNCH_ACK    = 0x0402,
    OP_DATA_OUT     = 0x0407,
    OP_DATA_IN      = 0x0408,
    OP_CLOSE_OUT    = 0x0417,
    OP_CLOSE_IN     = 0x0418,
    OP_ALIVE_OUT    = 0x0427,
    OP_ALIVE_IN     = 0x0428,
    OP_LAN_SEARCH   = 0x0601,
    OP_LAN_REPLY    = 0x0602,
};

uint16_t iotc_get16(const uint8_t *p) TK_HIDDEN;
void iotc_put16(uint8_t *p, uint16_t v) TK_HIDDEN;
void iotc_put32(uint8_t *p, uint32_t v) TK_HIDDEN;
void iotc_build_key(void) TK_HIDDEN;
void iotc_scramble(const uint8_t key[16], const void *in, void *out, int len, int prefix) TK_HIDDEN;
void iotc_unscramble(const uint8_t key[16], const void *in, void *out, int len, int prefix) TK_HIDDEN;
void iotc_put_header(uint8_t *pkt, uint8_t flags, uint16_t length, uint16_t opcode, uint16_t field_a)
    TK_HIDDEN;
int  iotc_build_lan_probe(uint8_t out[IOTC_PROBE_LEN]) TK_HIDDEN;

/* ---- handshake.c ---- */
#define IOTC_RECORD_LEN     0x34
int  iotc_build_connect_probe(const IotcSession *s, uint8_t out[IOTC_PROBE_LEN]) TK_HIDDEN;
int  iotc_build_lan_record(const IotcSession *s, uint8_t out[IOTC_RECORD_LEN]) TK_HIDDEN;
/* Send one plaintext IOTC datagram to the session's peer, scrambled. */
int  iotc_send_to_peer(const IotcSession *s, uint8_t *pkt, int len) TK_HIDDEN;
void iotc_send_connect_request(const IotcSession *s) TK_HIDDEN;
void iotc_send_keepalive(const IotcSession *s) TK_HIDDEN;
void iotc_send_close(const IotcSession *s) TK_HIDDEN;

/* ---- inbound.c ---- */
void iotc_on_datagram(TConnection *c, const char *from_host, uint16_t from_port, const void *data,
                      long len, void *arg) TK_HIDDEN;

/* ---- discovery.c ---- */
int  iotc_open_transport(void) TK_HIDDEN;
void Broadcast_Message_By_Netmask(TConnection *conn, const void *packet, unsigned int len,
                                  uint16_t port) TK_HIDDEN;
int  AddLanSearchTask(IotcSession *s) TK_HIDDEN;

/* ---- session.c ---- */
/* Run every registered close callback of a session with `reason`; call without the table lock. */
void iotc_fire_close_callbacks(int sid, int reason) TK_HIDDEN;

/* ---- nat.c ---- */
extern TNatTrack iotc_nat TK_HIDDEN;
void initNatTrack(TNatTrack *nat) TK_HIDDEN;
void clearNatRecord(TNatTrack *nat) TK_HIDDEN;
char getNatType(TNatTrack *nat) TK_HIDDEN;
void forEachNat(TNatTrack *nat, void (*fn)(void *ctx, TNatRecord *rec, uint16_t port, void *body),
                void *ctx) TK_HIDDEN;
void iotcUpdateNonce(uint16_t *out) TK_HIDDEN;

/* ---- tasks.c ---- */
int  SessionTaskAdd(TSessionTaskList *list, TTask *task, int type) TK_HIDDEN;
void SessionTaskCleanNodesWithType(TSessionTaskList *list, int type) TK_HIDDEN;
void SessionTaskDeleteNode(TSessionTaskList *list, uint64_t task_handle, char delete_task) TK_HIDDEN;

/* ---- addresses.c ---- */
int  iotc_netaddr_get_content(const TIotcNetAddr *addr, int text_size, char *text, uint16_t *port,
                              char *family) TK_HIDDEN;
int  iotc_netaddr_put_content(const char *host, int port, TIotcNetAddr *addr) TK_HIDDEN;
void UpdateLocalNetworkInfo(void) TK_HIDDEN;

/* ---- dtls.c ---- */
int  IOTC_sCHL_initialize(void) TK_HIDDEN;

/* ---- errors.c ---- */
int  translate_Error(unsigned int source, SSL *ssl) TK_HIDDEN;

/* ---- data.c ---- */
int  __Fill_ReadBuf(int session_id, const void *data, uint16_t len, uint16_t seq, uint8_t channel,
                    uint8_t flags) TK_HIDDEN;
int  IOTC_Session_Write_Data_And_Datatype(int session_id, const void *data, unsigned int len,
                                          uint8_t channel, unsigned int datatype) TK_HIDDEN;
int  IOTC_Session_Write_DTLS(int session_id, const void *data, unsigned int len, uint8_t channel);
int  IOTC_Session_Has_Data(int session_id, unsigned int channel, unsigned int timeout_ms) TK_HIDDEN;
int  IOTC_Session_Read_Check_Lost_Data_And_Datatype(unsigned int session_id, void *buf,
        unsigned int buf_size, unsigned int timeout_ms, uint16_t *out_seq, char *out_lost,
        uint8_t channel, unsigned int *out_flags) TK_HIDDEN;

/* ---- tryport.c ---- */
int  IOTC_TryPortInitialize(void *conn_mgr, void *iface) TK_HIDDEN;
int  IOTC_TryPortGetInterface(void **iface_out) TK_HIDDEN;

#endif
