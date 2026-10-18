# Build the replacement SDK libraries as Android (bionic) libraries with the NDK.
#
#   make [global|iotc|av] [ARCH=x86_64|aarch64] [DEBUG=1] [NDK=/path/to/ndk]
#
# With no target, builds all three. Switching DEBUG needs a `make clean` first.
#
# The results are patched and loaded exactly like the stock libraries (SDK patcher + companion), in
# any mix with the stock ones (tests/load_test.py).

ARCH ?= x86_64
DEBUG ?= 0
NDK  ?= $(HOME)/android-sdk/ndk/28.2.13676358
export ARCH DEBUG NDK

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
