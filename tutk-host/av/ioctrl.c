/* IOCtrl, and the reliable control frames it rides on (kit: av/40-ioctrl.md). */
#include <stdlib.h>
#include <string.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avRecvIOCtrl
 *   avSendIOCtrl
 */

#define CTRL_HEADER        0x18   /* control-frame header, from the frame's first byte */
#define CTRL_OFFSET        0x10   /* the frame's offset in the packet: header + group header */
#define RETRANSMIT_MS      60
#define RESET_WAIT_MS      1000

/* Queue one control frame and send it; it is resent until the camera acknowledges it. */
int av_ctrl_send(AvObj *o, uint8_t ctrl_class, uint32_t type, const void *data, int len,
                 uint16_t *frame_out) {
    int plen = CTRL_OFFSET + CTRL_HEADER + len;
    CtrlOut *c = calloc(1, sizeof *c + (size_t)plen), **tail;
    uint8_t copy[CTRL_OFFSET + CTRL_HEADER + IOCTRL_MAX];
    uint8_t *f;

    if (!c)
        return AV_ER_MEM_INSUFF;
    c->len = plen;
    c->pkt[0] = CLASS_DATA;
    av_put16(c->pkt + 2, AV_VERSION);
    f = c->pkt + CTRL_OFFSET;
    f[1] = ctrl_class;
    av_put32(f + 4, 1);
    av_put32(f + 8, (uint32_t)len + 4);
    av_put32(f + 0x14, type);
    if (len)
        memcpy(f + CTRL_HEADER, data, (size_t)len);

    pthread_mutex_lock(&o->lock);
    c->frame = o->ctrl_next++;
    av_put16(f + 2, c->frame);
    av_put32(f + 0x0c, c->frame);
    c->sent_ms = av_now_ms();
    for (tail = &o->ctrl_out; *tail; tail = &(*tail)->next)
        ;
    *tail = c;
    memcpy(copy, c->pkt, (size_t)plen);
    if (frame_out)
        *frame_out = c->frame;
    pthread_mutex_unlock(&o->lock);
    av_send(o, copy, plen);
    return 0;
}

/* Resend every unacknowledged frame whose last send is RETRANSMIT_MS old. */
void av_ctrl_retransmit(AvObj *o, uint32_t now) {
    for (;;) {
        uint8_t copy[CTRL_OFFSET + CTRL_HEADER + IOCTRL_MAX];
        int len = 0;

        pthread_mutex_lock(&o->lock);
        for (CtrlOut *c = o->ctrl_out; c; c = c->next)
            if (now - c->sent_ms >= RETRANSMIT_MS) {
                c->sent_ms = now;
                len = c->len;
                memcpy(copy, c->pkt, (size_t)len);
                break;
            }
        pthread_mutex_unlock(&o->lock);
        if (!len)
            return;
        av_send(o, copy, len);
    }
}

/* The camera has every frame of ours up to `acked`. An ACK that does not advance still trims: a frame
 * queued after an earlier ACK of the same number is covered by it. */
void av_ctrl_on_ack(AvObj *o, uint16_t acked) {
    CtrlOut **pp = &o->ctrl_out;

    if (acked == 0xffff)
        return;
    pthread_mutex_lock(&o->lock);
    if (o->ctrl_acked == 0xffff || seq_after(acked, o->ctrl_acked))
        o->ctrl_acked = acked;
    while (*pp) {
        CtrlOut *c = *pp;

        if (!seq_after(c->frame, o->ctrl_acked)) {
            *pp = c->next;
            free(c);
        } else {
            pp = &c->next;
        }
    }
    pthread_mutex_unlock(&o->lock);
}

/* A control frame from the camera. Frames are taken in order only; the camera resends anything
 * not acknowledged. */
void av_ctrl_on_frame(AvObj *o, const uint8_t *f, int len) {
    uint16_t frame = av_get16(f + 2);
    uint32_t body = av_get32(f + 8), type;
    int dlen;

    pthread_mutex_lock(&o->lock);
    o->ack_due = true;
    if (frame != (uint16_t)(o->ctrl_in + 1) || body < 4 || (int)body > len - 0x14 ||
        av_get32(f + 4) != 1) {
        if (av_get32(f + 4) != 1)
            tk_trace("av: dropped a %u-fragment control frame", av_get32(f + 4));
        pthread_mutex_unlock(&o->lock);
        return;
    }
    o->ctrl_in = frame;
    type = av_get32(f + 0x14);
    dlen = (int)body - 4;
    if (f[1] == CTRL_USER) {
        CtrlIn *m = malloc(sizeof *m + (size_t)dlen);

        if (m) {
            m->next = NULL;
            m->type = type;
            m->len = dlen;
            memcpy(m->data, f + CTRL_HEADER, (size_t)dlen);
            if (o->ioctrl_tail)
                o->ioctrl_tail->next = m;
            else
                o->ioctrl = m;
            o->ioctrl_tail = m;
        }
    } else if (f[1] == CTRL_INTERNAL && type == OP_RESET_ACK && dlen >= 2 &&
               av_get16(f + CTRL_HEADER) == o->reset_seq) {
        o->reset_waiting = false;
    }
    pthread_mutex_unlock(&o->lock);
}

void av_ctrl_free(AvObj *o) {
    while (o->ctrl_out) {
        CtrlOut *c = o->ctrl_out;

        o->ctrl_out = c->next;
        free(c);
    }
    while (o->ioctrl) {
        CtrlIn *m = o->ioctrl;

        o->ioctrl = m->next;
        free(m);
    }
    o->ioctrl_tail = NULL;
}

/* Reset the media packet numbering: tell the camera the base to number from and wait for its
 * answer. Caller does not hold o->lock. */
int av_media_reset(AvObj *o) {
    uint8_t msg[8] = {0};
    uint32_t deadline;
    int rc;

    pthread_mutex_lock(&o->lock);
    o->reset_seq++;
    o->pn_cum = o->pn_high = (uint16_t)(o->pn_cum + 0x4000);
    o->ack_urgent = o->ack_due = true;
    o->reset_waiting = true;
    av_put16(msg, o->reset_seq);
    av_put16(msg + 2, o->pn_cum);
    pthread_mutex_unlock(&o->lock);
    rc = av_ctrl_send(o, CTRL_INTERNAL, OP_RESET, msg, sizeof msg, NULL);
    if (rc < 0)
        return rc;
    deadline = av_now_ms() + RESET_WAIT_MS;
    pthread_mutex_lock(&o->lock);
    while (o->reset_waiting && !o->error && av_wait(o, deadline))
        ;
    rc = o->error;
    pthread_mutex_unlock(&o->lock);
    return rc;
}

TK_EXPORT int avSendIOCtrl(int av_index, unsigned int io_ctrl_type, const char *data, int data_size) {
    AvObj *o;
    uint16_t frame;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (data_size < 0 || (data_size && !data))
        return AV_ER_INVALID_ARG;
    if (io_ctrl_type < 0x100)
        return AV_ER_NOT_SUPPORT;
    if (data_size > IOCTRL_MAX)
        return AV_ER_EXCEED_MAX_SIZE;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    pthread_mutex_lock(&o->lock);
    rc = av_check(o);
    pthread_mutex_unlock(&o->lock);
    if (rc == 0)
        rc = av_ctrl_send(o, CTRL_USER, io_ctrl_type, data, data_size, &frame);
    if (rc == 0) {
        pthread_mutex_lock(&o->lock);
        while (!(rc = av_check(o)) && !o->stopping &&
               (o->ctrl_acked == 0xffff || seq_after(frame, o->ctrl_acked)))
            av_wait(o, av_now_ms() + 50);
        if (!rc && o->stopping)
            rc = AV_ER_CLIENT_EXIT;
        pthread_mutex_unlock(&o->lock);
    }
    av_slot_release(av_index);
    return rc;
}

TK_EXPORT int avRecvIOCtrl(int av_index, unsigned int *io_ctrl_type, char *data, int data_size,
                           unsigned int timeout_ms) {
    AvObj *o;
    CtrlIn *m = NULL;
    uint32_t deadline = av_now_ms() + timeout_ms;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (data_size < 0 || !io_ctrl_type || !data)
        return AV_ER_INVALID_ARG;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    pthread_mutex_lock(&o->lock);
    for (;;) {
        if (o->ioctrl) {
            m = o->ioctrl;
            o->ioctrl = m->next;
            if (!o->ioctrl)
                o->ioctrl_tail = NULL;
            break;
        }
        if ((rc = av_check(o)) != 0)
            break;
        if (!timeout_ms) {
            rc = AV_ER_DATA_NOREADY;
            break;
        }
        if (!av_wait(o, deadline)) {
            rc = AV_ER_TIMEOUT;
            break;
        }
    }
    pthread_mutex_unlock(&o->lock);
    if (m) {
        rc = m->len < data_size ? m->len : data_size;
        *io_ctrl_type = m->type;
        memcpy(data, m->data, (size_t)rc);
        free(m);
    }
    av_slot_release(av_index);
    return rc;
}
