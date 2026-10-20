/* OpenSSL errors into IOTC's internal DTLS error space (kit: iotc/90-errors.md). */
#include "iotc.h"

#define ERR_REASON(e) ((int)((e) & 0x7fffff))

int translate_Error(unsigned int source, SSL *ssl) {
    switch (source) {
    case 0:
        return 0;
    case 1: {
        unsigned long e = tutk_third_ERR_get_error();
        char text[256];
        int reason;

        tutk_third_ERR_error_string_n(e, text, sizeof text);
        reason = ERR_REASON(e);
        if (reason == 0x119 || reason == 0x3fc)
            return -50002;
        return -52000 - reason;
    }
    case 5:
        return -52005;
    default:
        return -52000 - (int)source;
    }
}
