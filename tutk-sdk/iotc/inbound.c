/* The receive path: unscramble each datagram and dispatch it by opcode (kit: iotc/17-inbound.md). */
#include <string.h>

#include "iotc.h"

#define MAX_DATAGRAM        2048
#define REPLY_SHORT_MIN     0x24                    /* body lengths of the 0x602 short form */
#define REPLY_LONG_MIN      0xb8                    /* and of the long form */
#define REPLY_UID_AT        0x10
#define REPLY_TOKEN_AT      0xbc
#define REPLY_RESULT_AT     0xc4
#define KEY_AUTH_MIN_VERSION 0x17
#define AUTHKEY_NONE        "00000000"

static bool has_authkey(const IotcSession *s) {
    return memcmp(s->authkey, AUTHKEY_NONE, IOTC_AUTHKEY_LEN) != 0;
}

/* The connecting session whose token (or, with a NULL token, UID) the packet names. */
static IotcSession *find_connecting(const uint8_t *token, const uint8_t *uid) {
    for (int i = 0; i < iotc.session_count; i++) {
        IotcSession *s = &iotc.sessions[i];

        if (s->state != SESSION_CONNECTING || s->lan_state != LAN_SEARCH_RUNNING)
            continue;
        if (token ? memcmp(s->token, token, IOTC_TOKEN_LEN) == 0
                  : strncmp(s->uid, (const char *)uid, IOTC_UID_LEN) == 0)
            return s;
    }
    return NULL;
}

/* The established session a packet belongs to: by the 8-byte token at `token_at` when the packet
 * carries one, else by the token's low 16 bits at header +0x0c. */
static IotcSession *find_established(const uint8_t *pkt, int len, int token_at) {
    bool full = pkt[3] & IOTC_FLAG_SUBHEADER && len >= token_at + IOTC_TOKEN_LEN;

    for (int i = 0; i < iotc.session_count; i++) {
        IotcSession *s = &iotc.sessions[i];

        if (s->state != SESSION_ESTABLISHED)
            continue;
        if (full ? memcmp(s->token, pkt + token_at, IOTC_TOKEN_LEN) == 0
                 : memcmp(s->token, pkt + 0xc, 2) == 0)
            return s;
    }
    return NULL;
}

static int reject_code(uint8_t result) {
    switch (result) {
    case 3:
        return IOTC_ER_DEVICE_EXCEED_MAX_SESSION;
    case 4:
        return IOTC_ER_DEVICE_REJECT_BYPORT;
    case 5:
        return IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY;
    case 6:
        return IOTC_ER_DEVICE_NOT_USE_KEY_AUTHENTICATION;
    default:
        return 0;
    }
}

/* The camera's answer to the LAN search. The long form settles an authenticated connect; the short
 * form is the no-auth device-found, which a session with an authKey rejects. */
static void handle_lan_reply(const uint8_t *pkt, int len, const char *host, uint16_t port) {
    uint16_t body = iotc_get16(pkt + 4);
    uint8_t version = pkt[2];
    IotcSession *s;

    if (body >= REPLY_LONG_MIN && len >= REPLY_RESULT_AT + 1) {
        uint8_t result = pkt[REPLY_RESULT_AT];

        s = find_connecting(pkt + REPLY_TOKEN_AT, NULL);
        if (s == NULL)
            return;
        s->remote_proto_version = version;
        if (len >= 0x34)
            memcpy(&s->remote_iotc_version, pkt + 0x30, sizeof s->remote_iotc_version);
        if (result != 1 && result != 7) {
            s->connect_error = reject_code(result);
            return;
        }
        if (version < KEY_AUTH_MIN_VERSION && has_authkey(s)) {
            s->connect_error = IOTC_ER_DEVICE_NOT_USE_KEY_AUTHENTICATION;
            return;
        }
        s->pe_capable = (pkt[3] & IOTC_FLAG_SCRAMBLED) != 0;
        strncpy(s->peer_host, host, sizeof s->peer_host - 1);
        s->peer_port = port;
        iotc_send_connect_request(s);
        s->mode = 2;                                /* LAN-direct */
        s->lan_state = LAN_SEARCH_BOUND;
        s->silence_ticks = 0;
        s->state = SESSION_ESTABLISHED;
        tk_trace("iotc: session %d established with %s, version 0x%x", s->session_id, host, version);
    } else if (body > REPLY_SHORT_MIN - 1 && len >= REPLY_UID_AT + IOTC_UID_LEN) {
        s = find_connecting(NULL, pkt + REPLY_UID_AT);
        if (s && has_authkey(s)) {
            s->remote_proto_version = version;
            s->connect_error = IOTC_ER_DEVICE_NOT_USE_KEY_AUTHENTICATION;
        }
    }
}

static void handle_data(const uint8_t *pkt, int len) {
    int skip = pkt[3] & IOTC_FLAG_SUBHEADER ? IOTC_SUBHEADER_LEN : 0;
    int n = iotc_get16(pkt + 4) - skip;
    IotcSession *s = find_established(pkt, len, IOTC_HEADER_LEN + 4);

    if (s == NULL || n < 0 || IOTC_HEADER_LEN + skip + n > len) {
        tk_trace("iotc: dropped data, %s, len %d", s ? "bad length" : "no session", len);
        tk_hex("iotc-drop", 'R', -1, pkt, len);
        return;
    }
    s->silence_ticks = 0;
    __Fill_ReadBuf(s->session_id, pkt + IOTC_HEADER_LEN + skip, (uint16_t)n, iotc_get16(pkt + 6),
                   pkt[0xe], pkt[0xf]);
}

void iotc_on_datagram(TConnection *c, const char *from_host, uint16_t from_port, const void *data,
                      long len, void *arg) {
    uint8_t pkt[MAX_DATAGRAM];
    IotcSession *s;
    int n = (int)len;

    if (n < IOTC_HEADER_LEN || n > MAX_DATAGRAM)
        return;
    iotc_unscramble(iotc.key, data, pkt, n, IOTC_HEADER_LEN);
    if (iotc_get16(pkt) != 0x0204)
        return;
    iotc_unscramble(iotc.key, data, pkt, n, pkt[3] & IOTC_FLAG_PE ? IOTC_PE_PREFIX : n);
    if (iotc.init != INIT_UP)
        return;
    iotc_lock();
    switch (iotc_get16(pkt + 8)) {
    case OP_LAN_REPLY:
        handle_lan_reply(pkt, n, from_host, from_port);
        break;
    case OP_DATA_IN:
        handle_data(pkt, n);
        break;
    case OP_ALIVE_IN:
        if ((s = find_established(pkt, n, IOTC_HEADER_LEN)))
            s->silence_ticks = 0;
        break;
    case OP_CLOSE_IN:
        if ((s = find_established(pkt, n, IOTC_HEADER_LEN)))
            s->state = SESSION_CLOSED_BY_REMOTE;
        break;
    default:
        tk_trace("iotc: ignored opcode 0x%04x len %d", iotc_get16(pkt + 8), n);
        tk_hex("iotc-drop", 'R', -1, pkt, n);
        break;
    }
    iotc_unlock();
}
