# Build one library; run through the Makefile, which sets ARCH, LIBC, DEBUG, CC and NM.
#
#   make -f lib.mk LIB=global|iotc|av

OUT  := build/$(ARCH)-$(LIBC)$(if $(filter 1,$(DEBUG)),-debug)
DIR  := $(OUT)/$(LIB)

SONAME_global := libTUTKGlobalAPIs.so
SONAME_iotc   := libIOTCAPIs.so
SONAME_av     := libAVAPIs.so
# Our libraries this one links against, found at run time beside it ($ORIGIN).
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
LDFLAGS := -shared -Wl,--no-undefined -Wl,-z,max-page-size=16384 '-Wl,-rpath,$$ORIGIN' -lpthread

SRCS := $(wildcard $(LIB)/*.c)
OBJS := $(SRCS:$(LIB)/%.c=$(DIR)/%.o) $(patsubst common/%.c,$(DIR)/common-%.o,$(wildcard common/*.c))
HDRS := $(wildcard $(LIB)/*.h common/*.h include/*.h)
SO   := $(OUT)/$(SONAME_$(LIB))

# Global alone forwards the tutk_third_ calls to OpenSSL (libssl.so.3); IOTC and AV reach OpenSSL
# through those forwarders.
ifeq ($(LIB),global)
GEN_SSL  := $(DIR)/ssl_fwd.S $(DIR)/ssl_link.c
GEN_ARGS := --ossl global/ossl.h
LINK_SSL := $(DIR)/ssl_fwd.o -lssl -lcrypto
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

$(SO): $(OBJS) $(DIR)/stubs.o $(filter %.o,$(LINK_SSL)) $(NEEDS_$(LIB))
	$(CC) -Wl,-soname,$(SONAME_$(LIB)) -o $@ $(OBJS) $(DIR)/stubs.o $(LINK_SSL) $(NEEDS_$(LIB)) $(LDFLAGS)

$(DIR):
	mkdir -p $@

.PHONY: all
