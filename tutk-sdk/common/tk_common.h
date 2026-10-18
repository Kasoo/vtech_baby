/* Declarations every library shares: export markers, the abort stub and the debug trace. */
#ifndef TK_COMMON_H
#define TK_COMMON_H

#define TK_EXPORT __attribute__((visibility("default")))
#define TK_HIDDEN __attribute__((visibility("hidden")))

/* Report a call into a function that is not implemented, then abort. */
void tk_unimplemented(const char *name) TK_HIDDEN __attribute__((noreturn));

/* An exported symbol that aborts when called; its arguments are ignored. */
#define TK_STUB(name) TK_EXPORT void name(void) { tk_unimplemented(#name); }

/* Debug trace to stderr. Built in only with TK_DEBUG (make DEBUG=1), and then written only when the
 * environment has TK_TRACE=1. */
#ifdef TK_DEBUG
void tk_trace(const char *fmt, ...) TK_HIDDEN __attribute__((format(printf, 1, 2)));
#else
static inline __attribute__((format(printf, 1, 2))) void tk_trace(const char *fmt, ...) { (void)fmt; }
#endif

#endif
