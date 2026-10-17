#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "tk.h"

/* The SDK patcher identifies each library's SDK version by a string of this shape. */
__attribute__((used)) static const char tk_sdk_version[] = "4.3.8.1-tutk-sdk-lan-only";

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
        enabled = v && v[0] == '1';
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
#endif
