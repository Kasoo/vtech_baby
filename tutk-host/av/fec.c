/* Parity recovery (kit: av/80-fec.md). The camera follows each error-correction group of k data
 * packets with one Reed-Solomon parity packet over GF(256): parity[j] = sum of g_i * data_i[j], each
 * shard being its media sub-header and payload zero-padded to FEC_BLOCK bytes. With the parity and k-1
 * of the data packets, the missing one is rebuilt and handed to reassembly as if it had arrived. */
#include <pthread.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#include "av.h"

#define FEC_BLOCK   1044          /* media sub-header + 1024 payload bytes */
#define FEC_MAX_K   25
#define FEC_SLOTS   4             /* groups kept at once; older ones are dropped */
#define MEDIA_HEADER 0x14

/* The generator row for each group size the camera uses. */
static const uint8_t ROW_20[20] = {
    0x8f, 0xae, 0x5b, 0x70, 0xd0, 0xcd, 0x54, 0x43, 0x39, 0xa3,
    0xc9, 0x58, 0x1b, 0xbb, 0xb3, 0x18, 0x1b, 0x1c, 0x12, 0x14,
};
static const uint8_t ROW_25[25] = {
    0x19, 0x19, 0x6f, 0x6f, 0x53, 0x53, 0x24, 0x24, 0xb0, 0xb0, 0xd0, 0xd0, 0x92,
    0x92, 0xf3, 0xf3, 0x9e, 0x9e, 0x89, 0x89, 0xf7, 0xf7, 0xe1, 0xe1, 0x01,
};

typedef struct {
    bool     used, done;          /* done: complete, or its missing packet already rebuilt */
    uint16_t group;
    uint8_t  k;
    uint32_t seq;                 /* when the slot was taken, to drop the oldest */
    uint32_t have;                /* bit i: data shard i; bit k: the parity */
    uint8_t  block[FEC_MAX_K + 1][FEC_BLOCK];
} FecGroup;

typedef struct FecState {
    FecGroup slot[FEC_SLOTS];
    uint32_t seq;
} FecState;

/* GF(2^8), polynomial 0x11d, generator 2. */
static uint8_t gf_exp[510], gf_log[256];
static pthread_once_t gf_once = PTHREAD_ONCE_INIT;

static void gf_init(void) {
    unsigned x = 1;

    for (int i = 0; i < 255; i++) {
        gf_exp[i] = gf_exp[i + 255] = (uint8_t)x;
        gf_log[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100)
            x ^= 0x11d;
    }
}

static uint8_t gf_mul(uint8_t a, uint8_t b) {
    return a && b ? gf_exp[gf_log[a] + gf_log[b]] : 0;
}

static const uint8_t *row_for(uint8_t k) {
    return k == 20 ? ROW_20 : k == 25 ? ROW_25 : NULL;
}

/* The slot for `group`, taking the oldest one for a new group. */
static FecGroup *slot_for(FecState *st, uint16_t group, uint8_t k) {
    FecGroup *oldest = &st->slot[0];

    for (int i = 0; i < FEC_SLOTS; i++) {
        FecGroup *g = &st->slot[i];

        if (g->used && g->group == group && g->k == k)
            return g;
        if (!g->used || (oldest->used && (int32_t)(g->seq - oldest->seq) < 0))
            oldest = g;
    }
    memset(oldest, 0, offsetof(FecGroup, block));
    oldest->used = true;
    oldest->group = group;
    oldest->k = k;
    oldest->seq = st->seq++;
    return oldest;
}

/* Rebuild the one missing data shard of `g` into `out`; returns its index. */
static int rebuild(const FecGroup *g, const uint8_t *row, uint8_t *out) {
    int miss = 0;
    uint8_t inv;

    while (g->have & (1u << miss))
        miss++;
    memcpy(out, g->block[g->k], FEC_BLOCK);
    for (int i = 0; i < g->k; i++) {
        if (i == miss)
            continue;
        for (int j = 0; j < FEC_BLOCK; j++)
            out[j] ^= gf_mul(row[i], g->block[i][j]);
    }
    inv = gf_exp[255 - gf_log[row[miss]]];
    for (int j = 0; j < FEC_BLOCK; j++)
        out[j] = gf_mul(out[j], inv);
    return miss;
}

/* One class-0x0c media or parity packet: `fec` is its group header (group, index, k), `f` the media
 * sub-header and payload, `flen` bytes. */
void av_fec_on_packet(AvObj *o, const uint8_t *fec, const uint8_t *f, int flen) {
    uint16_t group = av_get16(fec);
    uint8_t index = fec[2], k = fec[3];
    const uint8_t *row = row_for(k), *src = f;
    bool parity = f[0] == CLASS_DATA;
    uint8_t out[FEC_BLOCK];
    int len = flen, plen, miss;
    FecGroup *g;

    if (!row || index > k || parity != (index == k))
        return;
    if (parity) {                   /* its RS block follows its own sub-header */
        src += MEDIA_HEADER;
        len -= MEDIA_HEADER;
        if (len > FEC_BLOCK)
            len = FEC_BLOCK;
    } else {
        len = MEDIA_HEADER + av_get16(f + 8);
        if (len > flen || len > FEC_BLOCK)
            return;
    }
    if (len <= 0)
        return;
    pthread_once(&gf_once, gf_init);

    pthread_mutex_lock(&o->lock);
    if (!o->fec)
        o->fec = calloc(1, sizeof(FecState));
    if (!o->fec) {
        pthread_mutex_unlock(&o->lock);
        return;
    }
    g = slot_for(o->fec, group, k);
    if (g->done || (g->have & (1u << index))) {
        pthread_mutex_unlock(&o->lock);
        return;
    }
    memcpy(g->block[index], src, (size_t)len);
    memset(g->block[index] + len, 0, (size_t)(FEC_BLOCK - len));
    g->have |= 1u << index;
    if ((g->have & ((1u << k) - 1)) == (1u << k) - 1) {
        g->done = true;             /* nothing missing */
        pthread_mutex_unlock(&o->lock);
        return;
    }
    if (!(g->have & (1u << k)) || __builtin_popcount(g->have) != k) {
        pthread_mutex_unlock(&o->lock);
        return;
    }
    miss = rebuild(g, row, out);
    g->done = true;
    o->fec_recovered++;
    pthread_mutex_unlock(&o->lock);

    plen = av_get16(out + 8);
    tk_trace("av: rebuilt packet 0x%04x (group %u, shard %d of %u, plen %d)", av_get16(out + 2), group, miss,
             k, plen);
    if (MEDIA_HEADER + plen <= FEC_BLOCK)
        av_media_on_packet(o, out, MEDIA_HEADER + plen);
}

/* Forget the groups in progress; packets already in flight then rebuild nothing. Caller holds o->lock. */
void av_fec_reset(AvObj *o) {
    if (o->fec)
        memset(o->fec, 0, sizeof(FecState));
}

void av_fec_free(AvObj *o) {
    free(o->fec);
    o->fec = NULL;
}
