/*
 * companion/stubs.c -- loud-abort placeholders for the ABI-divergent STRUCT watchlist.
 *
 * These bionic symbols are ABI-divergent (dirent / sigaction / passwd / hostent / servent /
 * struct tm layouts, GNU-vs-POSIX basename, GNU-vs-XSI strerror_r), so they are RENAMED to
 * companion tokens rather than left binding to the host with the wrong ABI. The session /
 * AV / IOCtrl telemetry / control path exercised by this client does not call any of them.
 * A real call traps LOUDLY with the original name, identifying precisely which one to promote
 * to a real translator -- a silent wrong-ABI bind is exactly what this avoids. The renamed
 * export just routes the trap through the companion.
 */
#include <stdio.h>
#include <stdlib.h>

#define EXPORT __attribute__((visibility("default")))

static __attribute__((noreturn)) void trap(const char *name) {
    fprintf(stderr, "[tutkabi] UNIMPLEMENTED divergent symbol called: %s\n"
                    "  (STRUCT watchlist -- promote to a real translator; see companion/stubs.c)\n", name);
    abort();
}

#define STUB(TOKEN, ORIG) \
    EXPORT __attribute__((noreturn)) void TOKEN(void) { trap(ORIG); }

STUB(Basename,      "basename")
STUB(Gethostbyname, "gethostbyname")
STUB(Getpwuid_r,    "getpwuid_r")
STUB(Getservbyname, "getservbyname")
STUB(Gmtime_r,      "gmtime_r")
STUB(Localtime_r,   "localtime_r")
STUB(Readdir,       "readdir")
STUB(Sigaction,     "sigaction")
STUB(Strerror_r,    "strerror_r")
STUB(Strftime,      "strftime")
