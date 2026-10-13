/*
 * companion/fortify.c -- the bionic _FORTIFY_SOURCE helpers, self-contained.
 *
 * These are the union of the host-missing set: glibc lacks __FD_SET_chk/__strlen_chk/
 * __strncpy_chk2; musl lacks ALL of the __*_chk family. So the companion DEFINES every one
 * itself over the plain base function (memcpy/strcpy/... exist on both libcs) -- it must not
 * IMPORT a host __*_chk, or it would fail to load on musl. Each reproduces bionic's semantics:
 * perform the operation, abort on a compile-time object-size bound violation (a real overflow).
 * Functional result (return value / bytes written) matches the unchecked call.
 *
 * Exported under the renamed tokens (__Memcpy_chk, __Strcpy_chk, __fD_SET_chk, ...); the base
 * calls (memcpy/strcpy/...) bind to the host by name.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <sys/select.h>

#define EXPORT __attribute__((visibility("default")))

static __attribute__((noreturn)) void fatal(const char *what) {
    fprintf(stderr, "[tutkabi] FORTIFY: %s: detected buffer overflow\n", what);
    abort();
}

/* size_t __strlen_chk(const char* s, size_t s_len): strlen, trap if it can't fit in s_len. */
EXPORT size_t __Strlen_chk(const char *s, size_t s_len) {
    size_t len = strlen(s);
    if (len >= s_len) fatal("__strlen_chk");
    return len;
}

/* void __FD_SET_chk(int fd, fd_set* set, size_t set_size): bounds-checked FD_SET. */
EXPORT void __fD_SET_chk(int fd, fd_set *set, size_t set_size) {
    if (fd < 0) fatal("__FD_SET_chk (fd < 0)");
    if ((size_t)(fd / 8 + 1) > set_size) fatal("__FD_SET_chk (fd too large for set)");
    FD_SET(fd, set);
}

/* char* __strncpy_chk2(dst, src, n, dst_len, src_len): strncpy with two object sizes. */
EXPORT char *__Strncpy_chk2(char *dst, const char *src, size_t n, size_t dst_len, size_t src_len) {
    if (n > dst_len) fatal("__strncpy_chk2 (n > dst buffer)");
    if (src_len < n && strnlen(src, src_len) == src_len) fatal("__strncpy_chk2 (src read overflow)");
    return strncpy(dst, src, n);
}

/* void* __memcpy_chk(dst, src, n, dst_len). */
EXPORT void *__Memcpy_chk(void *dst, const void *src, size_t n, size_t dst_len) {
    if (n > dst_len) fatal("__memcpy_chk");
    return memcpy(dst, src, n);
}

/* char* __strcpy_chk(dst, src, dst_len): strcpy, trap if src (incl NUL) exceeds dst_len. */
EXPORT char *__Strcpy_chk(char *dst, const char *src, size_t dst_len) {
    if (strlen(src) + 1 > dst_len) fatal("__strcpy_chk");
    return strcpy(dst, src);
}

/* char* __strncat_chk(dst, src, n, dst_len): strncat, trap if the result exceeds dst_len. */
EXPORT char *__Strncat_chk(char *dst, const char *src, size_t n, size_t dst_len) {
    size_t dl = strlen(dst);
    size_t sl = strnlen(src, n);
    if (dl + sl + 1 > dst_len) fatal("__strncat_chk");
    return strncat(dst, src, n);
}

/* char* __strncpy_chk(dst, src, n, dst_len): strncpy, trap if n exceeds dst_len. */
EXPORT char *__Strncpy_chk(char *dst, const char *src, size_t n, size_t dst_len) {
    if (n > dst_len) fatal("__strncpy_chk");
    return strncpy(dst, src, n);
}

/* int __vsprintf_chk(char* dst, int flags, size_t dst_len, const char* fmt, va_list ap):
 * bionic's checked vsprintf. We render bounded into dst; a result that would exceed dst_len
 * (i.e. vsnprintf's return >= dst_len) is the overflow bionic aborts on. Returns the count
 * that WOULD have been written (bionic/glibc semantics), matching an unchecked vsprintf. */
EXPORT int __Vsprintf_chk(char *dst, int flags, size_t dst_len, const char *fmt, va_list ap) {
    (void)flags;
    int n = vsnprintf(dst, dst_len, fmt, ap);
    if (n < 0) return n;
    if ((size_t)n >= dst_len) fatal("__vsprintf_chk");
    return n;
}
