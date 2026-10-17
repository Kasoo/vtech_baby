#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "tk_stub.h"

/* The SDK patcher identifies each library's SDK version by a string of this shape. */
__attribute__((used)) static const char tk_sdk_version[] = "4.3.8.1-tutk-sdk-lan-only";

void tk_unimplemented(const char *name) {
    static const char prefix[] = "unimplemented: ";
    write(2, prefix, sizeof prefix - 1);
    write(2, name, strlen(name));
    write(2, "\n", 1);
    abort();
}
