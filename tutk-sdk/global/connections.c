/* The connection manager: socket objects and one thread that waits on them and delivers datagrams
 * (kit: global/40-connections.md). Only bound UDP sockets are implemented: TCP, TLS and client
 * connections exist to reach ThroughTek's servers, so they abort as unimplemented. */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "tk.h"

#define RECV_BUFFER_MAX   1540
#define SOCKET_BUFFER     1024000
#define FROM_HOST_SIZE    128
#define MAX_POLL          64

/* ---- reference-counted objects ---- */

typedef struct TObjectHeader {
    const void *vtbl;
    int         refcount;
    void      (*destroy)(void *o);
} TObjectHeader;

static int object_retain(void *o) {
    int n = __atomic_add_fetch(&((TObjectHeader *)o)->refcount, 1, __ATOMIC_ACQ_REL);

    tk_trace("retain %p -> %d", o, n);
    return n;
}

static int object_release(void *o) {
    TObjectHeader *h = o;
    int n = __atomic_sub_fetch(&h->refcount, 1, __ATOMIC_ACQ_REL);

    tk_trace("release %p -> %d", o, n);
    if (n == 0)
        h->destroy(o);
    return n;
}

/* ---- objects ---- */

typedef struct ConnManager ConnManager;

typedef struct Connection {
    TObjectHeader    hdr;
    int              fd;                  /* -1 until opened */
    int              transport;
    int              role;
    uint32_t         flags;
    int              stage;               /* TConnStage, guarded by the manager lock */
    ConnManager     *manager;
    uint32_t         recv_buffer_size;
    uint8_t         *recv_buf;
    pthread_mutex_t  recv_lock;           /* recursive; held while recv_cb runs */
    TRecvCb          recv_cb;
    void            *recv_arg;
    TConnErrorCb     error_cb;
    void            *error_arg;
    TConnReadyCb     ready_cb;
    void            *ready_arg;
    TConnErrorCb     failed_cb;
    void            *failed_arg;
    bool             adopted;             /* in the manager thread's list; touched only there */
} Connection;

enum {
    TCONN_STAGE_NONE        = 0,
    TCONN_STAGE_CONNECTING  = 1,
    TCONN_STAGE_ESTABLISHED = 2,
    TCONN_STAGE_LISTENING   = 3,
    TCONN_STAGE_CLOSED      = 4,
};

enum { CMD_ADD = 0, CMD_RESET_LISTEN = 1, CMD_REMOVE = 2, CMD_CALL = 3 };

typedef struct Command {
    int         type;
    Connection *conn;
} Command;

struct ConnManager {
    TObjectHeader   hdr;
    TutkList       *commands;
    pthread_mutex_t lock;                 /* recursive; guards connection fds and stages */
    uint64_t        thread;
    volatile int    stop;
    volatile int    poll_interval_ms;
    TConnTickCb     tick_cb;
    void           *tick_arg;
    int             wake_pipe[2];         /* wake_pipe[0] is -1 if pipe() failed */
    TList          *conns;                /* the adopted connections; touched only by the thread */
};

static ConnManager *g_manager;
static pthread_mutex_t g_manager_lock = PTHREAD_MUTEX_INITIALIZER;

static void wake(ConnManager *m) {
    if (m->wake_pipe[0] >= 0)
        (void)!write(m->wake_pipe[1], "w", 1);
}

static int queue(ConnManager *m, int type, Connection *c) {
    Command *cmd = malloc(sizeof *cmd);

    if (cmd == NULL)
        return -TOS_ER_NO_MEMORY;
    cmd->type = type;
    cmd->conn = c;
    if (TutkList_push(m->commands, cmd) != 0) {
        free(cmd);
        return -TOS_ER_NO_MEMORY;
    }
    wake(m);
    return 0;
}

static int conn_error(int err) {
    switch (err) {
    case ENETUNREACH:  return TCONN_ERR_NET_UNREACHABLE;
    case ETIMEDOUT:    return TCONN_ERR_TIMED_OUT;
    case ECONNREFUSED: return TCONN_ERR_REFUSED;
    case EBADF:        return TCONN_ERR_BAD_FD;
    default:           return TCONN_ERR_OTHER;
    }
}

/* A failed socket call's return value: a negated TOS_ER code for the errno. The kit lists the
 * errnos that matter; any other gets the generic code. */
static int socket_error(int err) {
    switch (err) {
    case EBADF:        return -0x1100108;
    case EAGAIN:       return -0x110010a;
    case EINVAL:       return -0x1100115;
    case EPIPE:        return -0x110011f;
    case ENETUNREACH:  return -0x1100162;
    case ECONNRESET:   return -0x1100165;
    case ETIMEDOUT:    return -0x110016b;
    case ECONNREFUSED: return -0x110016c;
    case EHOSTUNREACH: return -0x110016e;
    default:           return -0x110017d;
    }
}

/* ---- method-table slots that are not implemented ---- */

#define SLOT_STUB(fn, label) static void fn(void) { tk_unimplemented(label); }
SLOT_STUB(mgr_release_stub, "TConnManager.release")
SLOT_STUB(mgr_reset_listen_stub, "TConnManager.resetListen")
SLOT_STUB(mgr_call_on_thread_stub, "TConnManager.callOnThread")
SLOT_STUB(conn_set_accept_callback_stub, "TConnection.setAcceptCallback")
SLOT_STUB(conn_send_to_peer_stub, "TConnection.sendToPeer")
SLOT_STUB(conn_get_stage_stub, "TConnection.getStage")
SLOT_STUB(conn_get_local_endpoint_stub, "TConnection.getLocalEndpoint")
SLOT_STUB(conn_get_option_stub, "TConnection.getOption")
SLOT_STUB(conn_set_reassemble_stub, "TConnection.setReassemble")

/* ---- connections ---- */

static void conn_destroy(void *o) {
    Connection *c = o;

    if (c->fd >= 0)
        close(c->fd);
    tutk_platform_mutex_destroy(&c->recv_lock);
    free(c->recv_buf);
    free(c);
}

static int conn_open(TConnection *tc, const char *host, uint16_t *port, int unused,
                     TConnReadyCb ready, void *ready_arg, TConnErrorCb failed, void *failed_arg) {
    Connection *c = (Connection *)tc;
    struct sockaddr_in6 addr;
    int fd, one = 1, hops = 1, ifindex = 0, sockbuf = SOCKET_BUFFER;

    c->ready_cb = ready;
    c->ready_arg = ready_arg;
    c->failed_cb = failed;
    c->failed_arg = failed_arg;

    tk_trace("open(%p, host=%s, port=%u)", (void *)c, host ? host : "NULL", port ? ntohs(*port) : 0);
    if (c->transport != TCONN_UDP || c->role != TCONN_ROLE_BOUND)
        tk_unimplemented("TConnection.open (TCP or client)");
    /* A bound socket with port 0 gets an ephemeral port, returned through *port. */
    if (tutk_Host_Get_Sockaddr(host, TCONN_UDP, port ? *port : 0, 0, &addr) != 0) {
        if (failed)
            failed(tc, TCONN_ERR_RESOLVE_FAILED, failed_arg);
        return -TOS_ER_RESOLVE_FAILED;
    }

    fd = socket(addr.sin6_family, SOCK_DGRAM, 0);
    if (fd < 0)
        goto fail;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
    if (addr.sin6_family == AF_INET6) {
        struct ipv6_mreq group = {.ipv6mr_interface = 0};

        inet_pton(AF_INET6, "ff02::1", &group.ipv6mr_multiaddr);
        setsockopt(fd, IPPROTO_IPV6, IPV6_MULTICAST_IF, &ifindex, sizeof ifindex);
        setsockopt(fd, IPPROTO_IPV6, IPV6_MULTICAST_HOPS, &hops, sizeof hops);
        setsockopt(fd, IPPROTO_IPV6, IPV6_JOIN_GROUP, &group, sizeof group);
    }
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &sockbuf, sizeof sockbuf);
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sockbuf, sizeof sockbuf);

    if (bind(fd, (struct sockaddr *)&addr,
             addr.sin6_family == AF_INET6 ? sizeof addr : sizeof(struct sockaddr_in)) != 0)
        goto fail;
    if (port && *port == 0) {
        struct sockaddr_in6 bound;
        socklen_t len = sizeof bound;

        if (getsockname(fd, (struct sockaddr *)&bound, &len) == 0)
            *port = bound.sin6_port;          /* the port is at the same offset for IPv4 */
    }

    pthread_mutex_lock(&c->manager->lock);
    c->fd = fd;
    c->stage = TCONN_STAGE_ESTABLISHED;
    pthread_mutex_unlock(&c->manager->lock);
    wake(c->manager);
    tk_trace("open(%p) -> 0, fd %d, port %u", (void *)c, fd, port ? ntohs(*port) : 0);
    if (ready)
        ready(tc, ready_arg);
    return 0;

fail: {
        int saved = errno;

        tk_trace("open(%p) failed: errno %d", (void *)c, saved);
        if (fd >= 0)
            close(fd);
        if (failed)
            failed(tc, conn_error(saved), failed_arg);
        return socket_error(saved);
    }
}

static int conn_send(TConnection *tc, const char *host, uint16_t port, const void *data, int *len) {
    Connection *c = (Connection *)tc;
    struct sockaddr_in6 dst;
    ssize_t n;
    int fd, stage;

    pthread_mutex_lock(&c->manager->lock);
    fd = c->fd;
    stage = c->stage;
    pthread_mutex_unlock(&c->manager->lock);
    if (stage != TCONN_STAGE_ESTABLISHED || len == NULL) {
        tk_trace("send(%p): stage %d", (void *)c, stage);
        return -TOS_ER_INVALID_ARG;
    }

    if (host) {
        if (tutk_Host_Get_Sockaddr(host, TCONN_UDP, port, 0, &dst) != 0)
            return -TOS_ER_RESOLVE_FAILED;
        n = sendto(fd, data, (size_t)*len, 0, (struct sockaddr *)&dst, sizeof dst);
    } else {
        n = sendto(fd, data, (size_t)*len, 0, NULL, 0);
    }
    tk_trace("send(%p, %s, %u, %d) -> %zd", (void *)c, host ? host : "NULL", ntohs(port), *len, n);
    if (n < 0)
        return socket_error(errno);
    *len = (int)n;
    return 0;
}

static int conn_set_receive_callback(TConnection *tc, TRecvCb cb, void *arg) {
    Connection *c = (Connection *)tc;

    tk_trace("setReceiveCallback(%p)", (void *)c);
    pthread_mutex_lock(&c->recv_lock);
    c->recv_cb = cb;
    c->recv_arg = arg;
    pthread_mutex_unlock(&c->recv_lock);
    return 0;
}

static int conn_set_error_callback(TConnection *tc, TConnErrorCb cb, void *arg) {
    Connection *c = (Connection *)tc;

    tk_trace("setErrorCallback(%p)", (void *)c);
    c->error_cb = cb;
    c->error_arg = arg;
    return 0;
}

static int conn_set_unicast_hops(TConnection *tc, int hops) {
    Connection *c = (Connection *)tc;

    tk_trace("setUnicastHops(%p, %d)", (void *)c, hops);
    if (c == NULL)
        return -TOS_ER_INVALID_ARG;
    if (setsockopt(c->fd, IPPROTO_IPV6, IPV6_UNICAST_HOPS, &hops, sizeof hops) != 0)
        return socket_error(errno);
    return 0;
}

static int conn_clear_callbacks(TConnection *tc) {
    Connection *c = (Connection *)tc;

    c->ready_cb = NULL;
    c->failed_cb = NULL;
    c->error_cb = NULL;
    pthread_mutex_lock(&c->recv_lock);
    c->recv_cb = NULL;
    pthread_mutex_unlock(&c->recv_lock);
    return 0;
}

static int conn_set_option(TConnection *tc, int option, const void *value) {
    Connection *c = (Connection *)tc;
    uint32_t size;

    if (option != TCONN_OPT_RECV_BUFFER_SIZE)
        tk_unimplemented("TConnection.setOption (option other than the receive buffer size)");
    memcpy(&size, value, sizeof size);
    tk_trace("setOption(%p, recv buffer %u)", (void *)c, size);
    pthread_mutex_lock(&c->recv_lock);
    c->recv_buffer_size = size < RECV_BUFFER_MAX ? size : RECV_BUFFER_MAX;
    free(c->recv_buf);
    c->recv_buf = NULL;                       /* reallocated at the next receive */
    pthread_mutex_unlock(&c->recv_lock);
    return 0;
}

static const TConnectionVtbl connection_vtbl = {
    .retain             = (int (*)(TConnection *))object_retain,
    .release            = (int (*)(TConnection *))object_release,
    .open               = conn_open,
    .setAcceptCallback  = (void *)conn_set_accept_callback_stub,
    .sendToPeer         = (void *)conn_send_to_peer_stub,
    .send               = conn_send,
    .setReceiveCallback = conn_set_receive_callback,
    .setErrorCallback   = conn_set_error_callback,
    .getStage           = (void *)conn_get_stage_stub,
    .getLocalEndpoint   = (void *)conn_get_local_endpoint_stub,
    .setUnicastHops     = conn_set_unicast_hops,
    .setOption          = conn_set_option,
    .getOption          = (void *)conn_get_option_stub,
    .setReassemble      = (void *)conn_set_reassemble_stub,
    .clearCallbacks     = conn_clear_callbacks,
};

/* Receive one datagram on the manager thread and deliver it, or report a failed socket. */
static void conn_receive(ConnManager *m, Connection *c) {
    struct sockaddr_storage from;
    socklen_t from_len = sizeof from;
    char from_host[FROM_HOST_SIZE] = "";
    uint16_t from_port = 0;                    /* network byte order, as send takes it */
    ssize_t n;

    pthread_mutex_lock(&c->recv_lock);
    if (c->recv_buf == NULL && (c->recv_buf = malloc(c->recv_buffer_size)) == NULL) {
        pthread_mutex_unlock(&c->recv_lock);
        return;
    }
    n = recvfrom(c->fd, c->recv_buf, c->recv_buffer_size, 0, (struct sockaddr *)&from, &from_len);
    if (n < 0) {
        int err = errno == EBADF ? TCONN_ERR_BAD_FD : TCONN_ERR_PEER_CLOSED;

        pthread_mutex_unlock(&c->recv_lock);
        tk_trace("receive(%p) failed: errno %d", (void *)c, errno);
        pthread_mutex_lock(&m->lock);
        c->stage = TCONN_STAGE_CLOSED;
        pthread_mutex_unlock(&m->lock);
        if (c->error_cb)
            c->error_cb((TConnection *)c, err, c->error_arg);
        return;
    }
    if (from.ss_family == AF_INET6) {
        const struct sockaddr_in6 *v6 = (const struct sockaddr_in6 *)&from;
        inet_ntop(AF_INET6, &v6->sin6_addr, from_host, sizeof from_host);
        from_port = v6->sin6_port;
    } else if (from.ss_family == AF_INET) {
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)&from;
        inet_ntop(AF_INET, &v4->sin_addr, from_host, sizeof from_host);
        from_port = v4->sin_port;
    }
    tk_trace("receive(%p) %zd bytes from [%s]:%u", (void *)c, n, from_host, ntohs(from_port));
    if (c->recv_cb)
        c->recv_cb((TConnection *)c, from_host, from_port, c->recv_buf, (long)n, c->recv_arg);
    pthread_mutex_unlock(&c->recv_lock);
    tk_trace("receive(%p) delivered", (void *)c);
}

/* ---- the manager ---- */

static void drain_commands(ConnManager *m) {
    Command *cmd;

    while ((cmd = TutkList_pop(m->commands)) != NULL) {
        switch (cmd->type) {
        case CMD_ADD:
            tlistAppend(m->conns, cmd->conn);
            cmd->conn->adopted = true;
            break;
        case CMD_REMOVE:
            if (tlistRemove(m->conns, cmd->conn) == 0) {
                cmd->conn->adopted = false;
                object_release(cmd->conn);
            }
            break;
        default:
            tk_unimplemented("TConnManager command");
        }
        free(cmd);
    }
}

static void *manager_thread(void *arg) {
    ConnManager *m = arg;

    tutk_platform_set_thread_name("iotc_conn_hdlr");
    while (!m->stop) {
        struct pollfd fds[MAX_POLL + 1];
        Connection *polled[MAX_POLL + 1];
        int nfds = 0, ready;

        if (m->wake_pipe[0] >= 0) {
            fds[nfds] = (struct pollfd){.fd = m->wake_pipe[0], .events = POLLIN};
            polled[nfds++] = NULL;
        }
        pthread_mutex_lock(&m->lock);
        for (TListNode *node = tlistHead(m->conns); node && nfds <= MAX_POLL; node = node->next) {
            Connection *c = node->data;
            if (c->fd >= 0 && c->stage == TCONN_STAGE_ESTABLISHED) {
                fds[nfds] = (struct pollfd){.fd = c->fd, .events = POLLIN};
                polled[nfds++] = c;
            }
        }
        pthread_mutex_unlock(&m->lock);

        ready = poll(fds, (nfds_t)nfds, m->poll_interval_ms);
        tk_trace("manager: poll %d fds -> %d", nfds, ready);
        drain_commands(m);
        if (m->stop)
            break;
        if (ready == 0) {
            if (m->tick_cb)
                m->tick_cb((TConnManager *)m, m->tick_arg);
            continue;
        }
        if (ready < 0)
            continue;
        if (m->wake_pipe[0] >= 0 && (fds[0].revents & POLLIN)) {
            char buf[64];
            (void)!read(m->wake_pipe[0], buf, sizeof buf);
            continue;
        }
        for (int i = 0; i < nfds; i++) {
            Connection *c = polled[i];
            /* A connection removed by a command drained above is skipped. */
            if (c && c->adopted && (fds[i].revents & (POLLIN | POLLERR | POLLHUP)))
                conn_receive(m, c);
        }
    }
    return NULL;
}

static int mgr_create_connection(TConnManager *tm, int transport, int role, uint32_t flags,
                                 TConnection **out) {
    ConnManager *m = (ConnManager *)tm;
    Connection *c;

    if (transport != TCONN_UDP || role != TCONN_ROLE_BOUND || (flags & TCONN_FLAG_TLS))
        tk_unimplemented("TConnManager.createConnection (TCP, TLS or client)");
    if (out == NULL)
        return -TOS_ER_INVALID_ARG;
    c = calloc(1, sizeof *c);
    if (c == NULL)
        return -TOS_ER_NO_MEMORY;
    c->hdr.vtbl = &connection_vtbl;
    c->hdr.refcount = 1;
    c->hdr.destroy = conn_destroy;
    c->fd = -1;
    c->transport = transport;
    c->role = role;
    c->flags = flags;
    c->manager = m;
    c->recv_buffer_size = RECV_BUFFER_MAX;
    tutk_platform_mutex_init(&c->recv_lock);

    tk_trace("createConnection(%d, %d, 0x%x) -> %p", transport, role, flags, (void *)c);
    object_retain(c);                         /* the manager's reference, dropped on removal */
    queue(m, CMD_ADD, c);
    *out = (TConnection *)c;
    return 0;
}

static int mgr_remove_connection(TConnManager *tm, TConnection *tc) {
    tk_trace("removeConnection(%p)", (void *)tc);
    if (tm == NULL || tc == NULL)
        return -TOS_ER_INVALID_ARG;
    tc->vtbl->clearCallbacks(tc);
    return queue((ConnManager *)tm, CMD_REMOVE, (Connection *)tc);
}

static int mgr_set_poll_interval(TConnManager *tm, int ms) {
    tk_trace("setPollIntervalMs(%d)", ms);
    ((ConnManager *)tm)->poll_interval_ms = ms;
    return 0;
}

static int mgr_set_tick_callback(TConnManager *tm, TConnTickCb cb, void *arg) {
    ConnManager *m = (ConnManager *)tm;

    tk_trace("setTickCallback(%p)", (void *)cb);
    m->tick_arg = arg;
    m->tick_cb = cb;
    return 0;
}

static const TConnManagerVtbl manager_vtbl = {
    .retain            = (int (*)(TConnManager *))object_retain,
    .release           = (int (*)(TConnManager *))mgr_release_stub,
    .createConnection  = mgr_create_connection,
    .resetListen       = (void *)mgr_reset_listen_stub,
    .removeConnection  = mgr_remove_connection,
    .setPollIntervalMs = mgr_set_poll_interval,
    .setTickCallback   = mgr_set_tick_callback,
    .callOnThread      = (void *)mgr_call_on_thread_stub,
};

TK_EXPORT int TConnManager_create(TConnManager **out) {
    ConnManager *m;
    int err;

    pthread_mutex_lock(&g_manager_lock);
    if (g_manager) {
        object_retain(g_manager);
        *out = (TConnManager *)g_manager;
        pthread_mutex_unlock(&g_manager_lock);
        return 0;
    }
    m = calloc(1, sizeof *m);
    if (m == NULL || (m->conns = tlistNew()) == NULL || TutkListNew(&m->commands) != 0) {
        if (m)
            tlistDestroy(m->conns);
        free(m);
        pthread_mutex_unlock(&g_manager_lock);
        return -TOS_ER_NO_MEMORY;
    }
    m->hdr.vtbl = &manager_vtbl;
    m->hdr.refcount = 1;
    m->poll_interval_ms = 50;
    tutk_platform_mutex_init(&m->lock);
    if (pipe(m->wake_pipe) != 0)
        m->wake_pipe[0] = m->wake_pipe[1] = -1;
    m->thread = tutk_platform_CreateTask(&err, manager_thread, m, 0, 0);
    g_manager = m;
    *out = (TConnManager *)m;
    pthread_mutex_unlock(&g_manager_lock);
    return 0;
}
