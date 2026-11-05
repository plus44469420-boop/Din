# Build the PS5 payload:   make            (needs PS5_PAYLOAD_SDK)
# Build the desktop server: make host      (any POSIX cc, for testing)
# Copy ps5cable.elf to the console (USB stick + File Manager) and load it
# with the jailbreak's payload loader; see the README.

VERSION_TAG := $(shell git describe --abbrev=6 --dirty --always --tags 2>/dev/null || echo dev)

COMMON_CFLAGS := -O2 -Wall -Wextra -Werror -std=gnu11 -DVERSION_TAG=\"$(VERSION_TAG)\"
SOURCE        := server.c usblink.c fetch.c
HEADERS       := proto.h platform.h usblink.h fetch.h

.PHONY: all host clean

ifdef PS5_PAYLOAD_SDK
include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk

PS5_DEPS := $(abspath .deps/ps5/prefix)
CURL_A   := $(PS5_DEPS)/lib/libcurl.a
MBED_A   := $(PS5_DEPS)/lib/libmbedtls.a $(PS5_DEPS)/lib/libmbedx509.a $(PS5_DEPS)/lib/libmbedcrypto.a

all: ps5cable.elf

ps5cable.elf: $(SOURCE) platform-ps5.c usb-ps5.c $(HEADERS) $(CURL_A) $(PS5_DEPS)/include/ca_bundle.h
	$(CC) $(COMMON_CFLAGS) -DP5CX_EMBED_CA -I$(PS5_DEPS)/include -o $@ \
	  $(SOURCE) platform-ps5.c usb-ps5.c $(CURL_A) $(MBED_A) -lpthread

$(CURL_A) $(PS5_DEPS)/include/ca_bundle.h:
	PS5_PAYLOAD_SDK="$(PS5_PAYLOAD_SDK)" bash build-ps5-curl.sh
else
all:
	@echo "PS5_PAYLOAD_SDK is not set. Install https://github.com/ps5-payload-dev/sdk"
	@echo "and run: export PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk"
	@echo "To build the desktop test server instead, run: make host"
	@exit 1
endif

HOST_CC ?= cc
HOST_CURL_CFLAGS := $(shell pkg-config --cflags libcurl 2>/dev/null)
HOST_CURL_LIBS := $(shell pkg-config --libs libcurl 2>/dev/null)

host: ps5cable-host

ps5cable-host: $(SOURCE) platform-host.c usb-host.c $(HEADERS)
	@test -n "$(HOST_CURL_LIBS)" || { echo "libcurl development files are required (pkg-config libcurl)."; exit 1; }
	$(HOST_CC) $(COMMON_CFLAGS) $(HOST_CURL_CFLAGS) -o $@ $(SOURCE) platform-host.c usb-host.c $(HOST_CURL_LIBS) -lpthread

clean:
	rm -f ps5cable.elf ps5cable-host
