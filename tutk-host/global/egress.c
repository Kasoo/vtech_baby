/* LAN-only egress: datagrams leave only for a directly attached IPv4 subnet (host or directed
 * broadcast) or 255.255.255.255, also as an IPv4-mapped IPv6 address. Anything else (public IPv4,
 * native IPv6, NAT64) fails at once with ENETUNREACH, as on a host with no default route. Names are
 * never resolved (getaddrinfo is numeric-only); a refused name is counted here. */
#include <arpa/inet.h>
#include <errno.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <pthread.h>
#include <string.h>
#include <time.h>

#include "tk.h"

#define MAX_NETS 32

static unsigned long refused_sends, refused_lookups;

/* Directly attached IPv4 subnets, refreshed at most once a second (sends are on the media path). */
static struct { in_addr_t addr, mask; } nets[MAX_NETS];
static int n_nets;
static time_t nets_at;
static pthread_mutex_t nets_lock = PTHREAD_MUTEX_INITIALIZER;

static void refresh_nets(void) {
    struct ifaddrs *ifs;

    if (getifaddrs(&ifs) != 0)
        return;                                     /* keep the previous list */
    n_nets = 0;
    for (struct ifaddrs *i = ifs; i && n_nets < MAX_NETS; i = i->ifa_next) {
        if (!i->ifa_addr || i->ifa_addr->sa_family != AF_INET || !i->ifa_netmask)
            continue;
        nets[n_nets].addr = ((struct sockaddr_in *)i->ifa_addr)->sin_addr.s_addr;
        nets[n_nets].mask = ((struct sockaddr_in *)i->ifa_netmask)->sin_addr.s_addr;
        n_nets++;
    }
    freeifaddrs(ifs);
}

static int on_link_v4(in_addr_t dst) {              /* network byte order */
    struct timespec now;
    int ok = 0;

    if (dst == htonl(INADDR_BROADCAST))
        return 1;
    pthread_mutex_lock(&nets_lock);
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (now.tv_sec != nets_at || n_nets == 0) {
        refresh_nets();
        nets_at = now.tv_sec;
    }
    for (int k = 0; k < n_nets && !ok; k++)
        ok = (dst & nets[k].mask) == (nets[k].addr & nets[k].mask);
    pthread_mutex_unlock(&nets_lock);
    return ok;
}

/* 1 if a datagram may go to `sa`; counts and sets ENETUNREACH otherwise. No address (a connected
 * socket) passes. */
int tk_egress_allowed(const struct sockaddr *sa, socklen_t len) {
    int ok = 1;

    if (sa && sa->sa_family == AF_INET)
        ok = len >= sizeof(struct sockaddr_in) && on_link_v4(((const struct sockaddr_in *)sa)->sin_addr.s_addr);
    else if (sa && sa->sa_family == AF_INET6) {
        const struct in6_addr *a = &((const struct sockaddr_in6 *)sa)->sin6_addr;
        in_addr_t v4;

        ok = len >= sizeof(struct sockaddr_in6) && IN6_IS_ADDR_V4MAPPED(a);
        if (ok) {
            memcpy(&v4, &a->s6_addr[12], sizeof v4);
            ok = on_link_v4(v4);
        }
    }
    if (!ok) {
        __atomic_add_fetch(&refused_sends, 1, __ATOMIC_RELAXED);
        errno = ENETUNREACH;
    }
    return ok;
}

/* A host name the numeric-only resolver refused (Global's and IOTC's). */
TK_EXPORT void tk_lan_only_count_lookup(void) { __atomic_add_fetch(&refused_lookups, 1, __ATOMIC_RELAXED); }

/* Diagnostics for the integration: how many off-link sends and name lookups were refused. */
TK_EXPORT void tk_lan_only_stats(unsigned long *sends, unsigned long *lookups) {
    *sends = __atomic_load_n(&refused_sends, __ATOMIC_RELAXED);
    *lookups = __atomic_load_n(&refused_lookups, __ATOMIC_RELAXED);
}
