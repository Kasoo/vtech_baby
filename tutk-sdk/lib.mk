# Build one library; run through the Makefile, which sets ARCH, DEBUG and NDK.
#
#   make -f lib.mk LIB=global|iotc|av

API  := 21
BIN  := $(NDK)/toolchains/llvm/prebuilt/linux-x86_64/bin
CC   := $(BIN)/$(ARCH)-linux-android$(API)-clang
NM   := $(BIN)/llvm-nm

OUT  := build/$(ARCH)
DIR  := $(OUT)/$(LIB)

SONAME_global := libTUTKGlobalAPIs.so
SONAME_iotc   := libIOTCAPIs.so
SONAME_av     := libAVAPIs.so
# Our libraries this one links against. They are recorded by soname, so the stock ones serve as well
# at run time; linking against ours checks that we only use what our builds export.
NEEDS_global :=
NEEDS_iotc   := $(OUT)/libTUTKGlobalAPIs.so
NEEDS_av     := $(OUT)/libIOTCAPIs.so $(OUT)/libTUTKGlobalAPIs.so

ifeq ($(SONAME_$(LIB)),)
$(error LIB must be one of global, iotc, av)
endif

CFLAGS  := -O2 -g -fPIC -fvisibility=hidden -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0 \
           -Wall -Wextra -Wno-unused-parameter -Iinclude -Icommon -I$(LIB)
# DEBUG=1 builds in the TK_TRACE=1 debug trace.
ifeq ($(DEBUG),1)
CFLAGS += -DTK_DEBUG
endif
# libdl.so is the slot the patcher repoints to the companion, so it must be a dependency.
LDFLAGS := -shared -Wl,--no-undefined -Wl,-z,max-page-size=16384 -Wl,--no-as-needed -lm -ldl

SRCS := $(wildcard $(LIB)/*.c)
OBJS := $(SRCS:$(LIB)/%.c=$(DIR)/%.o) $(patsubst common/%.c,$(DIR)/common-%.o,$(wildcard common/*.c))
HDRS := $(wildcard $(LIB)/*.h common/*.h include/*.h)
SO   := $(OUT)/$(SONAME_$(LIB))

# Global alone forwards the tutk_third_ imports to the host's OpenSSL (libssl.so.3); IOTC and AV reach
# OpenSSL through those forwarders, as the stock libraries do.
ifeq ($(LIB),global)
GEN_SSL  := $(DIR)/ssl_fwd.S $(DIR)/ssl_link.c
GEN_ARGS := --ossl global/ossl.h
LINK_SSL := $(DIR)/ssl_fwd.o $(DIR)/libssl.so.3
endif

all: $(SO)

$(DIR)/%.o: $(LIB)/%.c $(HDRS) | $(DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(DIR)/common-%.o: common/%.c $(HDRS) | $(DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(DIR)/defined.txt: $(OBJS)
	$(NM) --defined-only --extern-only --format=just-symbols $(OBJS) | sort -u > $@

$(DIR)/stubs.c $(GEN_SSL) &: $(LIB)/imports.txt $(DIR)/defined.txt tools/gen_stubs.py $(filter-out --ossl,$(GEN_ARGS))
	python3 tools/gen_stubs.py $(LIB)/imports.txt $(DIR)/defined.txt $(DIR) $(GEN_ARGS)

$(DIR)/stubs.o: $(DIR)/stubs.c common/tk_common.h
	$(CC) $(CFLAGS) -c $< -o $@

$(DIR)/ssl_fwd.o: $(DIR)/ssl_fwd.S
	$(CC) -c $< -o $@

# Link-time stand-in for the host's libssl.so.3; never shipped.
$(DIR)/libssl.so.3: $(DIR)/ssl_link.c
	$(CC) -shared -fPIC -Wl,-soname,libssl.so.3 $< -o $@

$(SO): $(OBJS) $(DIR)/stubs.o $(LINK_SSL) $(NEEDS_$(LIB))
	$(CC) $(LDFLAGS) -Wl,-soname,$(SONAME_$(LIB)) -o $@ $(OBJS) $(DIR)/stubs.o $(LINK_SSL) $(NEEDS_$(LIB))

$(DIR):
	mkdir -p $@

.PHONY: all
