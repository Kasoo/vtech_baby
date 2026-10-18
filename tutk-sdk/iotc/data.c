/* Data transfer (kit: iotc/30-data.md): per-channel receive queues filled by the inbound dispatcher
 * and drained by the reads, under the table lock. */
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "iotc.h"

#define WAIT_STEP_MS 10

/* Lock-free: it reads only the state byte. */
TK_EXPORT int IOTC_Check_Session_Status(unsigned int session_id) {
    if (iotc.init != INIT_UP)
        return IOTC_ER_NOT_INITIALIZED;
    if ((int)session_id < 0 || (int)session_id >= iotc.session_count)
        return IOTC_ER_INVALID_SID;
    switch (iotc.sessions[session_id].state) {
    case SESSION_ESTABLISHED:
        return 0;
    case SESSION_CLOSED_BY_REMOTE:
        return IOTC_ER_SESSION_CLOSE_BY_REMOTE;
    case SESSION_REMOTE_TIMEOUT:
        return IOTC_ER_REMOTE_TIMEOUT_DISCONNECT;
    default:
        return IOTC_ER_INVALID_SID;
    }
}

int IOTC_Session_Has_Data(int session_id, unsigned int channel, unsigned int timeout_ms) {
    unsigned int waited = 0;

    if (iotc.init != INIT_UP || session_id < 0 || session_id >= iotc.session_count ||
        channel >= IOTC_MAX_CHANNELS)
        return 0;
    for (;;) {
        if (iotc.sessions[session_id].rx_queue[channel].head)
            return 1;
        if (waited >= timeout_ms)
            return 0;
        usleep(WAIT_STEP_MS * 1000);
        waited += WAIT_STEP_MS;
    }
}

int IOTC_Session_Read_Check_Lost_Data_And_Datatype(unsigned int session_id, void *buf,
        unsigned int buf_size, unsigned int timeout_ms, uint16_t *out_seq, char *out_lost,
        uint8_t channel, unsigned int *out_flags) {
    unsigned int tries = timeout_ms / WAIT_STEP_MS + 1;

    if (channel >= IOTC_MAX_CHANNELS)
        return IOTC_ER_CH_NOT_ON;
    for (;;) {
        IotcSession *s;
        TRxPacket *p;
        int rc = iotc_session_enter((int)session_id, &s), n;

        if (rc != 0)
            return rc;
        if (s->read_abort) {
            iotc_unlock();
            return IOTC_ER_INVALID_SID;
        }
        p = s->rx_queue[channel].head;
        if (p == NULL) {
            iotc_unlock();
            if (timeout_ms == 0)
                return 0;
            if (--tries == 0)
                return IOTC_ER_TIMEOUT;
            usleep(WAIT_STEP_MS * 1000);
            continue;
        }
        s->rx_queue[channel].head = p->next;
        if (p->next == NULL)
            s->rx_queue[channel].tail = NULL;
        n = p->len < buf_size ? p->len : (int)buf_size;
        memcpy(buf, p->data, (size_t)n);
        if (out_seq)
            *out_seq = p->seq;
        if (out_lost)
            *out_lost = p->seq != s->chan_rx[channel];
        s->chan_rx[channel] = (uint16_t)(p->seq + 1);
        if (out_flags)
            *out_flags = p->flags;
        iotc_unlock();
        free(p->data);
        free(p);
        return n;
    }
}

TK_EXPORT int IOTC_Session_Read_Data_And_Datatype(unsigned int session_id, void *buf,
        unsigned int buf_size, unsigned int timeout_ms, uint16_t *out_seq, char *out_lost,
        uint8_t channel, unsigned int *out_flags) {
    return IOTC_Session_Read_Check_Lost_Data_And_Datatype(session_id, buf, buf_size, timeout_ms,
                                                          out_seq, out_lost, channel, out_flags);
}

int __Fill_ReadBuf(int session_id, const void *data, uint16_t len, uint16_t seq, uint8_t channel,
                   uint8_t flags) {
    IotcSession *s;
    TRxPacket *p;

    if (channel >= IOTC_MAX_CHANNELS || iotc_session_enter(session_id, &s) != 0)
        return IOTC_ER_INVALID_SID;
    p = calloc(1, sizeof *p);
    if (p)
        p->data = malloc(len ? len : 1);
    if (p == NULL || p->data == NULL) {
        iotc_unlock();
        free(p);
        return -2;
    }
    memcpy(p->data, data, len);
    p->len = len;
    p->seq = seq;
    p->rx_index = s->rx_index[channel]++;
    p->flags = flags & 3;
    if (s->rx_queue[channel].tail)
        s->rx_queue[channel].tail->next = p;
    else
        s->rx_queue[channel].head = p;
    s->rx_queue[channel].tail = p;
    s->rx_count[channel]++;
    iotc_unlock();
    return 0;
}

/* LAN-direct send: header, the 12-byte sub-header carrying the token, then the payload; scrambled
 * whole, or only its first 0x40 bytes under partial encryption. */
int IOTC_Session_Write_Data_And_Datatype(int session_id, const void *data, unsigned int len,
                                         uint8_t channel, unsigned int datatype) {
    uint8_t pkt[IOTC_HEADER_LEN + IOTC_SUBHEADER_LEN + IOTC_MAX_PAYLOAD];
    int total = IOTC_HEADER_LEN + IOTC_SUBHEADER_LEN + (int)len, rc;
    IotcSession *s;

    if (len > IOTC_MAX_PAYLOAD || channel >= IOTC_MAX_CHANNELS)
        return IOTC_ER_INVALID_ARG;
    rc = iotc_session_enter(session_id, &s);
    if (rc != 0)
        return rc;
    iotc_put_header(pkt, IOTC_FLAG_SUBHEADER | (s->pe_enabled ? IOTC_FLAG_PE : 0),
                    (uint16_t)(IOTC_SUBHEADER_LEN + len), OP_DATA_OUT, 0x0021);
    iotc_put16(pkt + 6, (uint16_t)s->send_seq[channel]++);
    memcpy(pkt + 0xc, s->token, 2);
    pkt[0xe] = channel;
    pkt[0xf] = datatype & 3;
    iotc_put32(pkt + IOTC_HEADER_LEN, IOTC_SUBHEADER_LEN);
    memcpy(pkt + IOTC_HEADER_LEN + 4, s->token, IOTC_TOKEN_LEN);
    memcpy(pkt + IOTC_HEADER_LEN + IOTC_SUBHEADER_LEN, data, len);
    rc = iotc_send_to_peer(s, pkt, total);
    iotc_unlock();
    return rc < 0 ? rc : (int)len;
}

TK_EXPORT int IOTC_Session_Write(int session_id, const void *data, unsigned int len, uint8_t channel) {
    return IOTC_Session_Write_Data_And_Datatype(session_id, data, len, channel, 0);
}

TK_EXPORT int IOTC_Session_Write_DTLS(int session_id, const void *data, unsigned int len,
                                      uint8_t channel) {
    return IOTC_Session_Write_Data_And_Datatype(session_id, data, len, channel, 1);
}
