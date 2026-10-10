/*
 * companion/misc.c -- host-missing accessors + libc-independent sysconf + the __sF object
 *                     + bionic system properties + stat/fstat forwarders.
 *
 * All exported under renamed tokens; every host primitive called here (__errno_location,
 * __h_errno_location, getauxval, sched_getaffinity, getrlimit, sysinfo, the getrandom syscall)
 * exists on BOTH glibc and musl, so the single glibc-built companion runs on musl too.
 *
 *   __Errno / __Get_h_errno   bionic's errno / h_errno accessors -> host *_location().
 *   Arc4random                bionic arc4random -> the getrandom(2) SYSCALL directly, so this
 *                             does NOT pull glibc's arc4random@GLIBC_2.36 (would raise the
 *                             version floor) and does NOT need musl's (musl lacks arc4random).
 *   Res_init                  "reinitialize the resolver" -> no-op success (safe for callers).
 *   Sysconf                   the caller passes BIONIC _SC_* numbers (it's bionic-compiled),
 *                             which differ from host numbering -> map bionic->value
 *                             libc-independently. Unmapped queries log + return -1/EINVAL.
 *   __System_property_get     bionic-only (on aarch64 libs: compiler-rt's outline-atomics init
 *                             constructor asks for "ro.arch" at dlopen time). There is no property
 *                             service on a host: answer "unset" (empty value, return 0), which is
 *                             what bionic itself returns for a missing key; the caller then falls
 *                             back to AT_HWCAP for its LSE-instruction detection.
 *   Stat / Fstat               forward to the host stat/fstat. The struct layout is identical across
 *                             bionic, glibc and musl on both x86_64 and aarch64; the rename exists
 *                             purely because glibc below 2.33 exports no `stat`/`fstat` symbol at
 *                             all (only the older `__xstat`/`__fxstat` interface), so a direct bind
 *                             fails on an old floor. Built on a low glibc floor these calls compile
 *                             down to __xstat/__fxstat, which every glibc version and musl both export.
 *   __SF                      bionic's stdio stream array (stdin/stdout/stderr = &__sF[0..2]).
 *                             Placeholder object: only its ADDRESS is taken at runtime. If a
 *                             TUTK path ever does real FILE* I/O through it, that surfaces as a
 *                             crash and needs a real stream-translation (not on the
 *                             session/telemetry/control path).
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <errno.h>
#include <string.h>
#include <sched.h>
#include <sys/auxv.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/sysinfo.h>
#include <sys/syscall.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))

extern int *__errno_location(void);
extern int *__h_errno_location(void);

EXPORT int *__Errno(void)       { return __errno_location(); }
EXPORT int *__Get_h_errno(void) { return __h_errno_location(); }

EXPORT int Res_init(void) { return 0; }

#ifndef SYS_getrandom      /* old headers only; <sys/syscall.h> normally provides the per-arch number */
#if defined(__x86_64__)
#define SYS_getrandom 318
#elif defined(__aarch64__)
#define SYS_getrandom 278
#else
#error "unknown arch: SYS_getrandom"
#endif
#endif
EXPORT uint32_t Arc4random(void) {
    uint32_t v;
    long n = syscall(SYS_getrandom, &v, sizeof v, 0);
    if (n == (long)sizeof v) return v;
    /* Fallback (getrandom unavailable): mix a couple of clocks -- non-crypto, never expected. */
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_nsec * 2654435761u) ^ (uint32_t)ts.tv_sec;
}

/* ---- libc-independent sysconf over BIONIC _SC_* numbers (from the NDK sysroot) ---- */
enum {
    B_SC_ARG_MAX            = 0x0000,
    B_SC_CLK_TCK            = 0x0006,
    B_SC_NGROUPS_MAX        = 0x000a,
    B_SC_OPEN_MAX           = 0x000b,
    B_SC_PAGESIZE           = 0x0027,
    B_SC_PAGE_SIZE          = 0x0028,
    B_SC_THREAD_STACK_MIN   = 0x004c,
    B_SC_NPROCESSORS_CONF   = 0x0060,
    B_SC_NPROCESSORS_ONLN   = 0x0061,
    B_SC_PHYS_PAGES         = 0x0062,
    B_SC_AVPHYS_PAGES       = 0x0063,
};

static long nproc_online(void) {
    cpu_set_t set;
    if (sched_getaffinity(0, sizeof set, &set) == 0) {
        int n = CPU_COUNT(&set);
        if (n > 0) return n;
    }
    return 1;
}

EXPORT long Sysconf(int name) {
    switch (name) {
    case B_SC_PAGESIZE:
    case B_SC_PAGE_SIZE: {
        unsigned long p = getauxval(AT_PAGESZ);
        return p ? (long)p : 4096;
    }
    case B_SC_CLK_TCK: {
        unsigned long c = getauxval(AT_CLKTCK);
        return c ? (long)c : 100;
    }
    case B_SC_NPROCESSORS_CONF:
    case B_SC_NPROCESSORS_ONLN:
        return nproc_online();
    case B_SC_OPEN_MAX: {
        struct rlimit rl;
        if (getrlimit(RLIMIT_NOFILE, &rl) == 0 && rl.rlim_cur != RLIM_INFINITY)
            return (long)rl.rlim_cur;
        return 1024;
    }
    case B_SC_THREAD_STACK_MIN: return 16384;      /* bionic PTHREAD_STACK_MIN, same on x86_64 AND aarch64 */
    case B_SC_NGROUPS_MAX:      return 65536;      /* Linux NGROUPS_MAX */
    case B_SC_ARG_MAX:          return 131072;     /* bionic's ARG_MAX */
    case B_SC_PHYS_PAGES: {
        struct sysinfo si;
        if (sysinfo(&si) == 0) return (long)(si.totalram / (getauxval(AT_PAGESZ) ?: 4096) * si.mem_unit);
        return -1;
    }
    case B_SC_AVPHYS_PAGES: {
        struct sysinfo si;
        if (sysinfo(&si) == 0) return (long)(si.freeram / (getauxval(AT_PAGESZ) ?: 4096) * si.mem_unit);
        return -1;
    }
    default:
        fprintf(stderr, "[tutkabi] sysconf: unmapped bionic _SC_ %#x -> EINVAL\n", name);
        errno = EINVAL;
        return -1;
    }
}

/* bionic int __system_property_get(const char *name, char *value): value is PROP_VALUE_MAX (92) bytes;
 * returns the value length. "Not set" = empty string, 0. */
EXPORT int __System_property_get(const char *name, char *value) {
    (void)name;
    if (value) value[0] = '\0';
    return 0;
}

EXPORT int Stat(const char *path, struct stat *st) { return stat(path, st); }
EXPORT int Fstat(int fd, struct stat *st)          { return fstat(fd, st); }

/* __sF: placeholder data object (bionic 3-FILE stream array). Generously sized + zeroed;
 * only its address is used at runtime. Exported as the renamed token __SF.
 * Defined in C (not inline asm) on purpose: a top-level __asm__(".data ...") block leaves the
 * assembler in .data and drops the *following* functions into the non-executable .data section
 * (they then SIGSEGV on call). A plain C array lands in .bss and exports cleanly. */
EXPORT char __SF[480];
