/*
 * companion/net.c -- STRUCT translator for the getaddrinfo family.
 *
 * bionic and the host `struct addrinfo` have ai_addr and ai_canonname SWAPPED:
 *     host  : ... ai_addrlen; struct sockaddr *ai_addr; char *ai_canonname; ai_next;
 *     bionic: ... ai_addrlen; char *ai_canonname;      struct sockaddr *ai_addr; ai_next;
 * (bionic ai_canonname@24, ai_addr@32; host ai_addr@24, ai_canonname@32 -- glibc and musl agree.)
 * This calls the host getaddrinfo, then deep-copies into bionic-laid-out nodes; Freeaddrinfo frees
 * THOSE (so it must be ours too -- the host freeaddrinfo on our nodes would corrupt).
 *
 * Exported under the renamed tokens `Getaddrinfo`/`Freeaddrinfo`; calls the host by name
 * (no recursion, no .symver). getnameinfo is ABI-identical (sockaddr + char buffers) and is
 * NOT renamed -- it binds straight to the host.
 *
 * LAN-ONLY POLICY (always on; ../../docs/00-prerequisites.md "LAN-only enforcement"). The SDK races a
 * LAN search against ThroughTek's cloud (master DNS, STUN, P2P, relay) and has no switch to turn the
 * cloud off. The only dependable no-cloud condition is a client that cannot reach anything off-link,
 * so we recreate exactly that for the SDK's own calls (and only those -- these tokens are bound by the
 * patched TUTK libraries alone; the rest of the process uses libc directly):
 *   - Getaddrinfo resolves numeric literals only (AI_NUMERICHOST): no DNS query ever leaves. The LAN
 *     path needs no names -- discovery is a subnet broadcast and the camera's address comes back in
 *     the reply.
 *   - Connect / Sendto pass only IPv4 destinations on a directly attached subnet (host or directed
 *     broadcast), 255.255.255.255, or the same as an IPv4-mapped IPv6 address. Everything else --
 *     public IPv4, native IPv6, NAT64 64:ff9b:: -- fails at once with ENETUNREACH, which is what a
 *     host with no default route returns. The camera is IPv4-only, so no IPv6 is needed.
 * The SDK's network I/O is exactly socket/bind/connect/sendto/send/write/recv* (libTUTKGlobalAPIs) plus
 * getaddrinfo (libIOTCAPIs, libTUTKGlobalAPIs); its two raw syscall() sites are mlock2 and getrandom.
 * send/write reach only a socket Connect allowed. gethostbyname/getservbyname are loud-abort stubs.
 */
#include <errno.h>
#include <ifaddrs.h>
#include <netdb.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>

#define EXPORT __attribute__((visibility("default")))

/* bionic layout (canonname before addr). */
struct b_addrinfo {
    int ai_flags, ai_family, ai_socktype, ai_protocol;
    unsigned int ai_addrlen;
    char *ai_canonname;
    struct sockaddr *ai_addr;
    struct b_addrinfo *ai_next;
};

/* ---- LAN-only egress policy --------------------------------------------------------------- */

static unsigned long blocked_sends, blocked_lookups;

/* Directly attached IPv4 subnets, refreshed at most once a second (Sendto is on the media path). */
#define MAX_NETS 32
static struct { in_addr_t addr, mask; } nets[MAX_NETS];
static int n_nets;
static time_t nets_at;
static pthread_mutex_t nets_lock = PTHREAD_MUTEX_INITIALIZER;

static void refresh_nets(void) {
    struct ifaddrs *ifs;
    if (getifaddrs(&ifs) != 0) return;            /* keep the previous list */
    n_nets = 0;
    for (struct ifaddrs *i = ifs; i && n_nets < MAX_NETS; i = i->ifa_next) {
        if (!i->ifa_addr || i->ifa_addr->sa_family != AF_INET || !i->ifa_netmask) continue;
        nets[n_nets].addr = ((struct sockaddr_in *)i->ifa_addr)->sin_addr.s_addr;
        nets[n_nets].mask = ((struct sockaddr_in *)i->ifa_netmask)->sin_addr.s_addr;
        n_nets++;
    }
    freeifaddrs(ifs);
}

static int on_link_v4(in_addr_t dst) {             /* network byte order */
    if (dst == htonl(INADDR_BROADCAST)) return 1;
    int ok = 0;
    pthread_mutex_lock(&nets_lock);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (now.tv_sec != nets_at || n_nets == 0) { refresh_nets(); nets_at = now.tv_sec; }
    for (int k = 0; k < n_nets && !ok; k++)
        ok = (dst & nets[k].mask) == (nets[k].addr & nets[k].mask);
    pthread_mutex_unlock(&nets_lock);
    return ok;
}

/* 1 = may leave. No address (connected-socket sendto), AF_UNSPEC (UDP disconnect) and non-IP
 * families (AF_UNIX, AF_NETLINK) are not network egress and pass. */
static int allowed(const struct sockaddr *sa, socklen_t len) {
    if (!sa || len < sizeof(sa_family_t)) return 1;
    if (sa->sa_family == AF_INET && len >= sizeof(struct sockaddr_in))
        return on_link_v4(((const struct sockaddr_in *)sa)->sin_addr.s_addr);
    if (sa->sa_family == AF_INET6 && len >= sizeof(struct sockaddr_in6)) {
        const struct in6_addr *a = &((const struct sockaddr_in6 *)sa)->sin6_addr;
        if (IN6_IS_ADDR_V4MAPPED(a)) { in_addr_t v4; memcpy(&v4, &a->s6_addr[12], 4); return on_link_v4(v4); }
        return 0;
    }
    if (sa->sa_family == AF_INET || sa->sa_family == AF_INET6) return 0;   /* truncated: refuse */
    return 1;
}

EXPORT int Connect(int fd, const struct sockaddr *sa, socklen_t len) {
    if (!allowed(sa, len)) {
        __atomic_add_fetch(&blocked_sends, 1, __ATOMIC_RELAXED);
        errno = ENETUNREACH;
        return -1;
    }
    return connect(fd, sa, len);
}

EXPORT ssize_t Sendto(int fd, const void *buf, size_t n, int flags, const struct sockaddr *sa, socklen_t len) {
    if (!allowed(sa, len)) {
        __atomic_add_fetch(&blocked_sends, 1, __ATOMIC_RELAXED);
        errno = ENETUNREACH;
        return -1;
    }
    return sendto(fd, buf, n, flags, sa, len);
}

/* Diagnostics (not an SDK token): how many off-link sends/connects and name lookups were refused. */
EXPORT void tk_lan_only_stats(unsigned long *sends, unsigned long *lookups) {
    *sends = __atomic_load_n(&blocked_sends, __ATOMIC_RELAXED);
    *lookups = __atomic_load_n(&blocked_lookups, __ATOMIC_RELAXED);
}

EXPORT int Getaddrinfo(const char *node, const char *service,
                       const struct addrinfo *hints_, struct addrinfo **res_) {
    const struct b_addrinfo *bhints = (const struct b_addrinfo *)hints_;
    struct b_addrinfo **bres = (struct b_addrinfo **)res_;

    /* LAN-only: always pass hints so AI_NUMERICHOST can be forced (no DNS lookup, ever). */
    struct addrinfo ghints;
    memset(&ghints, 0, sizeof ghints);
    if (bhints) {
        ghints.ai_flags    = bhints->ai_flags;
        ghints.ai_family   = bhints->ai_family;
        ghints.ai_socktype = bhints->ai_socktype;
        ghints.ai_protocol = bhints->ai_protocol;
    }
    ghints.ai_flags |= AI_NUMERICHOST;
    struct addrinfo *glist = NULL;
    int rc = getaddrinfo(node, service, &ghints, &glist);
    if (rc != 0 && node) __atomic_add_fetch(&blocked_lookups, 1, __ATOMIC_RELAXED);
    if (rc != 0) { *bres = NULL; return rc; }

    /* Match bionic (the caller): ai_canonname is populated ONLY when AI_CANONNAME was requested.
     * musl's getaddrinfo fills ai_canonname even without the flag, so we must gate on it or a bionic
     * caller sees a non-NULL canonname it never asked for (a musl-only divergence). */
    int want_canon = bhints && (bhints->ai_flags & AI_CANONNAME);
    struct b_addrinfo *head = NULL, **tail = &head;
    for (struct addrinfo *g = glist; g; g = g->ai_next) {
        struct b_addrinfo *b = calloc(1, sizeof *b);
        b->ai_flags    = g->ai_flags;
        b->ai_family   = g->ai_family;
        b->ai_socktype = g->ai_socktype;
        b->ai_protocol = g->ai_protocol;
        b->ai_addrlen  = g->ai_addrlen;
        if (g->ai_addr && g->ai_addrlen) {
            b->ai_addr = malloc(g->ai_addrlen);
            memcpy(b->ai_addr, g->ai_addr, g->ai_addrlen);
        }
        if (want_canon && g->ai_canonname) b->ai_canonname = strdup(g->ai_canonname);
        *tail = b; tail = &b->ai_next;
    }
    freeaddrinfo(glist);
    *bres = head;
    return 0;
}

EXPORT void Freeaddrinfo(struct addrinfo *res_) {
    struct b_addrinfo *b = (struct b_addrinfo *)res_;
    while (b) {
        struct b_addrinfo *next = b->ai_next;
        free(b->ai_addr);
        free(b->ai_canonname);
        free(b);
        b = next;
    }
}
