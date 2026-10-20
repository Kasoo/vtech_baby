/* Host build of iotc/wire.c: prints the kit's worked example scrambled, and checks the inverse on every
 * length and prefix up to 100 bytes. */
#include <stdio.h>
#include <string.h>

#include "iotc.h"

IotcState iotc;

int GetLicenseKeyMask(uint8_t *out, int size) { return -1; }

int main(void) {
    static const uint8_t plain[16] = {4, 3, 2, 1, 8, 7, 6, 5, 0xc, 0xb, 0xa, 9, 0x10, 0xf, 0xe, 0xd};
    static const uint8_t key[16] = {0x44, 0x33, 0x22, 0x11, 0x88, 0x77, 0x66, 0x55,
                                    0xcc, 0xbb, 0xaa, 0x99, 0x00, 0xff, 0xee, 0xdd};
    uint8_t out[128], back[128], msg[128];

    iotc_scramble(key, plain, out, 16, 16);
    for (int i = 0; i < 16; i++)
        printf("%02x", out[i]);
    printf("\n");
    for (int i = 0; i < 128; i++)
        msg[i] = (uint8_t)(i * 37 + 11);
    for (int len = 0; len <= 100; len++)
        for (int prefix = 0; prefix <= len + 1; prefix++) {
            iotc_scramble(key, msg, out, len, prefix);
            iotc_unscramble(key, out, back, len, prefix);
            if (memcmp(back, msg, (size_t)len) != 0) {
                printf("mismatch len %d prefix %d\n", len, prefix);
                return 1;
            }
        }
    iotc_build_key();
    printf("%.16s\n", (const char *)iotc.key);
    return 0;
}
