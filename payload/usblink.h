/* ps5cable over a USB bridge cable.
 *
 * A PC and a PS5 are both USB hosts, so a plain USB cable (C-C, A-C or A-A)
 * between them does nothing. A bridge ("data link" / "transfer") cable, with
 * USB-C or USB-A ends in any combination, has a chip in the middle,
 * such as Prolific's PL27A1 (USB 3) or PL25A1 (USB 2), that shows up as a
 * device on both ends and passes bulk transfers from one host to the other.
 *
 * The link carries the normal ps5cable protocol (proto.h). The PC opens any
 * number of channels; each one becomes a TCP connection to the payload's own
 * server on 127.0.0.1, so the file server does not care which way a client
 * arrived. Every bulk transfer is exactly one frame:
 *
 *   magic[2]="PU" type:u8 pad:u8 chan:u16 reserved:u16 len:u32 data[len] pad
 *
 * `pad` (0 or 1 bytes) keeps the transfer from being a multiple of 512, so
 * it always ends with a short packet and the receiver gets it as one read.
 */
#pragma once

#include <stdint.h>

#define ULINK_VERSION    1
#define ULINK_HDR        12
#define ULINK_MAX_DATA   (256 * 1024)
#define ULINK_MAX_FRAME  (ULINK_HDR + ULINK_MAX_DATA + 1)
/* Bulk reads are a whole number of max-size packets so a full frame fits. */
#define ULINK_RX_BUF     (ULINK_MAX_DATA + 1024)

enum {
  UL_HELLO = 1, /* data: version:u32 max_data:u32 banner (PS5 -> PC reply) */
  UL_OPEN  = 2, /* PC -> PS5: connect channel `chan` to the file server */
  UL_DATA  = 3,
  UL_CLOSE = 4, /* either side: the channel is gone */
  UL_PING  = 5, /* PC -> PS5, echoed back; keeps the link alive */
};

/* Known bridge chips: vendor, product, name, nominal USB speed in Mbps. */
#define ULINK_BRIDGES                                  \
  X(0x067b, 0x27a1, "Prolific PL27A1", 5000)           \
  X(0x067b, 0x25a1, "Prolific PL25A1", 480)

/* Prolific "QuickLink" feature bits (Linux drivers/net/usb/plusb.c). */
#define PL_S_EN       (1 << 7)
#define PL_RESET_OUT  (1 << 4)
#define PL_RESET_IN   (1 << 3)
#define PL_PEER_E     (1 << 0)
#define PL_SET_FEATURE 3

typedef struct ulink {
  /* One bulk IN transfer into buf. Returns its length, 0 on timeout, or -1
   * when the cable is gone. */
  int (*recv)(struct ulink *l, uint8_t *buf, int cap, int timeout_ms);
  /* One bulk OUT transfer of exactly len bytes. 0 on success, -1 on error. */
  int (*send)(struct ulink *l, const uint8_t *buf, int len, int timeout_ms);
  void *ctx;
  const char *name;
} ulink_t;

/* Serves channels over an open link until it fails. `port` is the TCP port
 * of the file server on 127.0.0.1; `banner` is sent to the PC in HELLO.
 * Returns when the cable is unplugged. */
void ulink_session(ulink_t *l, uint16_t port, const char *banner);

/* Starts the background thread that waits for a bridge cable and runs
 * sessions on it. `arg` is backend specific (the host build's fake cable
 * address, or NULL for none); ignored on the PS5. */
void usb_start(uint16_t port, const char *banner, const char *arg);
