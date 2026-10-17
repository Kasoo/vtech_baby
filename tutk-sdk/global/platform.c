/* Platform layer: memory, threads, locks, random numbers, strings (kit: global/10-platform.md). */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "ossl.h"
#include "tk.h"

/* ---- memory ---- */

TK_EXPORT void *tutk_platform_malloc(int size) { return malloc((size_t)(long)size); }

TK_EXPORT void *tutk_platform_calloc(int count, int size) {
    return calloc((size_t)(long)count, (size_t)(long)size);
}

TK_EXPORT void tutk_platform_free(void *p) { free(p); }

/* ---- threads and locks ---- */

TK_EXPORT uint64_t tutk_platform_CreateTask(int *err, TThreadStart start, void *arg, char detach,
                                            int stack_size) {
    pthread_attr_t attr;
    pthread_t thread;
    int rc, e = 0;

    pthread_attr_init(&attr);
    if (stack_size > 0)
        pthread_attr_setstacksize(&attr, (size_t)stack_size);
    rc = pthread_create(&thread, &attr, start, arg);
    pthread_attr_destroy(&attr);
    if (rc != 0) {
        if (err)
            *err = -1;
        return (uint64_t)-1;
    }
    if (detach && pthread_detach(thread) != 0)
        e = -2;
    if (err)
        *err = e;
    return (uint64_t)thread;
}

TK_EXPORT int tutk_platform_WaitForTaskExit(uint64_t thread) {
    if (thread == 0)
        return 0;
    return pthread_join((pthread_t)thread, NULL);
}

TK_EXPORT void tutk_platform_set_thread_name(const char *name) {}

TK_EXPORT int tutk_platform_mutex_init(pthread_mutex_t *m) {
    pthread_mutexattr_t attr;
    int rc;

    pthread_mutexattr_init(&attr);
    pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
    rc = pthread_mutex_init(m, &attr);
    pthread_mutexattr_destroy(&attr);
    return rc;
}

TK_EXPORT int tutk_platform_mutex_destroy(pthread_mutex_t *m) { return pthread_mutex_destroy(m); }

/* ---- random numbers ---- */

TK_EXPORT void tutk_platform_srand(void) { srand48(time(NULL) + getpid()); }

TK_EXPORT long tutk_platform_rand(void) { return lrand48(); }

/* A value in [low, high]; the upper bound comes first. */
TK_EXPORT int tutk_platform_rand_range(int high, int low) {
    return low + (int)(lrand48() % (high - low + 1));
}

TK_EXPORT int TUTKSSL_rand(uint8_t *buf, int len) {
    tk_trace("TUTKSSL_rand(%d)", len);
    return RAND_bytes(buf, len) == 1 ? 0 : -1;
}

/* ---- strings ---- */

TK_EXPORT int tutk_platform_snprintf(char *buf, unsigned int size, const char *fmt, ...) {
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(buf, size, fmt, ap);
    va_end(ap);
    return n;
}
