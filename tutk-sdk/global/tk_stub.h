#ifndef TK_STUB_H
#define TK_STUB_H

#define TK_EXPORT __attribute__((visibility("default")))

/* Report a call into a function that is not implemented, then abort. */
void tk_unimplemented(const char *name) __attribute__((noreturn));

/* An exported symbol that aborts when called; its arguments are ignored. */
#define TK_STUB(name) TK_EXPORT void name(void) { tk_unimplemented(#name); }

#endif
