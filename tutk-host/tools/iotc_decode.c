/* Unscramble IOTC datagrams for reading captures. Reads lines "<label> <hex payload>" on stdin (as
 * tools/iotc_decode.sh produces from a pcap) and prints each with its payload unscrambled, or as it came
 * when the payload is not a scrambled IOTC datagram (the 0xcc51 records travel in clear).
 *
 *   cc -Iiotc -Iinclude -Icommon tools/iotc_decode.c iotc/wire.c -o build/iotc_decode -lpthread */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "iotc.h"

IotcState iotc;

/* The licence in use has no keyMask, so the key is the bare constant. */
int GetLicenseKeyMask(uint8_t *out, int size) { return -1; }

int main(void) {
    static char line[1 << 17];
    static uint8_t raw[1 << 16], plain[1 << 16];

    iotc_build_key();
    while (fgets(line, sizeof line, stdin)) {
        char *hex = strrchr(line, ' ');
        int n = 0;

        if (!hex)
            continue;
        *hex++ = 0;
        while (n < (int)sizeof raw && sscanf(hex + 2 * n, "%2hhx", &raw[n]) == 1)
            n++;
        /* A scrambled datagram unscrambles to type 0x0204 and a version byte of 0x1d (ours) or 0x1b
         * (the camera's); with partial encryption (flags bit0) only the first 0x40 bytes were
         * scrambled. */
        iotc_unscramble(iotc.key, raw, plain, n, 16);
        if (n >= 16 && plain[0] == 4 && plain[1] == 2 && (plain[2] == 0x1d || plain[2] == 0x1b))
            iotc_unscramble(iotc.key, raw, plain, n, plain[3] & 1 ? 0x40 : n);
        else
            memcpy(plain, raw, (size_t)n);
        printf("%s %d ", line, n);
        for (int i = 0; i < n; i++)
            printf("%02x", plain[i]);
        printf("\n");
    }
    return 0;
}
