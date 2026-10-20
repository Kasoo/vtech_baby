/* Internal declarations shared by AV's source files. */
#ifndef AV_H
#define AV_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>

#include "av_types.h"
#include "tk_common.h"
#include "tutk_shared.h"

/* ---- Global's and IOTC's exports AV calls ---- */
typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_st SSL;

int   TUTK3rdSHA256(const void *data, int len, uint8_t *out, unsigned int out_size);
long  tutk_platform_rand(void);
void  tutk_platform_set_thread_name(const char *name);
void  tutk_platform_srand(void);

int   IOTC_Check_Session_Status(unsigned int session_id);
int   IOTC_Get_Remote_ProtocolVersion(int session_id);
int   IOTC_Session_Channel_ON(unsigned int session_id, unsigned char channel);
int   IOTC_Session_Channel_OFF(unsigned int session_id, unsigned char channel);
void  IOTC_Session_Set_CloseCb_With_Arg(int session_id, uint8_t channel,
                                        void (*cb)(int, int, int, void *), int magic, void *arg);
int   IOTC_Session_Read_Data_And_Datatype(unsigned int session_id, void *buf, unsigned int buf_size,
                                          unsigned int timeout_ms, uint16_t *out_seq, char *out_lost,
                                          uint8_t channel, unsigned int *out_flags);
int   IOTC_Session_Write(int session_id, const void *data, unsigned int len, uint8_t channel);
int   IOTC_sCHL_CTX_new(void *unused, SSL_CTX **ctx_out);
void  IOTC_sCHL_CTX_free(SSL_CTX *ctx);
int   IOTC_sCHL_CTX_set_cipher_list(SSL_CTX *ctx, const char *ciphers);
void  IOTC_sCHL_CTX_set_psk_client_callback(SSL_CTX *ctx, void (*cb)(), void *arg);
void  IOTC_sCHL_CTX_remove_psk_client_callback(SSL_CTX *ctx);
SSL  *IOTC_sCHL_new(SSL_CTX *ctx, int session_id, int channel);
int   IOTC_sCHL_set_dtls_connect_timeout(SSL *ssl, int seconds);
int   IOTC_sCHL_connect(SSL *ssl);
int   IOTC_sCHL_read(SSL *ssl, void *buf, int len);
int   IOTC_sCHL_write(SSL *ssl, const void *buf, int len);
void  IOTC_sCHL_shutdown(SSL *ssl);
void  IOTC_sCHL_free(SSL *ssl);

/* ---- Limits and wire constants ---- */
#define AV_MAX_PACKET        0x56c   /* largest AV packet the read task accepts */
#define AV_VERSION           0x0c    /* the protocol version this build sends */
#define AV_ACCOUNT_MAX       0x78
#define AV_PASSWORD_MAX      0x400
#define AV_PSK_IDENTITY_MAX  0x77

/* The camera rejected the login (absent from the kit's AV_ER enum). */
#define AV_ER_WRONG_VIEWACCorPWD  (-20009)

/* ---- The transport sub-channel: DTLS-PSK or plain IOTC ---- */
typedef struct AvChan {
    int      session_id;
    uint8_t  channel;
    bool     dtls;
    SSL_CTX *ctx;
    SSL     *ssl;
    char     identity[AV_PSK_IDENTITY_MAX + 1];
    uint8_t  key[32];
} AvChan;

int  av_chan_open(AvChan *c, int session_id, uint8_t channel, bool dtls, const char *account,
                  const char *password) TK_HIDDEN;
int  av_chan_connect(AvChan *c, int timeout_s) TK_HIDDEN;
int  av_chan_read(AvChan *c, void *buf, int len, unsigned int *datatype) TK_HIDDEN;
int  av_chan_write(AvChan *c, const void *buf, int len) TK_HIDDEN;
void av_chan_close(AvChan *c) TK_HIDDEN;

/* ---- Packet classes (byte 0) and control classes (byte +0x11 of a class-0x0c packet) ---- */
#define CLASS_LOGIN    0x00
#define CLASS_ACK      0x09
#define CLASS_REPORT   0x0a
#define CLASS_RTT      0x0b
#define CLASS_DATA     0x0c
#define CTRL_USER      0x70   /* the application's IOCtrl */
#define CTRL_INTERNAL  0x10   /* AV's own control messages */
#define FLAG_STATS     0x08   /* an 8-byte stats block follows the 8-byte header */
#define MEDIA_FRAG     1024   /* media payload bytes per fragment */
#define IOCTRL_MAX     0x3fc  /* the largest IOCtrl payload sent in one fragment */
#define OP_RESET       0x24   /* internal: reset the media packet numbering */
#define OP_RESET_ACK   0x25
#define OP_HELLO       0x40   /* internal: the server's first control frame */
#define IOTC_DATATYPE_DTLS 1  /* IOTC's datatype for a DTLS record */
#define CLOSECB_MAGIC  (-0x27955e4)

/* An outgoing control frame waiting for the camera's acknowledgement. */
typedef struct CtrlOut {
    struct CtrlOut *next;
    uint16_t        frame;
    uint32_t        sent_ms;
    int             len;
    uint8_t         pkt[];
} CtrlOut;

/* An inbound IOCtrl message waiting for avRecvIOCtrl. */
typedef struct CtrlIn {
    struct CtrlIn *next;
    uint32_t       type;
    int            len;
    uint8_t        data[];
} CtrlIn;

/* A media frame being assembled from fragments, or complete and queued. */
typedef struct Frame {
    struct Frame *next;
    uint32_t      number;
    uint32_t      first_ms;       /* when its first fragment arrived */
    uint8_t       kind;
    uint16_t      count;          /* fragments in the frame */
    uint16_t      have;           /* fragments received */
    uint16_t     *seen;           /* per fragment: its payload length + 1; 0 until it arrives */
    int           size;           /* -1 until the last fragment arrives; frame bytes once complete */
    uint8_t       info[16];       /* the frame-info trailer of the last fragment */
    int           info_len;
    uint8_t      *data;           /* count * MEDIA_FRAG bytes */
} Frame;

typedef struct Stream {
    Frame *partial;               /* frames still assembling, any order */
    Frame *ready, *ready_tail;    /* complete frames, oldest first */
    int    ready_count;
    int    lost;                  /* incomplete frames dropped since the last read */
    /* cumulative, for tk_av_recv_stats */
    uint32_t given_up;            /* frames dropped as incomplete */
    uint32_t late;                /* frames delivered after a resend was asked for while they waited */
    uint32_t overflow;            /* complete frames dropped because the reader fell behind */
} Stream;

/* ---- The AV object behind one AV index ---- */
typedef struct AvObj {
    AvChan          chan;
    int             session_id;
    uint8_t         channel;
    pthread_mutex_t lock;            /* guards everything below */
    int             login_state;     /* -1 waiting, 0 accepted, 3 rejected */
    int             error;           /* the AV_ER_* every call returns once the channel is down */
    bool            stopping;
    pthread_t       rx_thread;
    pthread_t       tx_thread;
    bool            rx_started, tx_started;
    uint32_t        nonce;
    uint16_t        tx_counter;      /* +0x04 of every counted packet sent */

    /* control frames out: our next frame number, the frames not yet acknowledged */
    uint16_t        ctrl_next;
    uint16_t        ctrl_acked;      /* the camera's cumulative ack of our frames; 0xffff = none */
    CtrlOut        *ctrl_out;
    /* control frames in: the highest contiguous camera frame, and the IOCtrl queue */
    uint16_t        ctrl_in;         /* 0xffff = none */
    CtrlIn         *ioctrl, *ioctrl_tail;
    uint16_t        reset_seq;       /* the last OP_RESET sent; its OP_RESET_ACK clears waiting */
    bool            reset_waiting;

    /* media packet numbering, as acknowledged */
    uint16_t        pn_cum;          /* every packet up to here received; 0xffff at start */
    uint16_t        pn_high;
    Stream          video, audio;
    void           *media_state;     /* media_recv.c's packet-number window */

    /* acknowledgement, report and RTT bookkeeping (tx task) */
    bool            ack_due;
    bool            ack_urgent;
    uint16_t        ack_seq;
    uint32_t        ack_ms, report_ms, nak_ms;
    uint32_t        naks_sent;       /* media packets asked for again, cumulative */
    struct FecState *fec;            /* fec.c's error-correction groups in progress */
    uint32_t        fec_recovered;   /* media packets rebuilt from parity, cumulative */
    bool            rtt_due;
    uint16_t        rtt_echo;        /* the camera report's timestamp to echo */
    uint16_t        rtt_ms;          /* our RTT estimate */

    /* talk-back server: the login check, and media send numbering */
    bool            server;
    int           (*auth)(const char *account, char *password);
    uint16_t        tx_pn;           /* next media packet number, from 0 */
    uint32_t        tx_frame;        /* next media frame number, from 0 */
    uint32_t        tx_audio;        /* audio frames sent */
} AvObj;

#define TK_AV_RECV_STATS 8    /* the counters tk_av_recv_stats fills */

static inline bool seq_after(uint16_t a, uint16_t b) { return (int16_t)(a - b) > 0; }

/* ---- The AV index table (init.c) ---- */
bool   av_initialized(void) TK_HIDDEN;
int    av_slot_new(int session_id, uint8_t channel, AvObj *obj) TK_HIDDEN;
AvObj *av_slot_acquire(int av_index) TK_HIDDEN;
void   av_slot_release(int av_index) TK_HIDDEN;
int    av_slot_find(int session_id, uint8_t channel) TK_HIDDEN;

/* ---- Session (session.c) ---- */
extern const uint8_t av_opcode_support[20] TK_HIDDEN;
AvObj *av_obj_new(void) TK_HIDDEN;
void   av_obj_stop(AvObj *o) TK_HIDDEN;
void   av_obj_free(AvObj *o) TK_HIDDEN;
void  *av_read_task(void *arg) TK_HIDDEN;
void   av_on_session_closed(int sid, int channel, int reason, void *arg) TK_HIDDEN;
int    av_send(AvObj *o, uint8_t *pkt, int len) TK_HIDDEN;
int    av_check(AvObj *o) TK_HIDDEN;
void   av_put16(uint8_t *p, uint16_t v) TK_HIDDEN;
void   av_put32(uint8_t *p, uint32_t v) TK_HIDDEN;
uint16_t av_get16(const uint8_t *p) TK_HIDDEN;
uint32_t av_get32(const uint8_t *p) TK_HIDDEN;
uint32_t av_now_ms(void) TK_HIDDEN;
int    av_wait(AvObj *o, uint32_t deadline_ms) TK_HIDDEN;

/* ---- Transport (transport.c), IOCtrl (ioctrl.c), media (media_recv.c) ---- */
void   av_on_packet(AvObj *o, const uint8_t *p, int len) TK_HIDDEN;
void  *av_tx_task(void *arg) TK_HIDDEN;
int    av_ctrl_send(AvObj *o, uint8_t ctrl_class, uint32_t type, const void *data, int len,
                    uint16_t *frame_out) TK_HIDDEN;
void   av_ctrl_retransmit(AvObj *o, uint32_t now) TK_HIDDEN;
void   av_ctrl_on_ack(AvObj *o, uint16_t acked) TK_HIDDEN;
void   av_ctrl_on_frame(AvObj *o, const uint8_t *f, int len) TK_HIDDEN;
void   av_media_on_packet(AvObj *o, const uint8_t *f, int len) TK_HIDDEN;
int    av_media_naks(AvObj *o, uint16_t *out, int max) TK_HIDDEN;
void   av_ctrl_free(AvObj *o) TK_HIDDEN;
int    av_media_reset(AvObj *o) TK_HIDDEN;
void   av_media_free(AvObj *o) TK_HIDDEN;

/* ---- Parity recovery (fec.c) ---- */
void   av_fec_on_packet(AvObj *o, const uint8_t *fec, const uint8_t *f, int flen) TK_HIDDEN;
void   av_fec_reset(AvObj *o) TK_HIDDEN;
void   av_fec_free(AvObj *o) TK_HIDDEN;

/* ---- Talk-back server (talkback.c) ---- */
void   av_serv_on_dtls(AvObj *o) TK_HIDDEN;
void   av_serv_on_login(AvObj *o, const uint8_t *p, int len) TK_HIDDEN;

#endif
