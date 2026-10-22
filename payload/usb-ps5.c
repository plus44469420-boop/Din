/* USB bridge cable on the PS5, through the system's libSceUsbd. Its calls
 * follow libusb 1.0 (as on the PS4), so the library is loaded at run time
 * and anything missing just disables the USB link instead of the payload. */
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/types.h>

#include "platform.h"
#include "usblink.h"

typedef struct {
  uint8_t bLength;
  uint8_t bDescriptorType;
  uint16_t bcdUSB;
  uint8_t bDeviceClass;
  uint8_t bDeviceSubClass;
  uint8_t bDeviceProtocol;
  uint8_t bMaxPacketSize0;
  uint16_t idVendor;
  uint16_t idProduct;
  uint16_t bcdDevice;
  uint8_t iManufacturer;
  uint8_t iProduct;
  uint8_t iSerialNumber;
  uint8_t bNumConfigurations;
} usbd_device_desc_t;

static struct {
  int (*Init)(void);
  ssize_t (*GetDeviceList)(void ***list);
  void (*FreeDeviceList)(void **list, int unref);
  int (*GetDeviceDescriptor)(void *dev, usbd_device_desc_t *desc);
  int (*Open)(void *dev, void **handle);
  void (*Close)(void *handle);
  int (*SetConfiguration)(void *handle, int config);
  int (*ClaimInterface)(void *handle, int iface);
  int (*ReleaseInterface)(void *handle, int iface);
  int (*KernelDriverActive)(void *handle, int iface);
  int (*DetachKernelDriver)(void *handle, int iface);
  int (*BulkTransfer)(void *handle, uint8_t ep, uint8_t *data, int len, int *done,
                      unsigned timeout);
  int (*ControlTransfer)(void *handle, uint8_t type, uint8_t req, uint16_t value,
                         uint16_t index, uint8_t *data, uint16_t len, unsigned timeout);
} U;

static const struct {
  uint16_t vid, pid;
  const char *name;
  int mbps;
} BRIDGES[] = {
#define X(v, p, n, s) {v, p, n, s},
  ULINK_BRIDGES
#undef X
};

typedef struct {
  void *h;
  uint8_t ep_in;
  uint8_t ep_out;
  int iface;
} bridge_t;

/* libusb's LIBUSB_ERROR_TIMEOUT, and the same code in Sony's 0x8024xxxx form. */
static int
is_timeout(int rc) {
  return rc == -7 || rc == (int)0x80240007;
}

static int
load(void) {
  void *lib = dlopen("libSceUsbd.sprx", RTLD_LAZY);

  if(!lib) {
    plat_log("usb: libSceUsbd.sprx: %s", dlerror());
    return -1;
  }
#define SYM(field, name, required)                         \
  do {                                                     \
    *(void **)&U.field = dlsym(lib, name);                 \
    if(!U.field && (required)) {                           \
      plat_log("usb: libSceUsbd has no %s", name);         \
      return -1;                                           \
    }                                                      \
  } while(0)
  SYM(Init, "sceUsbdInit", 1);
  SYM(GetDeviceList, "sceUsbdGetDeviceList", 1);
  SYM(FreeDeviceList, "sceUsbdFreeDeviceList", 1);
  SYM(GetDeviceDescriptor, "sceUsbdGetDeviceDescriptor", 1);
  SYM(Open, "sceUsbdOpen", 1);
  SYM(Close, "sceUsbdClose", 1);
  SYM(SetConfiguration, "sceUsbdSetConfiguration", 0);
  SYM(ClaimInterface, "sceUsbdClaimInterface", 1);
  SYM(ReleaseInterface, "sceUsbdReleaseInterface", 0);
  SYM(KernelDriverActive, "sceUsbdKernelDriverActive", 0);
  SYM(DetachKernelDriver, "sceUsbdDetachKernelDriver", 0);
  SYM(BulkTransfer, "sceUsbdBulkTransfer", 1);
  SYM(ControlTransfer, "sceUsbdControlTransfer", 1);
#undef SYM
  return 0;
}

/* The bridge interface's bulk endpoints. These cables are composite: interface
 * 0 is the bridge and another interface is a mass-storage "easy transfer"
 * drive, which also has bulk endpoints and is not the link. */
static int
find_endpoints(bridge_t *d) {
  uint8_t cfg[1024];
  int len, cur = -1, alt = 0, class = 0, best = -1;
  uint8_t in = 0, out = 0, best_in = 0, best_out = 0;

  len = U.ControlTransfer(d->h, 0x80, 6, 0x0200, 0, cfg, sizeof cfg, 1000);
  if(len < 9) {
    plat_log("usb: GET_DESCRIPTOR(config) failed: %d", len);
    return -1;
  }
  for(int i = 0; i + 2 <= len && cfg[i] >= 2 && i + cfg[i] <= len; i += cfg[i]) {
    if(cfg[i + 1] == 4 && cfg[i] >= 6) {
      if(best < 0 && cur >= 0 && alt == 0 && class != 0x08 && in && out) {
        best = cur;
        best_in = in;
        best_out = out;
      }
      cur = cfg[i + 2];
      alt = cfg[i + 3];
      class = cfg[i + 5];
      in = out = 0;
    } else if(cfg[i + 1] == 5 && cur >= 0 && alt == 0 && cfg[i] >= 4 && (cfg[i + 3] & 3) == 2) {
      if(cfg[i + 2] & 0x80) {
        in = cfg[i + 2];
      } else {
        out = cfg[i + 2];
      }
    }
  }
  if(best < 0 && cur >= 0 && alt == 0 && class != 0x08 && in && out) {
    best = cur;
    best_in = in;
    best_out = out;
  }
  if(best < 0) {
    return -1;
  }
  d->iface = best;
  d->ep_in = best_in;
  d->ep_out = best_out;
  return 0;
}

static int g_found_unusable;

static int
open_bridge(bridge_t *d, const char **name) {
  void **list = NULL;
  ssize_t n = U.GetDeviceList(&list);
  int rc = -1;

  g_found_unusable = 0;

  for(ssize_t i = 0; i < n && rc; i++) {
    usbd_device_desc_t desc;

    if(U.GetDeviceDescriptor(list[i], &desc)) {
      continue;
    }
    for(size_t b = 0; b < sizeof BRIDGES / sizeof BRIDGES[0]; b++) {
      if(desc.idVendor != BRIDGES[b].vid || desc.idProduct != BRIDGES[b].pid) {
        continue;
      }
      if(U.Open(list[i], &d->h)) {
        plat_log("usb: found %s but could not open it", BRIDGES[b].name);
        g_found_unusable = 1;
        break;
      }
      if(find_endpoints(d)) {
        plat_log("usb: %s: no bridge endpoints", BRIDGES[b].name);
        g_found_unusable = 1;
        U.Close(d->h);
        break;
      }
      if(U.KernelDriverActive && U.DetachKernelDriver &&
         U.KernelDriverActive(d->h, d->iface) == 1) {
        U.DetachKernelDriver(d->h, d->iface);
      }
      if(U.ClaimInterface(d->h, d->iface) &&
         !(U.SetConfiguration && !U.SetConfiguration(d->h, 1) &&
           !U.ClaimInterface(d->h, d->iface))) {
        plat_log("usb: %s: could not claim interface %d", BRIDGES[b].name, d->iface);
        g_found_unusable = 1;
        U.Close(d->h);
        break;
      }
      /* Same reset Linux's plusb driver does: flush both directions and
       * tell the chip this side is present. */
      U.ControlTransfer(d->h, 0xc0, PL_SET_FEATURE,
                        PL_S_EN | PL_RESET_OUT | PL_RESET_IN | PL_PEER_E, 0, NULL, 0, 1000);
      *name = BRIDGES[b].name;
      rc = 0;
      break;
    }
  }
  if(list) {
    U.FreeDeviceList(list, 1);
  }
  return rc;
}

static int
ps5_recv(ulink_t *l, uint8_t *buf, int cap, int timeout_ms) {
  bridge_t *d = l->ctx;
  int done = 0;
  int rc = U.BulkTransfer(d->h, d->ep_in, buf, cap, &done, (unsigned)timeout_ms);

  if(!rc || (is_timeout(rc) && done)) {
    return done;
  }
  return is_timeout(rc) ? 0 : -1;
}

static int
ps5_send(ulink_t *l, const uint8_t *buf, int len, int timeout_ms) {
  bridge_t *d = l->ctx;
  int done = 0;
  int rc = U.BulkTransfer(d->h, d->ep_out, (uint8_t *)buf, len, &done, (unsigned)timeout_ms);

  return !rc && done == len ? 0 : -1;
}

typedef struct {
  uint16_t port;
  char banner[128];
} args_t;

static args_t g_args;

static void *
usb_thread(void *arg) {
  args_t *a = arg;
  int rc;

  if(load()) {
    plat_log("usb: no USB access on this firmware; the USB link is off");
    return NULL;
  }
  if((rc = U.Init()) < 0) {
    plat_log("usb: sceUsbdInit failed: %#x", rc);
    return NULL;
  }
  plat_log("usb: waiting for a USB bridge cable");

  for(;;) {
    bridge_t d = {0};
    ulink_t l = {ps5_recv, ps5_send, &d, NULL};

    if(open_bridge(&d, &l.name)) {
      if(g_found_unusable) {
        static time_t last;
        time_t now = time(NULL);

        if(now - last >= 8) {
          last = now;
          plat_notify("PS5 Cable: USB cable found, but it would not open");
        }
      }
      sleep(1);
      continue;
    }
    plat_notify("PS5 Cable: USB cable plugged in (%s)", l.name);
    plat_log("usb: %s ready (interface %d, in %#x, out %#x)", l.name, d.iface, d.ep_in, d.ep_out);
    ulink_session(&l, a->port, a->banner);
    plat_notify("PS5 Cable: USB cable unplugged");
    if(U.ReleaseInterface) {
      U.ReleaseInterface(d.h, d.iface);
    }
    U.Close(d.h);
    sleep(1);
  }
  return NULL;
}

void
usb_start(uint16_t port, const char *banner, const char *arg) {
  pthread_t thr;

  (void)arg;
  g_args.port = port;
  snprintf(g_args.banner, sizeof g_args.banner, "%s", banner);
  if(!pthread_create(&thr, NULL, usb_thread, &g_args)) {
    pthread_detach(thr);
  }
}
