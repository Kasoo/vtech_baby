# Build the replacement libTUTKGlobalAPIs.so as an Android (bionic) library with the NDK.
#
#   make [ARCH=x86_64|aarch64] [DEBUG=1] [NDK=/path/to/ndk]
#
# Switching DEBUG needs a `make clean` first.
#
# The result is patched and loaded exactly like the stock library (SDK patcher + companion).

ARCH ?= x86_64
DEBUG ?= 0
NDK  ?= $(HOME)/android-sdk/ndk/28.2.13676358
API  := 21
BIN  := $(NDK)/toolchains/llvm/prebuilt/linux-x86_64/bin
CC   := $(BIN)/$(ARCH)-linux-android$(API)-clang
NM   := $(BIN)/llvm-nm

OUT  := build/$(ARCH)
GEN  := $(OUT)/gen
OBJ  := $(OUT)/obj

CFLAGS  := -O2 -g -fPIC -fvisibility=hidden -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0 \
           -Wall -Wextra -Wno-unused-parameter -Iinclude -Iglobal
# DEBUG=1 builds in the TK_TRACE=1 debug trace.
ifeq ($(DEBUG),1)
CFLAGS += -DTK_DEBUG
endif
# libdl.so is the slot the patcher repoints to the companion, so it must be a dependency.
LDFLAGS := -shared -Wl,--no-undefined -Wl,-z,max-page-size=16384 -Wl,--no-as-needed -lm -ldl

SRCS := $(wildcard global/*.c)
OBJS := $(SRCS:global/%.c=$(OBJ)/%.o)

LIB := $(OUT)/libTUTKGlobalAPIs.so

all: $(LIB)

$(OBJ)/%.o: global/%.c $(wildcard global/*.h) include/tutk_shared.h | $(OBJ)
	$(CC) $(CFLAGS) -c $< -o $@

$(GEN)/defined.txt: $(OBJS) | $(GEN)
	$(NM) --defined-only --extern-only --format=just-symbols $(OBJS) | sort -u > $@

$(GEN)/stubs.c $(GEN)/ssl_fwd.S $(GEN)/ssl_link.c &: global/imports.txt $(GEN)/defined.txt global/ossl.h tools/gen_stubs.py
	python3 tools/gen_stubs.py global/imports.txt $(GEN)/defined.txt global/ossl.h $(GEN)

$(GEN)/stubs.o: $(GEN)/stubs.c global/tk_stub.h
	$(CC) $(CFLAGS) -c $< -o $@

$(GEN)/ssl_fwd.o: $(GEN)/ssl_fwd.S
	$(CC) -c $< -o $@

# Link-time stand-in for the host's libssl.so.3; never shipped.
$(GEN)/libssl.so.3: $(GEN)/ssl_link.c
	$(CC) -shared -fPIC -Wl,-soname,libssl.so.3 $< -o $@

$(LIB): $(OBJS) $(GEN)/stubs.o $(GEN)/ssl_fwd.o $(GEN)/libssl.so.3
	$(CC) $(LDFLAGS) -Wl,-soname,libTUTKGlobalAPIs.so -o $@ \
	    $(OBJS) $(GEN)/stubs.o $(GEN)/ssl_fwd.o $(GEN)/libssl.so.3

$(OBJ) $(GEN):
	mkdir -p $@

clean:
	rm -rf build

.PHONY: all clean
