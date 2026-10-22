/* Desktop stand-in for the USB bridge cable, for tests: a TCP connection to a
 * "cable" (tests/fakecable.py) that carries each bulk transfer as a 4-byte
 * little-endian length followed by the bytes, so both ends see the same
 * transfer boundaries a real bridge chip gives them. */
#include <errno.h>
#include <netdb.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sys/socket.h>

#include "platform.h"
#include "usblink.h"

typedef struct {
  uint16_t port;
  char banner[128];
  char host[256];
  char service[16];
} args_t;

static args_t g_args;

static int
read_exact(int fd, uint8_t *p, size_t len) {
  while(len) {
    ssize_t n = read(fd, p, len);
    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0) {
      return -1;
    }
    p += n;
    len -= (size_t)n;
  }
  return 0;
}

static int
fake_recv(ulink_t *l, uint8_t *buf, int cap, int timeout_ms) {
  int fd = *(int *)l->ctx;
  struct pollfd p = {fd, POLLIN, 0};
  uint32_t len;
  int rc = poll(&p, 1, timeout_ms);

  if(rc == 0) {
    return 0;
  }
  if(rc < 0 || read_exact(fd, (uint8_t *)&len, 4)) {
    return -1;
  }
  if(len > (uint32_t)cap) {
    return -1;
  }
  return read_exact(fd, buf, len) ? -1 : (int)len;
}

static int
fake_send(ulink_t *l, const uint8_t *buf, int len, int timeout_ms) {
  int fd = *(int *)l->ctx;
  uint32_t n = (uint32_t)len;

  (void)timeout_ms;
  if(write(fd, &n, 4) != 4) {
    return -1;
  }
  while(len) {
    ssize_t w = write(fd, buf, (size_t)len);
    if(w < 0 && errno == EINTR) {
      continue;
    }
    if(w <= 0) {
      return -1;
    }
    buf += w;
    len -= (int)w;
  }
  return 0;
}

static int
dial(const args_t *a) {
  struct addrinfo hints = {0}, *res;
  int fd = -1;

  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  if(getaddrinfo(a->host, a->service, &hints, &res)) {
    return -1;
  }
  for(struct addrinfo *ai = res; ai && fd < 0; ai = ai->ai_next) {
    if((fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol)) < 0) {
      continue;
    }
    if(connect(fd, ai->ai_addr, ai->ai_addrlen)) {
      close(fd);
      fd = -1;
    }
  }
  freeaddrinfo(res);
  return fd;
}

static void *
usb_thread(void *arg) {
  args_t *a = arg;

  for(;;) {
    int fd = dial(a);
    ulink_t l = {fake_recv, fake_send, &fd, "fake cable"};

    if(fd < 0) {
      usleep(200000);
      continue;
    }
    ulink_session(&l, a->port, a->banner);
    close(fd);
  }
  return NULL;
}

void
usb_start(uint16_t port, const char *banner, const char *arg) {
  const char *colon;
  pthread_t thr;

  if(!arg || !(colon = strrchr(arg, ':'))) {
    return;
  }
  g_args.port = port;
  snprintf(g_args.banner, sizeof g_args.banner, "%s", banner);
  snprintf(g_args.host, sizeof g_args.host, "%.*s", (int)(colon - arg), arg);
  snprintf(g_args.service, sizeof g_args.service, "%s", colon + 1);
  if(!pthread_create(&thr, NULL, usb_thread, &g_args)) {
    pthread_detach(thr);
  }
}
