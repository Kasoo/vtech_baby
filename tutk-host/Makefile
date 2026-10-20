# Build the TUTK SDK replacement libraries for the host's own libc (glibc or musl).
#
#   make [global|iotc|av] [DEBUG=1] [CC=cc]
#
# With no target, builds all three into build/ARCH-LIBC (…-debug with DEBUG=1), ARCH and LIBC (glibc or
# musl) being the compiler's target. Each library finds the others beside it at run time.

CC    ?= cc
NM    ?= nm
DEBUG ?= 0
ARCH  := $(shell $(CC) -dumpmachine | cut -d- -f1)
LIBC  := $(if $(findstring musl,$(shell $(CC) -dumpmachine)),musl,glibc)
export ARCH LIBC DEBUG CC NM

LIBS := global iotc av

all: $(LIBS)

# Each library links against the ones below it.
iotc: global
av: iotc

$(LIBS):
	$(MAKE) -f lib.mk LIB=$@

clean:
	rm -rf build

.PHONY: all clean $(LIBS)
