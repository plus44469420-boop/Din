# Build the PS5 payload:   make            (needs PS5_PAYLOAD_SDK)
# Build the desktop server: make host      (any POSIX cc, for testing)
# Send to the console:      make deploy PS5_HOST=<PS5 IP>  (needs the PS5 on a network)

PS5_HOST ?= 192.168.88.2
PS5_PORT ?= 9021

VERSION_TAG := $(shell git describe --abbrev=6 --dirty --always --tags 2>/dev/null || echo dev)

COMMON_CFLAGS := -O2 -Wall -Wextra -Werror -std=gnu11 -DVERSION_TAG=\"$(VERSION_TAG)\"
SOURCE        := server.c usblink.c
HEADERS       := proto.h platform.h usblink.h

.PHONY: all host deploy clean

ifdef PS5_PAYLOAD_SDK
include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk

all: ps5cable.elf

ps5cable.elf: $(SOURCE) platform-ps5.c usb-ps5.c $(HEADERS)
	$(CC) $(COMMON_CFLAGS) -o $@ $(SOURCE) platform-ps5.c usb-ps5.c -lpthread

deploy: ps5cable.elf
	$(PS5_DEPLOY) -h $(PS5_HOST) -p $(PS5_PORT) $<
else
all:
	@echo "PS5_PAYLOAD_SDK is not set. Install https://github.com/ps5-payload-dev/sdk"
	@echo "and run: export PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk"
	@echo "To build the desktop test server instead, run: make host"
	@exit 1
endif

HOST_CC ?= cc

host: ps5cable-host

ps5cable-host: $(SOURCE) platform-host.c usb-host.c $(HEADERS)
	$(HOST_CC) $(COMMON_CFLAGS) -o $@ $(SOURCE) platform-host.c usb-host.c -lpthread

clean:
	rm -f ps5cable.elf ps5cable-host
