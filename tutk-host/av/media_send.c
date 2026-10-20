/* Media send (kit: av/60-media-send.md, av/80-fec.md): talk-back audio, sent as it is given. */
#include <string.h>

#include "av.h"

/* Drop-in exports documented here:
 *   avSendAudioData
 */

#define KIND_AUDIO      0x03
#define MEDIA_FLAGS     0x01      /* header byte 1 of every media packet */
#define FEC_K           20        /* data packets per error-correction group */
#define GROUP_HEADER    8
#define MEDIA_HEADER    0x14
#define INFO_MAX        16

/* The media header checksum: a 16-bit sum of the header bytes (checksum field zero) and the payload. */
static uint16_t checksum(const uint8_t *header, const uint8_t *payload, int len) {
    uint16_t sum = 0;

    for (int i = 0; i < MEDIA_HEADER; i++)
        sum = (uint16_t)(sum + header[i]);
    for (int i = 0; i < len; i++)
        sum = (uint16_t)(sum + payload[i]);
    return sum;
}

/* One frame as fragments of MEDIA_FRAG bytes; the frame info rides at the end of the last. Each packet
 * carries the error-correction group header (group, index, k, 1); no parity packets are sent. */
static int send_frame(AvObj *o, uint8_t kind, const uint8_t *data, int size, const uint8_t *info,
                      int info_len) {
    int count = size ? (size + MEDIA_FRAG - 1) / MEDIA_FRAG : 1;
    uint32_t number, stream_count;
    uint16_t pn;

    pthread_mutex_lock(&o->lock);
    pn = o->tx_pn;
    o->tx_pn = (uint16_t)(o->tx_pn + count);
    number = o->tx_frame++;
    stream_count = ++o->tx_audio;
    pthread_mutex_unlock(&o->lock);

    for (int i = 0; i < count; i++, pn++) {
        uint8_t pkt[8 + GROUP_HEADER + MEDIA_HEADER + MEDIA_FRAG + INFO_MAX] = {0};
        uint8_t *g = pkt + 8, *m = g + GROUP_HEADER, *payload = m + MEDIA_HEADER;
        bool last = i == count - 1;
        int chunk = last ? size - i * MEDIA_FRAG : MEDIA_FRAG, plen = chunk + (last ? info_len : 0);
        int rc;

        pkt[0] = CLASS_DATA;
        pkt[1] = MEDIA_FLAGS;
        av_put16(pkt + 2, AV_VERSION);
        av_put16(g, pn / FEC_K);
        g[2] = (uint8_t)(pn % FEC_K);
        g[3] = FEC_K;
        av_put32(g + 4, 1);
        m[0] = kind;
        m[1] = last;
        av_put16(m + 2, pn);
        av_put16(m + 4, (uint16_t)count);
        av_put16(m + 6, (uint16_t)(last ? info_len : i));
        av_put16(m + 8, (uint16_t)plen);
        av_put32(m + 0x0c, number);
        av_put32(m + 0x10, stream_count);
        memcpy(payload, data + i * MEDIA_FRAG, (size_t)chunk);
        if (last && info_len)
            memcpy(payload + chunk, info, (size_t)info_len);
        av_put16(m + 0x0a, checksum(m, payload, plen));
        rc = av_send(o, pkt, 8 + GROUP_HEADER + MEDIA_HEADER + plen);
        if (rc < 0) {
            pthread_mutex_lock(&o->lock);
            rc = av_check(o);
            pthread_mutex_unlock(&o->lock);
            return rc ? rc : AV_ER_IOTC_SESSION_CLOSED;
        }
    }
    return 0;
}

TK_EXPORT int avSendAudioData(int av_index, const char *data, int data_size, void *frame_info,
                              int frame_info_size) {
    AvObj *o;
    int rc;

    if (!av_initialized())
        return AV_ER_NOT_INITIALIZED;
    if (!data || data_size <= 0 || frame_info_size < 0 || frame_info_size > INFO_MAX ||
        (frame_info_size && !frame_info))
        return AV_ER_INVALID_ARG;
    o = av_slot_acquire(av_index);
    if (!o)
        return AV_ER_INVALID_ARG;
    pthread_mutex_lock(&o->lock);
    rc = o->server ? av_check(o) : AV_ER_NOT_SUPPORT;
    pthread_mutex_unlock(&o->lock);
    if (rc == 0)
        rc = send_frame(o, KIND_AUDIO, (const uint8_t *)data, data_size, frame_info, frame_info_size);
    av_slot_release(av_index);
    return rc;
}
