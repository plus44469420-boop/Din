/* Channel multiplexer that runs the ps5cable protocol over a USB bridge
 * cable. See usblink.h for the frame format. */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sys/socket.h>

#include "platform.h"
#include "usblink.h"

#define MAX_CHANNELS 128
#define SEND_TIMEOUT_MS 10000

typedef struct session session_t;

typedef struct {
  session_t *s;
  uint16_t id;
  int fd;
  int used;
  int peer_closed;
} chan_t;

struct session {
  ulink_t *l;
  pthread_mutex_t wlock; /* one bulk OUT transfer at a time */
  pthread_mutex_t clock; /* channel table */
  chan_t ch[MAX_CHANNELS];
  volatile int dead;
};

/* `buf` has ULINK_HDR bytes of room before the `len` data bytes and one
 * spare byte after them for the padding. */
static int
frame_send(session_t *s, uint8_t *buf, uint8_t type, uint16_t chan, uint32_t len) {
  int total = ULINK_HDR + (int)len;
  uint8_t pad = (total % 512) == 0;
  int rc = -1;

  buf[0] = 'P';
  buf[1] = 'U';
  buf[2] = type;
  buf[3] = pad;
  memcpy(buf + 4, &chan, 2);
  buf[6] = buf[7] = 0;
  memcpy(buf + 8, &len, 4);
  if(pad) {
    buf[total] = 0;
  }

  pthread_mutex_lock(&s->wlock);
  if(!s->dead) {
    rc = s->l->send(s->l, buf, total + pad, SEND_TIMEOUT_MS);
    if(rc) {
      s->dead = 1;
    }
  }
  pthread_mutex_unlock(&s->wlock);
  return rc;
}

static int
send_small(session_t *s, uint8_t type, uint16_t chan, const void *data, uint32_t len) {
  uint8_t buf[ULINK_HDR + 256 + 1];

  if(len > 256) {
    len = 256;
  }
  if(len) {
    memcpy(buf + ULINK_HDR, data, len);
  }
  return frame_send(s, buf, type, chan, len);
}

static chan_t *
find_chan(session_t *s, uint16_t id) {
  for(int i = 0; i < MAX_CHANNELS; i++) {
    if(s->ch[i].used && s->ch[i].id == id) {
      return &s->ch[i];
    }
  }
  return NULL;
}

static int
write_all(int fd, const uint8_t *p, size_t len) {
  while(len) {
    ssize_t n = write(fd, p, len);
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

/* Socket -> cable. The only thread that closes the channel's socket. */
static void *
pump(void *arg) {
  chan_t *c = arg;
  session_t *s = c->s;
  uint8_t *buf = malloc(ULINK_MAX_FRAME);

  while(buf) {
    ssize_t n = read(c->fd, buf + ULINK_HDR, ULINK_MAX_DATA);
    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0 || frame_send(s, buf, UL_DATA, c->id, (uint32_t)n)) {
      break;
    }
  }
  free(buf);

  pthread_mutex_lock(&s->clock);
  if(!c->peer_closed && !s->dead) {
    send_small(s, UL_CLOSE, c->id, NULL, 0);
  }
  close(c->fd);
  c->used = 0;
  pthread_mutex_unlock(&s->clock);
  return NULL;
}

static void
open_chan(session_t *s, uint16_t id, uint16_t port) {
  struct sockaddr_in addr = {0};
  int bufsize = 4 * 1024 * 1024, one = 1;
  chan_t *c = NULL;
  pthread_t thr;
  int fd;

  pthread_mutex_lock(&s->clock);
  if((c = find_chan(s, id))) {
    c->peer_closed = 1;
    shutdown(c->fd, SHUT_RDWR);
    c = NULL;
  }
  for(int i = 0; i < MAX_CHANNELS && !c; i++) {
    if(!s->ch[i].used) {
      c = &s->ch[i];
    }
  }
  pthread_mutex_unlock(&s->clock);
  if(!c) {
    send_small(s, UL_CLOSE, id, NULL, 0);
    return;
  }

  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if((fd = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
    send_small(s, UL_CLOSE, id, NULL, 0);
    return;
  }
  setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof bufsize);
  setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &bufsize, sizeof bufsize);
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  if(connect(fd, (struct sockaddr *)&addr, sizeof addr)) {
    plat_log("usb: channel %u: connect to the file server: %s", id, strerror(errno));
    close(fd);
    send_small(s, UL_CLOSE, id, NULL, 0);
    return;
  }

  pthread_mutex_lock(&s->clock);
  c->s = s;
  c->id = id;
  c->fd = fd;
  c->peer_closed = 0;
  c->used = 1;
  pthread_mutex_unlock(&s->clock);

  if(pthread_create(&thr, NULL, pump, c)) {
    pthread_mutex_lock(&s->clock);
    close(fd);
    c->used = 0;
    pthread_mutex_unlock(&s->clock);
    send_small(s, UL_CLOSE, id, NULL, 0);
    return;
  }
  pthread_detach(thr);
}

/* Shuts every channel down and waits for their pump threads to finish. */
static void
close_all(session_t *s) {
  int busy = 1;

  pthread_mutex_lock(&s->clock);
  for(int i = 0; i < MAX_CHANNELS; i++) {
    if(s->ch[i].used) {
      s->ch[i].peer_closed = 1;
      shutdown(s->ch[i].fd, SHUT_RDWR);
    }
  }
  pthread_mutex_unlock(&s->clock);

  for(int tries = 0; busy && tries < 500; tries++) {
    busy = 0;
    pthread_mutex_lock(&s->clock);
    for(int i = 0; i < MAX_CHANNELS; i++) {
      busy |= s->ch[i].used;
    }
    pthread_mutex_unlock(&s->clock);
    if(busy) {
      usleep(10000);
    }
  }
}

void
ulink_session(ulink_t *l, uint16_t port, const char *banner) {
  session_t *s = calloc(1, sizeof *s);
  uint8_t *buf = malloc(ULINK_MAX_FRAME);

  if(!s || !buf) {
    free(s);
    free(buf);
    return;
  }
  s->l = l;
  pthread_mutex_init(&s->wlock, NULL);
  pthread_mutex_init(&s->clock, NULL);

  while(!s->dead) {
    uint32_t len;
    uint16_t id;
    chan_t *c;
    int n = l->recv(l, buf, ULINK_MAX_FRAME, 1000);

    if(n < 0) {
      break;
    }
    if(n < ULINK_HDR || buf[0] != 'P' || buf[1] != 'U') {
      continue;
    }
    memcpy(&id, buf + 4, 2);
    memcpy(&len, buf + 8, 4);
    if(len > (uint32_t)(n - ULINK_HDR)) {
      continue;
    }

    switch(buf[2]) {
    case UL_HELLO: {
      uint8_t hello[256];
      uint32_t ver = ULINK_VERSION, max = ULINK_MAX_DATA;
      size_t blen = strlen(banner);

      /* A HELLO starts a fresh session: the PC has forgotten any channels. */
      close_all(s);
      if(blen > sizeof hello - 8) {
        blen = sizeof hello - 8;
      }
      memcpy(hello, &ver, 4);
      memcpy(hello + 4, &max, 4);
      memcpy(hello + 8, banner, blen);
      send_small(s, UL_HELLO, 0, hello, (uint32_t)(8 + blen));
      plat_notify("ps5cable: PC connected over USB (%s)", l->name);
      break;
    }
    case UL_PING:
      send_small(s, UL_PING, id, buf + ULINK_HDR, len);
      break;
    case UL_OPEN:
      open_chan(s, id, port);
      break;
    case UL_DATA:
      pthread_mutex_lock(&s->clock);
      if((c = find_chan(s, id)) && write_all(c->fd, buf + ULINK_HDR, len)) {
        shutdown(c->fd, SHUT_RDWR);
      }
      pthread_mutex_unlock(&s->clock);
      break;
    case UL_CLOSE:
      pthread_mutex_lock(&s->clock);
      if((c = find_chan(s, id))) {
        c->peer_closed = 1;
        shutdown(c->fd, SHUT_RDWR);
      }
      pthread_mutex_unlock(&s->clock);
      break;
    default:
      break;
    }
  }

  s->dead = 1;
  close_all(s);
  free(buf);
  pthread_mutex_destroy(&s->wlock);
  pthread_mutex_destroy(&s->clock);
  free(s);
}
