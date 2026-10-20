/* Host build of iotc/wire.c as a shared library, so the offline tests can play the camera's side of
 * the wire: wire_scramble/wire_unscramble with the licence-less key. */
#include "iotc.h"

IotcState iotc;

int GetLicenseKeyMask(uint8_t *out, int size) { return -1; }

TK_EXPORT void wire_scramble(const void *in, void *out, int len, int prefix) {
    iotc_build_key();
    iotc_scramble(iotc.key, in, out, len, prefix);
}

TK_EXPORT void wire_unscramble(const void *in, void *out, int len, int prefix) {
    iotc_build_key();
    iotc_unscramble(iotc.key, in, out, len, prefix);
}
