/* The on-wire header, scramble, its key and the LAN-search probe (kit: iotc/15-wire-format.md). */
#include <string.h>

#include "iotc.h"

static const char KEY_CONSTANT[] = "Charlie is the designer of P2P!!";
static const int ROT[4] = {1, 5, 9, 13};

static uint32_t ror(uint32_t x, int r) { return x >> r | x << (32 - r); }
static uint32_t rol(uint32_t x, int r) { return x << r | x >> (32 - r); }

static uint32_t load32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void store32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

uint16_t iotc_get16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

void iotc_put16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

void iotc_put32(uint8_t *p, uint32_t v) { store32(p, v); }

/* The fixed 128-bit permutation of step 3. */
static void permute(const uint32_t b[4], uint32_t y[4]) {
    uint32_t b0 = b[0], b1 = b[1], b2 = b[2], b3 = b[3];
    uint32_t t = (b0 >> 16 & 0xff) | (b0 & 0xff00) | b0 << 24;
    uint32_t u = (b1 >> 16 & 0xff) | (b1 >> 8 & 0xff0000) | (b1 << 8 & 0xffff) | (b0 & 0xff000000);

    y[0] = (b2 >> 24) << 29 | (((b2 & 0xff00) | (b2 & 0xff) << 16 | b2 >> 24 | (b3 & 0xff000000)) >> 3);
    y[1] = (b3 >> 8) << 25 |
           (((b3 >> 8 & 0xff) | (b2 >> 8 & 0xff00) | (b3 & 0xff) << 16 | (b3 & 0xff0000) << 8) >> 7);
    y[2] = t << 21 | ((t | (b1 << 8 & 0xff0000)) >> 11);
    y[3] = u << 17 | u >> 15;
}

/* inverse_map[j] is the input bit that permute() moves to output bit j; built once by pushing each
 * single bit through. */
static uint8_t inverse_map[128];
static pthread_once_t inverse_once = PTHREAD_ONCE_INIT;

static void build_inverse(void) {
    for (int i = 0; i < 128; i++) {
        uint32_t b[4] = {0}, y[4];

        b[i / 32] = 1u << (i % 32);
        permute(b, y);
        for (int j = 0; j < 128; j++)
            if (y[j / 32] >> (j % 32) & 1)
                inverse_map[j] = (uint8_t)i;
    }
}

static void unpermute(const uint32_t y[4], uint32_t b[4]) {
    pthread_once(&inverse_once, build_inverse);
    memset(b, 0, 16);
    for (int j = 0; j < 128; j++)
        if (y[j / 32] >> (j % 32) & 1)
            b[inverse_map[j] / 32] |= 1u << (inverse_map[j] % 32);
}

/* The tail reorder, out[i] = in[order[i]], for the lengths that have one. */
static const uint8_t *tail_order(int n) {
    static const uint8_t two[] = {1, 0}, four[] = {2, 3, 0, 1}, eight[] = {7, 4, 3, 2, 1, 6, 5, 0};

    return n == 2 ? two : n == 4 ? four : n == 8 ? eight : NULL;
}

void iotc_scramble(const uint8_t key[16], const void *in, void *out, int len, int prefix) {
    const uint8_t *src = in;
    uint8_t *dst = out, tmp[16];
    int n = prefix < len ? prefix : len, nblk = n >> 4, tail = n & 0xf;
    const uint8_t *order = tail_order(tail);

    for (int k = 0; k < nblk; k++) {
        uint32_t b[4], y[4];

        for (int i = 0; i < 4; i++)
            b[i] = ror(load32(src + 16 * k + 4 * i), ROT[i]) ^ load32(key + 4 * i);
        permute(b, y);
        for (int i = 0; i < 4; i++)
            store32(dst + 16 * k + 4 * i, y[i]);
    }
    for (int i = 0; i < tail; i++)
        tmp[i] = src[16 * nblk + i] ^ key[i];
    for (int i = 0; i < tail; i++)
        dst[16 * nblk + i] = tmp[order ? order[i] : i];
    if (len > n && dst != src)
        memmove(dst + n, src + n, (size_t)(len - n));
}

void iotc_unscramble(const uint8_t key[16], const void *in, void *out, int len, int prefix) {
    const uint8_t *src = in;
    uint8_t *dst = out, tmp[16];
    int n = prefix < len ? prefix : len, nblk = n >> 4, tail = n & 0xf;
    const uint8_t *order = tail_order(tail);

    for (int k = 0; k < nblk; k++) {
        uint32_t b[4], y[4];

        for (int i = 0; i < 4; i++)
            y[i] = load32(src + 16 * k + 4 * i);
        unpermute(y, b);
        for (int i = 0; i < 4; i++)
            store32(dst + 16 * k + 4 * i, rol(b[i] ^ load32(key + 4 * i), ROT[i]));
    }
    for (int i = 0; i < tail; i++)
        tmp[order ? order[i] : i] = src[16 * nblk + i];
    for (int i = 0; i < tail; i++)
        dst[16 * nblk + i] = tmp[i] ^ key[i];
    if (len > n && dst != src)
        memmove(dst + n, src + n, (size_t)(len - n));
}

/* The key is the constant XOR the licence mask, or the bare constant without a licence. */
void iotc_build_key(void) {
    uint8_t mask[16];

    memcpy(iotc.key, KEY_CONSTANT, 16);
    if (GetLicenseKeyMask(mask, sizeof mask) == 0)
        for (int i = 0; i < 16; i++)
            iotc.key[i] ^= mask[i];
}

/* The 16-byte header of every IOTC datagram; the fields from +0x06 on are left zero. */
void iotc_put_header(uint8_t *pkt, uint8_t flags, uint16_t length, uint16_t opcode, uint16_t field_a) {
    memset(pkt, 0, IOTC_HEADER_LEN);
    iotc_put16(pkt, 0x0204);
    pkt[2] = IOTC_VERSION;
    pkt[3] = flags;
    iotc_put16(pkt + 4, length);
    iotc_put16(pkt + 8, opcode);
    iotc_put16(pkt + 0xa, field_a);
}

/* The 88-byte discovery probe before scrambling. */
int iotc_build_lan_probe(uint8_t out[IOTC_PROBE_LEN]) {
    memset(out, 0, IOTC_PROBE_LEN);
    iotc_put_header(out, 0, IOTC_PROBE_LEN - IOTC_HEADER_LEN, OP_LAN_SEARCH, 0x0021);
    out[0xf] = 0x01;
    iotc_put32(out + 0x34, IOTC_SDK_VERSION);
    return IOTC_PROBE_LEN;
}
