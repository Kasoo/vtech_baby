/* Channels (kit: iotc/25-channels.md). Channel 0 is the session's own and is never handed out. */
#include "iotc.h"

TK_EXPORT int IOTC_Session_Get_Free_Channel(unsigned int session_id) {
    IotcSession *s;
    int rc = iotc_session_enter((int)session_id, &s);

    if (rc != 0)
        return rc;
    rc = IOTC_ER_SESSION_NO_FREE_CHANNEL;
    for (int ch = 1; ch < IOTC_MAX_CHANNELS; ch++) {
        if (!s->chan_on[ch]) {
            s->chan_on[ch] = true;
            rc = ch;
            break;
        }
    }
    iotc_unlock();
    return rc;
}

TK_EXPORT int IOTC_Session_Channel_ON(unsigned int session_id, unsigned char channel) {
    IotcSession *s;
    int rc = iotc_session_enter((int)session_id, &s);

    if (rc != 0)
        return rc;
    if (channel >= IOTC_MAX_CHANNELS)
        rc = IOTC_ER_CH_NOT_ON;
    else
        s->chan_on[channel] = true;
    iotc_unlock();
    return rc;
}

TK_EXPORT int IOTC_Session_Channel_OFF(unsigned int session_id, unsigned char channel) {
    IotcSession *s;
    int rc = iotc_session_enter((int)session_id, &s);

    if (rc != 0)
        return rc;
    if (channel >= IOTC_MAX_CHANNELS) {
        rc = IOTC_ER_CH_NOT_ON;
    } else if (channel != 0) {
        s->chan_on[channel] = false;
        s->chan_rx[channel] = 0;
    }
    iotc_unlock();
    return rc;
}
