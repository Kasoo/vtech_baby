/* Linked into every library: the abort stub and the debug trace. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "tk_common.h"

void tk_unimplemented(const char *name) {
    static const char prefix[] = "unimplemented: ";
    write(2, prefix, sizeof prefix - 1);
    write(2, name, strlen(name));
    write(2, "\n", 1);
    abort();
}

#ifdef TK_DEBUG
void tk_trace(const char *fmt, ...) {
    static int enabled = -1;
    char line[512];
    va_list ap;
    int n;

    if (enabled < 0) {
        const char *v = getenv("TK_TRACE");
        enabled = v && (v[0] == '1' || v[0] == '2');
    }
    if (!enabled)
        return;
    n = snprintf(line, sizeof line, "tk: ");
    va_start(ap, fmt);
    n += vsnprintf(line + n, sizeof line - (size_t)n, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof line - 2)
        n = (int)sizeof line - 2;
    line[n++] = '\n';
    write(2, line, (size_t)n);
}

void tk_hex(const char *tag, char dir, long id, const void *buf, int len) {
    static const char hex[] = "0123456789abcdef";
    static int enabled = -1;
    const unsigned char *p = buf;
    struct timespec ts;
    char *line;
    int n;

    if (enabled < 0) {
        const char *v = getenv("TK_TRACE");
        enabled = v && v[0] == '2';
    }
    if (!enabled || len <= 0)
        return;
    line = malloc(96 + 2 * (size_t)len + 1);
    if (!line)
        return;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    n = snprintf(line, 96, "tk-%s: %lld.%03ld %c %ld %d ", tag, (long long)ts.tv_sec, ts.tv_nsec / 1000000,
                 dir, id, len);
    for (int i = 0; i < len; i++) {
        line[n++] = hex[p[i] >> 4];
        line[n++] = hex[p[i] & 15];
    }
    line[n++] = '\n';
    write(2, line, (size_t)n);
    free(line);
}
#endif
