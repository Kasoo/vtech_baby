/* The new-protocol transport: inbound dispatch, and the task that sends acknowledgements, reports,
 * RTT echoes and control-frame retransmissions (kit: av/90-fifos.md, av/70-congestion.md). */
#include <string.h>
#include <unistd.h>

#include "av.h"

#define TX_TICK_MS         10
#define ACK_IDLE_MS        110    /* an ACK at least this often, data or not */
#define REPORT_MS          200
#define ACK_LEN            0x18
#define NAK_MAX            32     /* missing packets named in one ACK */
#define NAK_REPEAT_MS      50     /* name a still-missing packet again after this long */
#define REPORT_LEN         0x10
#define RTT_LEN            0x14

static uint16_t now16(void) { return (uint16_t)av_now_ms(); }

/* A class-0x0b RTT packet echoing a report's timestamp is the camera's answer to our report. */
static void on_rtt(AvObj *o, const uint8_t *p, int len) {
    uint16_t rtt;

    if (len < 0x0a)
        return;
    rtt = (uint16_t)(now16() - av_get16(p + 8));
    pthread_mutex_lock(&o->lock);
    if (rtt < 5000)
        o->rtt_ms = rtt ? rtt : 1;
    pthread_mutex_unlock(&o->lock);
}

/* The camera's report: echo its timestamp in an RTT packet. */
static void on_report(AvObj *o, const uint8_t *p, int len) {
    if (len < 0x0a)
        return;
    pthread_mutex_lock(&o->lock);
    o->rtt_echo = av_get16(p + 8);
    o->rtt_due = true;
    pthread_mutex_unlock(&o->lock);
}

/* A class-0x0c packet: an error-correction group header at B, then a control frame (byte 0 = 0)
 * or a media fragment. Parity packets (kind 0x0c) are not used. */
static void on_data(AvObj *o, const uint8_t *p, int len) {
    int b = p[1] & FLAG_STATS ? 0x10 : 0x08;
    const uint8_t *f = p + b + 8;
    int flen = len - b - 8;

    if (flen < 0x14)
        return;
    if (f[0] == 0)
        av_ctrl_on_frame(o, f, flen);
    else if (f[0] != CLASS_DATA)
        av_media_on_packet(o, f, flen);
}

void av_on_packet(AvObj *o, const uint8_t *p, int len) {
    if (len < 8)
        return;
    switch (p[0]) {
    case CLASS_ACK:
        if (len >= 0x0e)
            av_ctrl_on_ack(o, av_get16(p + 0x0c));
        break;
    case CLASS_REPORT:
        on_report(o, p, len);
        break;
    case CLASS_RTT:
        on_rtt(o, p, len);
        break;
    case CLASS_DATA:
        on_data(o, p, len);
        break;
    default:
        tk_trace("av: ignored class 0x%02x len %d", p[0], len);
        break;
    }
}

/* ---- outbound ---- */

static void put_header(uint8_t *pkt, uint8_t cls, uint8_t flags) {
    pkt[0] = cls;
    pkt[1] = flags;
    av_put16(pkt + 2, AV_VERSION);
}

/* ACK: cumulative and highest media packet numbers, the camera's control frames received
 * contiguously, the NAK count, the reset flag, our ACK sequence, a timestamp, then the NAK list (missing
 * packets as offsets from the cumulative number), which asks the camera to resend them. */
static void send_ack(AvObj *o, uint32_t now) {
    uint8_t pkt[0x16 + 2 * NAK_MAX] = {0};
    uint16_t naks[NAK_MAX];
    int n = 0;

    put_header(pkt, CLASS_ACK, 0);
    pthread_mutex_lock(&o->lock);
    av_put16(pkt + 0x08, o->pn_cum);
    av_put16(pkt + 0x0a, o->pn_high);
    av_put16(pkt + 0x0c, o->ctrl_in);
    if (now - o->nak_ms >= NAK_REPEAT_MS && (n = av_media_naks(o, naks, NAK_MAX)) > 0) {
        o->nak_ms = now;
        o->naks_sent += (uint32_t)n;
    }
    av_put16(pkt + 0x0e, (uint16_t)n);
    av_put16(pkt + 0x10, o->ack_urgent);
    av_put16(pkt + 0x12, o->ack_seq++);
    o->ack_due = o->ack_urgent = false;
    pthread_mutex_unlock(&o->lock);
    av_put16(pkt + 0x14, now16());
    for (int i = 0; i < n; i++)
        av_put16(pkt + 0x16 + 2 * i, naks[i]);
    av_send(o, pkt, n ? 0x16 + 2 * n : ACK_LEN);
}

/* Report: the stats block alone, carrying our timestamp (echoed back in the camera's RTT packet)
 * and our RTT estimate. */
static void send_report(AvObj *o) {
    uint8_t pkt[REPORT_LEN] = {0};

    put_header(pkt, CLASS_REPORT, FLAG_STATS);
    av_put16(pkt + 0x08, now16());
    pthread_mutex_lock(&o->lock);
    av_put16(pkt + 0x0a, o->rtt_ms);
    pthread_mutex_unlock(&o->lock);
    av_send(o, pkt, sizeof pkt);
}

static void send_rtt(AvObj *o, uint16_t echo) {
    uint8_t pkt[RTT_LEN] = {0};

    put_header(pkt, CLASS_RTT, 0);
    av_put16(pkt + 0x08, echo);
    pthread_mutex_lock(&o->lock);
    av_put16(pkt + 0x0a, o->rtt_ms);
    av_put16(pkt + 0x0c, o->rtt_ms);
    pthread_mutex_unlock(&o->lock);
    av_send(o, pkt, sizeof pkt);
}

void *av_tx_task(void *arg) {
    AvObj *o = arg;

    tutk_platform_set_thread_name("avTxThread");
    send_report(o);
    o->report_ms = av_now_ms();
    for (;;) {
        uint32_t now = av_now_ms();
        bool ack, rtt;
        uint16_t echo;

        pthread_mutex_lock(&o->lock);
        if (o->stopping || o->error) {
            pthread_mutex_unlock(&o->lock);
            break;
        }
        ack = o->ack_due || now - o->ack_ms >= ACK_IDLE_MS;
        rtt = o->rtt_due;
        echo = o->rtt_echo;
        o->rtt_due = false;
        pthread_mutex_unlock(&o->lock);

        if (rtt)
            send_rtt(o, echo);
        if (now - o->report_ms >= REPORT_MS) {
            send_report(o);
            o->report_ms = now;
        }
        av_ctrl_retransmit(o, now);
        if (ack) {
            send_ack(o, now);
            o->ack_ms = now;
        }
        usleep(TX_TICK_MS * 1000);
    }
    return NULL;
}
