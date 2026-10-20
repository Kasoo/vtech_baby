/* DEBUG builds only: the DTLS plaintext tap. tutk_third_SSL_read and tutk_third_SSL_write log every
 * successful call's bytes, in hex, to the file named by TK_SSL_TAP (or to stderr, prefixed "tk-ssl: ",
 * when TK_TRACE=2), then return as OpenSSL did. Otherwise they only forward. Release builds forward both through the generated tail-jumps instead. */
#ifdef TK_DEBUG
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "ossl.h"
#include "tk.h"

static pthread_mutex_t tap_lock = PTHREAD_MUTEX_INITIALIZER;
static int tap_fd = -2;

static int tap_open(void) {
    pthread_mutex_lock(&tap_lock);
    if (tap_fd == -2) {
        const char *path = getenv("TK_SSL_TAP"), *trace = getenv("TK_TRACE");
        if (path && path[0])
            tap_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
        else
            tap_fd = trace && trace[0] == '2' ? 2 : -1;
    }
    pthread_mutex_unlock(&tap_lock);
    return tap_fd;
}

/* One line per call: milliseconds, direction, SSL handle, length, bytes. */
static void tap_log(char dir, const void *ssl, const void *buf, int len) {
    static const char hex[] = "0123456789abcdef";
    const unsigned char *p = buf;
    struct timespec ts;
    char *line;
    int fd = tap_open(), n;

    if (fd < 0 || len <= 0)
        return;
    line = malloc(80 + 2 * (size_t)len + 1);
    if (!line)
        return;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    n = snprintf(line, 80, "%s%lld.%03ld %c %p %d ", fd == 2 ? "tk-ssl: " : "", (long long)ts.tv_sec, ts.tv_nsec / 1000000, dir,
                 ssl, len);
    for (int i = 0; i < len; i++) {
        line[n++] = hex[p[i] >> 4];
        line[n++] = hex[p[i] & 15];
    }
    line[n++] = '\n';
    pthread_mutex_lock(&tap_lock);
    write(fd, line, (size_t)n);
    pthread_mutex_unlock(&tap_lock);
    free(line);
}

static long long mono_ms(void) {
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000LL + ts.tv_nsec / 1000000;
}

TK_EXPORT int tutk_third_SSL_read(SSL *s, void *buf, int num) {
    long long t0 = mono_ms();
    int rc = SSL_read(s, buf, num);
    long long dt = mono_ms() - t0;

    if (dt >= 100 && tap_open() >= 0) {
        char line[96];
        int n = snprintf(line, sizeof line, "%sslow SSL_read %lld ms rc %d\n",
                         tap_fd == 2 ? "tk-ssl: " : "", dt, rc);
        write(tap_fd, line, (size_t)n);
    }
    tap_log('R', s, buf, rc);
    return rc;
}

TK_EXPORT int tutk_third_SSL_write(SSL *s, const void *buf, int num) {
    int rc = SSL_write(s, buf, num);
    tap_log('W', s, buf, rc);
    return rc;
}
#endif
