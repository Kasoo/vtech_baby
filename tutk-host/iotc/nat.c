/* NAT tracking and connection nonces (kit: iotc/60-nat.md). On the LAN-direct paths no mapped
 * address is ever recorded, so the type stays undetermined. */
#include "iotc.h"

TNatTrack iotc_nat;

/* Two library-wide nonces the cloud paths set; zero here, and zero is never drawn. */
static uint16_t global_nonce[2];

void initNatTrack(TNatTrack *nat) {
    tutk_platform_mutex_init(&nat->lock);
    clearNatRecord(nat);
}

void clearNatRecord(TNatTrack *nat) {
    pthread_mutex_lock(&nat->lock);
    nat->count = 0;
    nat->nat_flag_a = 0;
    pthread_mutex_unlock(&nat->lock);
}

char getNatType(TNatTrack *nat) {
    char type;

    pthread_mutex_lock(&nat->lock);
    if (nat->nat_flag_a)
        type = 1;
    else if (nat->count < 2)
        type = 0;
    else
        type = nat->nat_flag_b ? 2 : 3;
    pthread_mutex_unlock(&nat->lock);
    return type;
}

void forEachNat(TNatTrack *nat, void (*fn)(void *ctx, TNatRecord *rec, uint16_t port, void *body),
                void *ctx) {
    pthread_mutex_lock(&nat->lock);
    for (int i = 0; i < nat->count && i < 8; i++)
        fn(ctx, &nat->records[i], nat->records[i].port, nat->records[i]._reserved_00);
    pthread_mutex_unlock(&nat->lock);
}

void iotcUpdateNonce(uint16_t *out) {
    uint16_t n;
    bool used;

    iotc_lock();
    do {
        n = (uint16_t)(tutk_platform_rand() % 0xffff + 1);
        used = n == global_nonce[0] || n == global_nonce[1];
        for (int i = 0; i < iotc.session_count && !used; i++)
            if (iotc.sessions[i].state != SESSION_FREE)
                used = n == iotc.sessions[i].nonce_a || n == iotc.sessions[i].nonce_b;
    } while (used);
    iotc_unlock();
    *out = n;
}
