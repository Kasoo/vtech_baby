/* Addresses and interfaces (kit: global/10-platform.md). Host names are never resolved: Global is
 * LAN-only, so a lookup accepts numeric addresses only and a name fails as unresolvable. */
#include <stddef.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <netdb.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include "tk.h"

_Static_assert(sizeof(TNetInterfaceInfo) == 488, "TNetInterfaceInfo size");
_Static_assert(offsetof(TNetInterfaceInfo, addr) == 244, "TNetInterfaceInfo.addr offset");
_Static_assert(offsetof(TNetInterfaceInfo, netmask) == 284, "TNetInterfaceInfo.netmask offset");

#define MAX_INTERFACES 10

TK_EXPORT int tutk_Host_Get_Sockaddr(const char *host, int transport, uint16_t port_be,
                                     unsigned int mode, struct sockaddr_in6 *out) {
    struct addrinfo hints, *res;
    char service[8];
    int rc;

    if (host == NULL) {
        memset(out, 0, sizeof *out);
        out->sin6_family = AF_INET6;
        out->sin6_addr = in6addr_any;
        out->sin6_port = port_be;
        return 0;
    }
    if (transport != TCONN_TCP && transport != TCONN_UDP)
        return TUTK_ER_INVALID_ARG;

    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = transport == TCONN_TCP ? SOCK_STREAM : SOCK_DGRAM;
    hints.ai_flags = AI_NUMERICHOST;
    snprintf(service, sizeof service, "%u", ntohs(port_be));
    rc = getaddrinfo(host, port_be ? service : NULL, &hints, &res);
    if (rc != 0 || res == NULL) {
        if (rc == EAI_NONAME)
            tk_lan_only_count_lookup();
        tk_trace("Host_Get_Sockaddr(%s) not numeric: refused", host);
        return -TOS_ER_RESOLVE_FAILED;
    }

    if (res->ai_family == AF_INET6) {
        memcpy(out, res->ai_addr, sizeof *out);
        out->sin6_port = port_be;
    } else if (mode < 2) {
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)res->ai_addr;
        memset(out, 0, sizeof *out);
        out->sin6_family = AF_INET6;
        out->sin6_port = port_be;
        out->sin6_addr.s6_addr[10] = 0xff;
        out->sin6_addr.s6_addr[11] = 0xff;
        memcpy(&out->sin6_addr.s6_addr[12], &v4->sin_addr, 4);
    } else {
        memcpy(out, res->ai_addr, sizeof(struct sockaddr_in));
    }
    freeaddrinfo(res);
    return 0;
}

TK_EXPORT int tutk_Sockaddr_To_Ip_String(const struct sockaddr_in *sa, socklen_t out_size,
                                         unsigned int mode, char *out) {
    char text[INET_ADDRSTRLEN];
    struct sockaddr_in6 resolved;

    if (sa == NULL || out == NULL)
        return -1;
    inet_ntop(AF_INET, &sa->sin_addr, text, sizeof text);
    if (tutk_Host_Get_Sockaddr(text, TCONN_UDP, 0, mode, &resolved) != 0)
        return -TOS_ER_RESOLVE_FAILED;
    if (resolved.sin6_family == AF_INET6)
        inet_ntop(AF_INET6, &resolved.sin6_addr, out, out_size);
    else
        inet_ntop(AF_INET, &((struct sockaddr_in *)&resolved)->sin_addr, out, out_size);
    return 0;
}

int tutk_Sockaddr_Get_Content(const struct sockaddr *sa, int size, char *buf, uint16_t *port) {
    if (sa == NULL)
        return TUTK_ER_INVALID_ARG;
    if (sa->sa_family == AF_INET6) {
        const struct sockaddr_in6 *v6 = (const struct sockaddr_in6 *)sa;
        if (buf && size > 0 && !inet_ntop(AF_INET6, &v6->sin6_addr, buf, size))
            snprintf(buf, size, "?ipaddr?");
        if (port)
            *port = ntohs(v6->sin6_port);
    } else {
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)sa;
        if (buf && size > 0 && !inet_ntop(AF_INET, &v4->sin_addr, buf, size))
            snprintf(buf, size, "?ipaddr?");
        if (port)
            *port = ntohs(v4->sin_port);
    }
    return 0;
}

TK_EXPORT const char *tutk_platform_inet_ntop(int af, const void *src, char *dst, socklen_t size) {
    return inet_ntop(af, src, dst, size);
}

TK_EXPORT int DetermineIpv6Address(const char *text) { return strchr(text, ':') != NULL; }

/* NAT64 is never enabled: the only setter is a stub. */
TK_EXPORT int GetProcEnvVarUsingNat64(void) { return 0; }

TK_EXPORT int tutk_Net_GetInterfaceInfo(TNetInterfaceInfo *info) {
    struct ifreq reqs[64];
    struct ifconf ifc;
    int fd, n, count = 0;

    memset(info, 0, sizeof *info);
    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0)
        return -1;
    ifc.ifc_len = sizeof reqs;
    ifc.ifc_req = reqs;
    if (ioctl(fd, SIOCGIFCONF, &ifc) < 0) {
        close(fd);
        return -1;
    }
    n = ifc.ifc_len / (int)sizeof(struct ifreq);
    for (int i = 0; i < n && count < MAX_INTERFACES; i++) {
        struct ifreq r = reqs[i];
        in_addr_t addr;

        if (ioctl(fd, SIOCGIFFLAGS, &r) < 0)
            continue;
        if (!(r.ifr_flags & IFF_UP) || (r.ifr_flags & IFF_LOOPBACK))
            continue;
        if (ioctl(fd, SIOCGIFADDR, &r) < 0)
            continue;
        addr = ((struct sockaddr_in *)&r.ifr_addr)->sin_addr.s_addr;
        if (ioctl(fd, SIOCGIFNETMASK, &r) < 0)
            continue;
        info->addr[count] = addr;
        info->netmask[count] = ((struct sockaddr_in *)&r.ifr_netmask)->sin_addr.s_addr;
        count++;
    }
    info->count = (uint8_t)count;
    close(fd);
    for (int i = 0; i < count; i++)
        tk_trace("GetInterfaceInfo[%d] %08x/%08x", i, ntohl(info->addr[i]), ntohl(info->netmask[i]));
    return count;
}
