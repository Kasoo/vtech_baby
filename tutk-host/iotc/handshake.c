/* The datagrams of the LAN-direct connect, keepalive and close (kit: iotc/16-handshake.md). */
#include <string.h>

#include "iotc.h"

#define RECORD_MAGIC        0xcc51
#define RECORD_VERSION      2
#define RECORD_LAN_COMMAND  0x10
#define RECORD_HMAC_AT      0x20
#define SHA1_LEN            20
#define CONNECT_ACK_LEN     0x34

/* The connect-path probe: the discovery probe with the UID, token and authKey filled in. */
int iotc_build_connect_probe(const IotcSession *s, uint8_t out[IOTC_PROBE_LEN]) {
    iotc_build_lan_probe(out);
    memcpy(out + 0x10, s->uid, strlen(s->uid));
    memcpy(out + 0x38, s->token, IOTC_TOKEN_LEN);
    iotc_put32(out + 0x40, 1);
    memcpy(out + 0x4a, s->authkey, IOTC_AUTHKEY_LEN);
    return IOTC_PROBE_LEN;
}

/* The credentialed LAN command: an unscrambled record signed with HMAC-SHA1 keyed by UID || authKey. */
int iotc_build_lan_record(const IotcSession *s, uint8_t out[IOTC_RECORD_LEN]) {
    uint8_t key[IOTC_UID_LEN + IOTC_AUTHKEY_LEN];

    memset(out, 0, IOTC_RECORD_LEN);
    iotc_put16(out, RECORD_MAGIC);
    out[4] = RECORD_VERSION;
    out[5] = RECORD_LAN_COMMAND;
    iotc_put16(out + 6, IOTC_RECORD_LEN - 12);
    memcpy(out + 0x10, s->token, IOTC_TOKEN_LEN);
    iotc_put32(out + 0x18, IOTC_SDK_VERSION);
    out[0x1c] = IOTC_VERSION;
    memset(key, 0, sizeof key);
    memcpy(key, s->uid, strlen(s->uid));
    memcpy(key + IOTC_UID_LEN, s->authkey, IOTC_AUTHKEY_LEN);
    if (TUTK3rdSHA1HMAC(key, sizeof key, out, RECORD_HMAC_AT, out + RECORD_HMAC_AT, SHA1_LEN) != 0)
        return -1;
    return IOTC_RECORD_LEN;
}

int iotc_send_to_peer(const IotcSession *s, uint8_t *pkt, int len) {
    int n = len;

    if (iotc.conn == NULL || s->peer_host[0] == 0)
        return IOTC_ER_INVALID_SID;
    pkt[3] |= IOTC_FLAG_SCRAMBLED;
    iotc_scramble(iotc.key, pkt, pkt, len, pkt[3] & IOTC_FLAG_PE ? IOTC_PE_PREFIX : len);
    return iotc.conn->vtbl->send(iotc.conn, s->peer_host, s->peer_port, pkt, &n);
}

/* Our answer to the camera's connect response: the connect-path probe, then the 0x0402 ack. */
void iotc_send_connect_request(const IotcSession *s) {
    uint8_t probe[IOTC_PROBE_LEN], ack[CONNECT_ACK_LEN];

    iotc_build_connect_probe(s, probe);
    iotc_send_to_peer(s, probe, sizeof probe);
    iotc_put_header(ack, 0, CONNECT_ACK_LEN - IOTC_HEADER_LEN, OP_PUNCH_ACK, 0x0033);
    memcpy(ack + 0x10, s->uid, strlen(s->uid));
    memcpy(ack + 0x24, s->token, IOTC_TOKEN_LEN);
    iotc_put32(ack + 0x30, IOTC_SDK_VERSION);
    iotc_send_to_peer(s, ack, sizeof ack);
}

/* Keepalive and close share one shape: the header and the 8-byte token. */
static void send_token_datagram(const IotcSession *s, uint16_t opcode) {
    uint8_t pkt[IOTC_HEADER_LEN + IOTC_TOKEN_LEN];

    iotc_put_header(pkt, IOTC_FLAG_SUBHEADER, IOTC_TOKEN_LEN, opcode, 0x0021);
    memcpy(pkt + IOTC_HEADER_LEN, s->token, IOTC_TOKEN_LEN);
    iotc_send_to_peer(s, pkt, sizeof pkt);
}

void iotc_send_keepalive(const IotcSession *s) { send_token_datagram(s, OP_ALIVE_OUT); }

/* Three back-to-back, for UDP reliability. */
void iotc_send_close(const IotcSession *s) {
    for (int i = 0; i < 3; i++)
        send_token_datagram(s, OP_CLOSE_OUT);
}
