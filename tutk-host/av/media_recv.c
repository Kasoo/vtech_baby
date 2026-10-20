/* Media receive: fragment assembly, the frame queues and their readers (kit: av/50-media-recv.md). */
#include <stdlib.h>
#include <string.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avClientCleanAudioBuf
 *   avClientCleanBuf (not in the drop-in surface; probe_restart.py binds it)
 *   avClientCleanVideoBuf
 *   avRecvAudioData
 *   avRecvFrameData2
 */

#define KIND_AUDIO        0x03
#define SUB_LAST          0x01    /* sub-flags: the frame's last fragment */
#define MAX_FRAGS         2048    /* a 2 MiB frame */
#define VIDEO_QUEUE       60
#define AUDIO_QUEUE       200
#define GAP_SKIP_MS       300     /* acknowledge past a missing packet after this long */
#define PARTIAL_WAIT_MS   500     /* how long an incomplete frame holds back the frames after it */

/* Received media packet numbers ahead of pn_cum, by pn & (WINDOW - 1). */
#define WINDOW            0x1000

typedef struct MediaState {
    uint8_t  seen[WINDOW / 8];
    uint32_t gap_ms;              /* when pn_cum last stalled behind pn_high; 0 = no gap */
} MediaState;

static bool bit(const MediaState *m, uint16_t pn) { return m->seen[(pn % WINDOW) / 8] >> (pn % 8) & 1; }
static void set_bit(MediaState *m, uint16_t pn, bool v) {
    if (v)
        m->seen[(pn % WINDOW) / 8] |= (uint8_t)(1 << (pn % 8));
    else
        m->seen[(pn % WINDOW) / 8] &= (uint8_t)~(1 << (pn % 8));
}

static void frame_free(Frame *f) {
    if (f) {
        free(f->seen);
        free(f->data);
        free(f);
    }
}

static void stream_clear(Stream *s) {
    while (s->partial) {
        Frame *f = s->partial;

        s->partial = f->next;
        frame_free(f);
    }
    while (s->ready) {
        Frame *f = s->ready;

        s->ready = f->next;
        frame_free(f);
    }
    s->ready_tail = NULL;
    s->ready_count = 0;
    s->lost = 0;
}

void av_media_free(AvObj *o) {
    stream_clear(&o->video);
    stream_clear(&o->audio);
    free(o->media_state);
    o->media_state = NULL;
}

/* Track packet `pn` for the ACK: advance pn_cum over every contiguous packet received, and past
 * a gap that has stayed open for GAP_SKIP_MS. Returns false for a packet already counted. */
static bool track_pn(AvObj *o, MediaState *m, uint16_t pn) {
    uint32_t now = av_now_ms();

    if (!seq_after(pn, o->pn_cum) || (uint16_t)(pn - o->pn_cum) >= WINDOW)
        return false;
    if (bit(m, pn))
        return false;
    set_bit(m, pn, true);
    if (seq_after(pn, o->pn_high))
        o->pn_high = pn;
    while (bit(m, (uint16_t)(o->pn_cum + 1))) {
        o->pn_cum++;
        set_bit(m, o->pn_cum, false);
    }
    if (o->pn_cum == o->pn_high) {
        m->gap_ms = 0;
    } else if (!m->gap_ms) {
        m->gap_ms = now ? now : 1;
    } else if (now - m->gap_ms >= GAP_SKIP_MS) {
        tk_trace("av: skipping lost media packet 0x%04x", (uint16_t)(o->pn_cum + 1));
        do
            o->pn_cum++;
        while (o->pn_cum != o->pn_high && !bit(m, (uint16_t)(o->pn_cum + 1)));
        while (bit(m, (uint16_t)(o->pn_cum + 1))) {
            o->pn_cum++;
            set_bit(m, o->pn_cum, false);
        }
        m->gap_ms = o->pn_cum == o->pn_high ? 0 : now;
    }
    return true;
}

/* The media packets still missing between pn_cum and pn_high, as offsets from pn_cum, for the ACK's NAK
 * list; at most `max`. Caller holds o->lock. */
int av_media_naks(AvObj *o, uint16_t *out, int max) {
    MediaState *m = o->media_state;
    int n = 0;

    if (!m)
        return 0;
    for (uint16_t pn = (uint16_t)(o->pn_cum + 1); seq_after(o->pn_high, pn) && n < max; pn++)
        if (!bit(m, pn))
            out[n++] = (uint16_t)(pn - o->pn_cum);
    return n;
}

/* Move frames to the ready queue in frame order: the oldest goes once complete, or is dropped as lost
 * once it has waited PARTIAL_WAIT_MS for a missing fragment (the camera resends what the ACK's NAK list
 * names, under a new packet number). Caller holds o->lock. */
static void release(Stream *s, int limit, uint32_t now) {
    for (;;) {
        Frame **oldest = NULL, *f;

        for (Frame **pp = &s->partial; *pp; pp = &(*pp)->next)
            if (!oldest || (int32_t)((*pp)->number - (*oldest)->number) < 0)
                oldest = pp;
        if (!oldest)
            return;
        f = *oldest;
        if (f->have < f->count || f->size < 0) {
            if (now - f->first_ms < PARTIAL_WAIT_MS)
                return;
            *oldest = f->next;
            frame_free(f);
            s->lost++;
            continue;
        }
        *oldest = f->next;
        f->next = NULL;
        /* Fragments are stored MEDIA_FRAG apart; any but the last may be shorter, so close the gaps. */
        for (int i = 0; i < f->count; i++) {
            memmove(f->data + f->size, f->data + (size_t)i * MEDIA_FRAG, (size_t)(f->seen[i] - 1));
            f->size += f->seen[i] - 1;
        }
        if (s->ready_tail)
            s->ready_tail->next = f;
        else
            s->ready = f;
        s->ready_tail = f;
        if (++s->ready_count > limit) {
            Frame *old = s->ready;

            s->ready = old->next;
            if (!s->ready)
                s->ready_tail = NULL;
            s->ready_count--;
            frame_free(old);
            s->lost++;
        }
    }
}

/* One media fragment: kind, sub-flags, packet number, fragment count, fragment index (or, on the
 * last fragment, the frame-info length), payload length, checksum, frame number, stream count,
 * then the payload; the last fragment's payload ends with the frame info. */
void av_media_on_packet(AvObj *o, const uint8_t *f, int len) {
    uint8_t kind = f[0], sub = f[1];
    uint16_t pn = av_get16(f + 2), count = av_get16(f + 4), w6 = av_get16(f + 6);
    int plen = av_get16(f + 8);
    uint32_t number = av_get32(f + 0x0c);
    bool last = sub & SUB_LAST;
    uint16_t index = last ? (uint16_t)(count - 1) : w6;
    int info_len = last ? w6 : 0;
    Stream *s = kind == KIND_AUDIO ? &o->audio : &o->video;
    MediaState *m;
    Frame *fr;

    if (plen > len - 0x14 || !count || count > MAX_FRAGS || index >= count || info_len > plen ||
        info_len > (int)sizeof fr->info || plen - info_len > MEDIA_FRAG)
        return;
    pthread_mutex_lock(&o->lock);
    o->ack_due = true;
    if (!o->media_state)
        o->media_state = calloc(1, sizeof(MediaState));
    m = o->media_state;
    if (!m || !track_pn(o, m, pn))
        goto out;
    for (fr = s->partial; fr && fr->number != number; fr = fr->next)
        ;
    if (!fr) {
        fr = calloc(1, sizeof *fr);
        if (!fr)
            goto out;
        fr->number = number;
        fr->first_ms = av_now_ms();
        fr->kind = kind;
        fr->count = count;
        fr->size = -1;
        fr->seen = calloc(count, sizeof *fr->seen);
        fr->data = malloc((size_t)count * MEDIA_FRAG);
        if (!fr->seen || !fr->data) {
            frame_free(fr);
            goto out;
        }
        fr->next = s->partial;
        s->partial = fr;
    }
    if (fr->count != count || fr->seen[index])
        goto out;
    fr->seen[index] = (uint16_t)(plen - info_len + 1);
    fr->have++;
    memcpy(fr->data + (size_t)index * MEDIA_FRAG, f + 0x14, (size_t)(plen - info_len));
    if (last) {
        fr->size = 0;
        fr->info_len = info_len;
        memcpy(fr->info, f + 0x14 + plen - info_len, (size_t)info_len);
    }
    release(s, s == &o->audio ? AUDIO_QUEUE : VIDEO_QUEUE, av_now_ms());
out:
    pthread_mutex_unlock(&o->lock);
}

/* Take the oldest complete frame of `s` into the caller's buffers. Returns its size or an AV_ER. */
static int take_frame(int av_index, bool audio, char *buf, int buf_size, int *out_actual,
                      int *out_expected, char *info, int info_size, int *out_info,
                      unsigned int *out_number) {
    AvObj *o;
    Frame *f = NULL;
    Stream *s;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (!buf || buf_size < 0 || !info || info_size < 0 || !out_number)
        return AV_ER_INVALID_ARG;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    s = audio ? &o->audio : &o->video;
    pthread_mutex_lock(&o->lock);
    release(s, audio ? AUDIO_QUEUE : VIDEO_QUEUE, av_now_ms());
    if ((rc = av_check(o)) != 0) {
    } else if (s->lost) {
        s->lost--;
        rc = AV_ER_LOSED_THIS_FRAME;
    } else if (!s->ready) {
        rc = AV_ER_DATA_NOREADY;
    } else {
        f = s->ready;
        s->ready = f->next;
        if (!s->ready)
            s->ready_tail = NULL;
        s->ready_count--;
    }
    pthread_mutex_unlock(&o->lock);
    if (f) {
        if (out_expected)
            *out_expected = f->size;
        if (f->size > buf_size) {
            rc = AV_ER_BUFPARA_MAXSIZE_INSUFF;
        } else {
            int n = f->info_len < info_size ? f->info_len : info_size;

            memcpy(buf, f->data, (size_t)f->size);
            memcpy(info, f->info, (size_t)n);
            if (out_actual)
                *out_actual = f->size;
            if (out_info)
                *out_info = n;
            *out_number = f->number;
            rc = f->size;
        }
        frame_free(f);
    }
    av_slot_release(av_index);
    return rc;
}

TK_EXPORT int avRecvFrameData2(int av_index, char *buf, int buf_size, int *out_actual,
                               int *out_expected, char *info, int info_size, int *out_info,
                               unsigned int *out_number) {
    if (!out_actual || !out_expected || !out_info)
        return AV_ER_INVALID_ARG;
    return take_frame(av_index, false, buf, buf_size, out_actual, out_expected, info, info_size,
                      out_info, out_number);
}

TK_EXPORT int avRecvAudioData(int av_index, char *buf, int buf_size, char *info, int info_size,
                              unsigned int *out_number) {
    return take_frame(av_index, true, buf, buf_size, NULL, NULL, info, info_size, NULL, out_number);
}

/* Drop the queued frames of one stream; cleaning video also resets the packet numbering with the
 * camera, so packets already in flight are ignored. */
static int clean(int av_index, bool audio) {
    AvObj *o;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    pthread_mutex_lock(&o->lock);
    stream_clear(audio ? &o->audio : &o->video);
    if (!audio && o->media_state)
        memset(o->media_state, 0, sizeof(MediaState));
    rc = av_check(o);
    pthread_mutex_unlock(&o->lock);
    if (!rc && !audio)
        rc = av_media_reset(o);
    av_slot_release(av_index);
    return rc;
}

TK_EXPORT int avClientCleanVideoBuf(int av_index) { return clean(av_index, false); }
TK_EXPORT int avClientCleanAudioBuf(int av_index) { return clean(av_index, true); }
TK_EXPORT void avClientCleanBuf(int av_index) {
    clean(av_index, true);
    clean(av_index, false);
}
