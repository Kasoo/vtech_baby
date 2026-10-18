/* Parallel connect (kit: iotc/65-tryport.md). The LAN-direct path has one candidate, so only the
 * lifecycle is kept: bring-up and the interface handle. */
#include "iotc.h"

enum { TRYPORT_ER_NOT_INITIALIZED = -1001, TRYPORT_ER_ALREADY_INITIALIZED = -1002,
       TRYPORT_ER_INVALID_ARG = -1003 };

static struct {
    bool  up;
    void *conn_mgr;
    void *iface;
} tryport;

int IOTC_TryPortInitialize(void *conn_mgr, void *iface) {
    if (tryport.up)
        return TRYPORT_ER_ALREADY_INITIALIZED;
    if (conn_mgr == NULL || iface == NULL)
        return TRYPORT_ER_INVALID_ARG;
    tryport.conn_mgr = conn_mgr;
    tryport.iface = iface;
    tryport.up = true;
    return 0;
}

int IOTC_TryPortGetInterface(void **iface_out) {
    if (!tryport.up)
        return TRYPORT_ER_NOT_INITIALIZED;
    if (iface_out == NULL)
        return TRYPORT_ER_INVALID_ARG;
    *iface_out = &tryport.conn_mgr;
    return 0;
}
