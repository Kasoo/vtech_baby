/* The packed address codec and the local-address cache (kit: iotc/80-addresses.md). */
#include <arpa/inet.h>
#include <netdb.h>
#include <resolv.h>
#include <stdio.h>
#include <string.h>

#include "iotc.h"

#define MAX_LOCAL 10

/* The host's own IPv4 interfaces, filled once. */
static struct {
    bool     built;
    int      count;
    in_addr_t addr[MAX_LOCAL];
    char     text[MAX_LOCAL][INET_ADDRSTRLEN];
    uint16_t port_be;
    uint16_t local_id[2];
} local;

static pthread_mutex_t res_lock = PTHREAD_MUTEX_INITIALIZER;

int iotc_netaddr_get_content(const TIotcNetAddr *addr, int text_size, char *text, uint16_t *port,
                             char *family) {
    if (addr == NULL || addr->family > 1)
        return IOTC_ER_INVALID_ARG;
    if (text && text_size > 0)
        tutk_platform_inet_ntop(addr->family ? AF_INET6 : AF_INET, addr->addr, text,
                                (socklen_t)text_size);
    if (port)
        *port = addr->port;
    if (family)
        *family = (char)addr->family;
    return 0;
}

int iotc_netaddr_put_content(const char *host, int port, TIotcNetAddr *addr) {
    struct addrinfo hints = {.ai_flags = AI_NUMERICHOST, .ai_socktype = SOCK_DGRAM}, *res = NULL;
    char service[8];
    int rc;

    memset(addr, 0, sizeof *addr);
    if (port)
        snprintf(service, sizeof service, "%d", port);
    rc = getaddrinfo(host, port ? service : NULL, &hints, &res);
    if (rc != 0) {
        if (rc == EAI_NONAME)
            tk_lan_only_count_lookup();
        if (rc == EAI_AGAIN || rc == EAI_FAIL) {
            pthread_mutex_lock(&res_lock);
            res_init();
            pthread_mutex_unlock(&res_lock);
        }
        return IOTC_ER_INVALID_ARG;
    }
    addr->port = (uint16_t)port;
    if (res->ai_family == AF_INET6) {
        addr->family = 1;
        memcpy(addr->addr, &((struct sockaddr_in6 *)res->ai_addr)->sin6_addr, 16);
    } else {
        addr->family = 0;
        memcpy(addr->addr, &((struct sockaddr_in *)res->ai_addr)->sin_addr, 4);
    }
    freeaddrinfo(res);
    return 0;
}

void UpdateLocalNetworkInfo(void) {
    TNetInterfaceInfo info;

    if (local.built && iotc.listen_port == 0)
        return;
    memset(&local, 0, sizeof local);
    tutk_Net_GetInterfaceInfo(&info);
    local.port_be = htons(iotc.listen_port);
    for (int i = 0; i < info.count && i < MAX_LOCAL; i++) {
        local.addr[i] = info.addr[i];
        tutk_platform_inet_ntop(AF_INET, &info.addr[i], local.text[i], INET_ADDRSTRLEN);
        local.count++;
    }
    for (int i = 0; i < 2; i++)
        while (local.local_id[i] == 0)
            local.local_id[i] = (uint16_t)tutk_platform_rand();
    local.built = true;
}
