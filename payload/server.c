#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>

#include "platform.h"
#include "proto.h"

#ifndef VERSION_TAG
#define VERSION_TAG "dev"
#endif

#define IO_BUF_SIZE   (4 * 1024 * 1024)
#define SOCK_BUF_SIZE (4 * 1024 * 1024)

static int
read_full(int fd, void *buf, size_t len) {
  uint8_t *p = buf;

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
write_full(int fd, const void *buf, size_t len) {
  const uint8_t *p = buf;

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

static int
send_res(int fd, uint8_t kind, uint32_t mode, uint64_t a, uint64_t b) {
  p5cx_res_t res = {
    .status = ST_OK, .kind = kind, .reserved = 0, .mode = mode, .a = a, .b = b,
  };
  return write_full(fd, &res, sizeof res);
}

static int
send_ok(int fd) {
  return send_res(fd, 0, 0, 0, 0);
}

static int
send_err(int fd, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

static int
send_err(int fd, const char *fmt, ...) {
  char msg[P5CX_MAX_PATH + 256];
  p5cx_res_t res = {0};
  va_list args;
  int len;

  va_start(args, fmt);
  len = vsnprintf(msg, sizeof msg, fmt, args);
  va_end(args);
  if(len < 0) {
    len = 0;
  } else if((size_t)len >= sizeof msg) {
    len = sizeof msg - 1;
  }

  res.status = ST_ERR;
  res.a = (uint64_t)len;
  if(write_full(fd, &res, sizeof res)) {
    return -1;
  }
  return write_full(fd, msg, (size_t)len);
}

#define SEND_ERRNO(fd, what, path) \
  send_err(fd, "%s: %s: %s", what, path, strerror(errno))

static uint8_t
mode_kind(mode_t mode) {
  if(S_ISREG(mode)) {
    return KIND_FILE;
  }
  if(S_ISDIR(mode)) {
    return KIND_DIR;
  }
  return KIND_OTHER;
}

static int
mkdir_p(const char *path) {
  char tmp[P5CX_MAX_PATH];
  struct stat st;
  size_t len = strlen(path);

  if(len == 0 || len >= sizeof tmp) {
    errno = ENAMETOOLONG;
    return -1;
  }
  memcpy(tmp, path, len + 1);

  for(char *p = tmp + 1; *p; p++) {
    if(*p != '/') {
      continue;
    }
    *p = '\0';
    if(mkdir(tmp, 0777) && errno != EEXIST) {
      return -1;
    }
    *p = '/';
  }
  if(mkdir(tmp, 0777) && errno != EEXIST) {
    return -1;
  }
  if(stat(tmp, &st)) {
    return -1;
  }
  if(!S_ISDIR(st.st_mode)) {
    errno = ENOTDIR;
    return -1;
  }
  return 0;
}

static int
mkdir_parent(const char *path) {
  char tmp[P5CX_MAX_PATH];
  char *slash;

  snprintf(tmp, sizeof tmp, "%s", path);
  if(!(slash = strrchr(tmp, '/')) || slash == tmp) {
    return 0;
  }
  *slash = '\0';
  return mkdir_p(tmp);
}

/* Heap-allocates child paths so deep trees do not exhaust small PS5 thread
 * stacks. */
static int
rm_rf(const char *path) {
  struct dirent *ent;
  struct stat st;
  DIR *dir;
  int rc = 0;

  if(lstat(path, &st)) {
    return -1;
  }
  if(!S_ISDIR(st.st_mode)) {
    return unlink(path);
  }
  if(!(dir = opendir(path))) {
    return -1;
  }
  while((ent = readdir(dir))) {
    char *child;

    if(!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) {
      continue;
    }
    if(asprintf(&child, "%s/%s", path, ent->d_name) < 0) {
      rc = -1;
      break;
    }
    rc = rm_rf(child);
    free(child);
    if(rc) {
      break;
    }
  }
  closedir(dir);
  if(rc) {
    return rc;
  }
  return rmdir(path);
}

static int
handle_ping(int fd) {
  static const char version[] = "ps5cable " VERSION_TAG
#ifdef __PROSPERO__
    " (ps5)";
#else
    " (host)";
#endif
  size_t len = sizeof version - 1;

  if(send_res(fd, 0, 0, P5CX_PROTO_VER, len)) {
    return -1;
  }
  return write_full(fd, version, len);
}

static int
handle_list(int fd, const char *path) {
  size_t cap = 64 * 1024, len = 0;
  struct dirent *ent;
  uint8_t *blob;
  DIR *dir;
  int rc;

  if(!(dir = opendir(path))) {
    return SEND_ERRNO(fd, "opendir", path);
  }
  if(!(blob = malloc(cap))) {
    closedir(dir);
    return send_err(fd, "out of memory");
  }

  while((ent = readdir(dir))) {
    char full[P5CX_MAX_PATH];
    size_t namelen = strlen(ent->d_name);
    struct stat st;
    uint8_t kind = KIND_OTHER;
    uint64_t size = 0;
    int64_t mtime = 0;
    uint16_t nl;

    if(!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) {
      continue;
    }
    snprintf(full, sizeof full, "%s/%s", path, ent->d_name);
    if(!stat(full, &st) || !lstat(full, &st)) {
      kind = mode_kind(st.st_mode);
      size = (uint64_t)st.st_size;
      mtime = (int64_t)st.st_mtime;
    }

    if(namelen > UINT16_MAX) {
      continue;
    }
    while(len + 19 + namelen > cap) {
      uint8_t *bigger = realloc(blob, cap * 2);
      if(!bigger) {
        free(blob);
        closedir(dir);
        return send_err(fd, "out of memory");
      }
      blob = bigger;
      cap *= 2;
    }

    nl = (uint16_t)namelen;
    blob[len] = kind;
    memcpy(blob + len + 1, &size, 8);
    memcpy(blob + len + 9, &mtime, 8);
    memcpy(blob + len + 17, &nl, 2);
    memcpy(blob + len + 19, ent->d_name, namelen);
    len += 19 + namelen;
  }
  closedir(dir);

  rc = send_res(fd, 0, 0, len, 0);
  if(!rc) {
    rc = write_full(fd, blob, len);
  }
  free(blob);
  return rc;
}

static int
handle_stat(int fd, const char *path) {
  struct stat st;

  if(stat(path, &st) && lstat(path, &st)) {
    return SEND_ERRNO(fd, "stat", path);
  }
  return send_res(fd, mode_kind(st.st_mode), (uint32_t)st.st_mode,
                  (uint64_t)st.st_size, (uint64_t)(int64_t)st.st_mtime);
}

static int
handle_mkdir(int fd, const char *path) {
  if(mkdir_p(path)) {
    return SEND_ERRNO(fd, "mkdir", path);
  }
  return send_ok(fd);
}

static int
handle_delete(int fd, const char *path, uint8_t flags) {
  struct stat st;
  int rc;

  if(lstat(path, &st)) {
    return SEND_ERRNO(fd, "stat", path);
  }
  if(S_ISDIR(st.st_mode)) {
    rc = (flags & F_RECURSIVE) ? rm_rf(path) : rmdir(path);
  } else {
    rc = unlink(path);
  }
  if(rc) {
    return SEND_ERRNO(fd, "delete", path);
  }
  return send_ok(fd);
}

static int
handle_rename(int fd, const char *path, size_t pathlen) {
  size_t srclen = strlen(path);
  const char *dst;

  if(srclen + 1 >= pathlen) {
    return send_err(fd, "rename: expected \"src\\0dst\"");
  }
  dst = path + srclen + 1;
  if(rename(path, dst)) {
    return send_err(fd, "rename: %s -> %s: %s", path, dst, strerror(errno));
  }
  return send_ok(fd);
}

static int
handle_put(int fd, const char *path, uint8_t flags, uint64_t total,
           uint64_t offset, uint8_t *buf) {
  uint64_t remaining, written = 0;
  struct stat st;
  int file;

  if(offset > total) {
    return send_err(fd, "put: %s: resume offset past end of file", path);
  }
  if((flags & F_MKDIRS) && mkdir_parent(path)) {
    return SEND_ERRNO(fd, "mkdir", path);
  }

  if(offset) {
    if((file = open(path, O_WRONLY)) < 0) {
      return SEND_ERRNO(fd, "open", path);
    }
    if(fstat(file, &st) || (uint64_t)st.st_size < offset) {
      close(file);
      return send_err(fd, "put: %s: remote file is shorter than resume offset",
                      path);
    }
    if(ftruncate(file, (off_t)offset) ||
       lseek(file, (off_t)offset, SEEK_SET) < 0) {
      int err = errno;
      close(file);
      errno = err;
      return SEND_ERRNO(fd, "seek", path);
    }
  } else if((file = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0777)) < 0) {
    return SEND_ERRNO(fd, "open", path);
  }

  if(send_ok(fd)) {
    close(file);
    return -1;
  }

  remaining = total - offset;
  while(remaining) {
    size_t want = remaining < IO_BUF_SIZE ? (size_t)remaining : IO_BUF_SIZE;
    ssize_t n = read(fd, buf, want);

    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0) {
      plat_log("put %s: client went away after %llu bytes", path,
               (unsigned long long)(offset + written));
      close(file);
      return -1;
    }
    if(write_full(file, buf, (size_t)n)) {
      /* The client is still streaming, so the connection cannot be reused:
       * report and let the caller drop it. */
      SEND_ERRNO(fd, "write", path);
      close(file);
      return -1;
    }
    remaining -= (uint64_t)n;
    written += (uint64_t)n;
  }

  if(close(file)) {
    return SEND_ERRNO(fd, "close", path);
  }
  return send_res(fd, 0, 0, written, offset + written);
}

static int
handle_get(int fd, const char *path, uint64_t offset, uint8_t *buf) {
  uint64_t remaining;
  struct stat st;
  int file;

  if((file = open(path, O_RDONLY)) < 0) {
    return SEND_ERRNO(fd, "open", path);
  }
  if(fstat(file, &st)) {
    int err = errno;
    close(file);
    errno = err;
    return SEND_ERRNO(fd, "stat", path);
  }
  if(!S_ISREG(st.st_mode)) {
    close(file);
    return send_err(fd, "get: %s: not a regular file", path);
  }
  if(offset > (uint64_t)st.st_size) {
    close(file);
    return send_err(fd, "get: %s: offset past end of file", path);
  }
  if(lseek(file, (off_t)offset, SEEK_SET) < 0) {
    int err = errno;
    close(file);
    errno = err;
    return SEND_ERRNO(fd, "seek", path);
  }

  remaining = (uint64_t)st.st_size - offset;
  if(send_res(fd, KIND_FILE, (uint32_t)st.st_mode, remaining,
              (uint64_t)st.st_size)) {
    close(file);
    return -1;
  }

  while(remaining) {
    size_t want = remaining < IO_BUF_SIZE ? (size_t)remaining : IO_BUF_SIZE;
    ssize_t n = read(file, buf, want);

    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0) {
      /* Header already promised `remaining` bytes; closing is the only way
       * to tell the client the stream is short. */
      plat_log("get %s: read failed: %s", path, strerror(errno));
      close(file);
      return -1;
    }
    if(write_full(fd, buf, (size_t)n)) {
      close(file);
      return -1;
    }
    remaining -= (uint64_t)n;
  }

  close(file);
  return 0;
}

static int
handle_df(int fd, const char *path) {
  struct statvfs sv;

  if(statvfs(path, &sv)) {
    return SEND_ERRNO(fd, "statvfs", path);
  }
  return send_res(fd, 0, 0, (uint64_t)sv.f_bavail * sv.f_frsize,
                  (uint64_t)sv.f_blocks * sv.f_frsize);
}

static void *
client_thread(void *arg) {
  int fd = (int)(intptr_t)arg;
  char path[P5CX_MAX_PATH];
  uint8_t *buf;

  if(!(buf = malloc(IO_BUF_SIZE))) {
    plat_log("client: out of memory");
    close(fd);
    return NULL;
  }

  for(;;) {
    p5cx_req_t req;
    int rc;

    if(read_full(fd, &req, sizeof req)) {
      break;
    }
    if(memcmp(req.magic, P5CX_MAGIC, 4)) {
      plat_log("client: bad magic, dropping connection");
      break;
    }
    if(req.pathlen >= sizeof path) {
      send_err(fd, "path too long");
      break;
    }
    if(read_full(fd, path, req.pathlen)) {
      break;
    }
    path[req.pathlen] = '\0';

    if(req.cmd != CMD_PING && req.cmd != CMD_SHUTDOWN && path[0] != '/') {
      if(send_err(fd, "path must be absolute: \"%s\"", path)) {
        break;
      }
      continue;
    }

    switch(req.cmd) {
    case CMD_PING:     rc = handle_ping(fd); break;
    case CMD_LIST:     rc = handle_list(fd, path); break;
    case CMD_STAT:     rc = handle_stat(fd, path); break;
    case CMD_MKDIR:    rc = handle_mkdir(fd, path); break;
    case CMD_DELETE:   rc = handle_delete(fd, path, req.flags); break;
    case CMD_RENAME:   rc = handle_rename(fd, path, req.pathlen); break;
    case CMD_PUT:      rc = handle_put(fd, path, req.flags, req.a, req.b, buf); break;
    case CMD_GET:      rc = handle_get(fd, path, req.a, buf); break;
    case CMD_DF:       rc = handle_df(fd, path); break;
    case CMD_SHUTDOWN:
      send_ok(fd);
      plat_notify("ps5cable stopped");
      exit(EXIT_SUCCESS);
    default:
      rc = send_err(fd, "unknown command %u", req.cmd);
      break;
    }
    if(rc) {
      break;
    }
  }

  free(buf);
  close(fd);
  return NULL;
}

/* Answers "P5CX?" broadcasts so the PC client can find the console on a
 * direct cable without the user typing an IP. */
static void *
discovery_thread(void *arg) {
  uint16_t port = (uint16_t)(intptr_t)arg;
  struct sockaddr_in addr = {0};
  int fd;

  if((fd = socket(AF_INET, SOCK_DGRAM, 0)) < 0) {
    plat_log("discovery: socket: %s", strerror(errno));
    return NULL;
  }
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &(int){1}, sizeof(int));
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(port);
  if(bind(fd, (struct sockaddr *)&addr, sizeof addr)) {
    plat_log("discovery: bind: %s", strerror(errno));
    close(fd);
    return NULL;
  }

  for(;;) {
    struct sockaddr_in peer;
    socklen_t peerlen = sizeof peer;
    char msg[64];
    ssize_t n = recvfrom(fd, msg, sizeof msg, 0, (struct sockaddr *)&peer,
                         &peerlen);

    if(n == 5 && !memcmp(msg, "P5CX?", 5)) {
      int len = snprintf(msg, sizeof msg, "P5CX! %u %s", port, VERSION_TAG);
      sendto(fd, msg, (size_t)len, 0, (struct sockaddr *)&peer, peerlen);
    }
  }
  return NULL;
}

/* Returns the number of non-loopback IPv4 addresses and optionally announces
 * them on screen. */
static int
announce_addresses(uint16_t port, int notify) {
  struct ifaddrs *ifaddr;
  char ip[INET_ADDRSTRLEN];
  int count = 0;

  if(getifaddrs(&ifaddr)) {
    return 0;
  }
  for(struct ifaddrs *ifa = ifaddr; ifa; ifa = ifa->ifa_next) {
    if(!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET ||
       !strncmp(ifa->ifa_name, "lo", 2)) {
      continue;
    }
    inet_ntop(AF_INET, &((struct sockaddr_in *)ifa->ifa_addr)->sin_addr, ip,
              sizeof ip);
    if(!strncmp(ip, "0.", 2)) {
      continue;
    }
    if(notify) {
      plat_notify("ps5cable ready on %s:%u (%s)", ip, port, ifa->ifa_name);
    }
    count++;
  }
  freeifaddrs(ifaddr);
  return count;
}

static int
serve(uint16_t port) {
  struct sockaddr_in addr = {0};
  int srv;

  if((srv = socket(AF_INET, SOCK_STREAM, 0)) < 0) {
    plat_log("socket: %s", strerror(errno));
    return -1;
  }
  setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &(int){1}, sizeof(int));
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(port);

  if(bind(srv, (struct sockaddr *)&addr, sizeof addr)) {
    plat_log("bind: %s", strerror(errno));
    close(srv);
    return -1;
  }
  if(listen(srv, SOMAXCONN)) {
    plat_log("listen: %s", strerror(errno));
    close(srv);
    return -1;
  }

  for(;;) {
    int bufsize = SOCK_BUF_SIZE, one = 1;
    pthread_t thr;
    int conn;

    if((conn = accept(srv, NULL, NULL)) < 0) {
      if(errno == EINTR) {
        continue;
      }
      plat_log("accept: %s", strerror(errno));
      break;
    }
    setsockopt(conn, SOL_SOCKET, SO_RCVBUF, &bufsize, sizeof bufsize);
    setsockopt(conn, SOL_SOCKET, SO_SNDBUF, &bufsize, sizeof bufsize);
    setsockopt(conn, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
    setsockopt(conn, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);

    if(pthread_create(&thr, NULL, client_thread, (void *)(intptr_t)conn)) {
      close(conn);
      continue;
    }
    pthread_detach(thr);
  }

  close(srv);
  return -1;
}

int
main(int argc, char *argv[]) {
  uint16_t port = P5CX_PORT;
  int notify = 1;
  pthread_t thr;
  int c;

  while((c = getopt(argc, argv, "p:qh")) != -1) {
    switch(c) {
    case 'p':
      port = (uint16_t)atoi(optarg);
      break;
    case 'q':
      notify = 0;
      break;
    default:
      printf("usage: %s [-p PORT] [-q]\n", argv[0]);
      printf("  -p PORT  TCP+UDP port to listen on (default %d)\n", P5CX_PORT);
      printf("  -q       no on-screen notification at startup\n");
      return EXIT_FAILURE;
    }
  }

  signal(SIGPIPE, SIG_IGN);
  if(plat_init()) {
    return EXIT_FAILURE;
  }

  /* With a direct cable the link may come up after the payload starts, so
   * wait for an address instead of exiting. */
  while(!announce_addresses(port, 0)) {
    sleep(2);
  }
  announce_addresses(port, notify);

  if(!pthread_create(&thr, NULL, discovery_thread, (void *)(intptr_t)port)) {
    pthread_detach(thr);
  }

  for(;;) {
    serve(port);
    sleep(3);
  }
  return EXIT_SUCCESS;
}
