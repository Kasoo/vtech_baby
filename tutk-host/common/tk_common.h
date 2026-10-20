/* Declarations every library shares: export markers, the abort stub and the debug trace. */
#ifndef TK_COMMON_H
#define TK_COMMON_H

#define TK_EXPORT __attribute__((visibility("default")))

/* The SDK version this build advertises to the camera, packed major.minor.patch.build: 4.3.8.100.
 * Build 100 is no TUTK release, so this build is recognisable on the wire; it sorts above the 4.3.8.1
 * the stock libraries send, so a camera-side minimum-version check still passes. */
#define TK_SDK_VERSION 0x04030864
#define TK_HIDDEN __attribute__((visibility("hidden")))

/* Report a call into a function that is not implemented, then abort. */
void tk_unimplemented(const char *name) TK_HIDDEN __attribute__((noreturn));

/* An exported symbol that aborts when called; its arguments are ignored. */
#define TK_STUB(name) TK_EXPORT void name(void) { tk_unimplemented(#name); }

/* Debug trace to stderr. Built in only with TK_DEBUG (make DEBUG=1), and then written only when the
 * environment has TK_TRACE=1 (or 2, which adds the hex dumps). */
/* tk_hex logs `len` bytes in hex as "tk-<tag>: <s.ms> <dir> <id> <len> <hex>", only when TK_TRACE=2. */
#ifdef TK_DEBUG
void tk_trace(const char *fmt, ...) TK_HIDDEN __attribute__((format(printf, 1, 2)));
void tk_hex(const char *tag, char dir, long id, const void *buf, int len) TK_HIDDEN;
#else
static inline __attribute__((format(printf, 1, 2))) void tk_trace(const char *fmt, ...) { (void)fmt; }
static inline void tk_hex(const char *tag, char dir, long id, const void *buf, int len) {}
#endif

#endif
