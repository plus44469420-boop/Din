#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>

#include "platform.h"
#include "proto.h"
#include "usblink.h"

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

static const char version[] = "ps5cable " VERSION_TAG
#ifdef __PROSPERO__
  " (ps5)";
#else
  " (host)";
#endif

static int
handle_ping(int fd) {
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

/* The PS5 kernel does not return EXDEV for rename(2) across mounts: it
 * panics (black screen, power-cord pull). Both ends must be proven to live
 * on the same device first, and any doubt counts as "different". */
static int
same_device(const char *src, const char *dst) {
  char parent[P5CX_MAX_PATH];
  struct stat s, d;
  char *slash;

  if(lstat(src, &s)) {
    return 0;
  }
  if(!lstat(dst, &d)) {
    return s.st_dev == d.st_dev;
  }
  snprintf(parent, sizeof parent, "%s", dst);
  if(!(slash = strrchr(parent, '/'))) {
    return 0;
  }
  if(slash == parent) {
    slash[1] = '\0';
  } else {
    *slash = '\0';
  }
  if(stat(parent, &d)) {
    return 0;
  }
  return s.st_dev == d.st_dev;
}

static int
handle_rename(int fd, const char *path, size_t pathlen) {
  size_t srclen = strlen(path);
  const char *dst;

  if(srclen + 1 >= pathlen) {
    return send_err(fd, "rename: expected \"src\\0dst\"");
  }
  dst = path + srclen + 1;
  if(dst[0] != '/') {
    return send_err(fd, "rename: destination must be absolute: \"%s\"", dst);
  }
  if(!same_device(path, dst)) {
    return send_err(fd, "rename: %s and %s are on different drives (or missing). "
                    "Moving between drives crashes the PS5, so copy instead.",
                    path, dst);
  }
  if(rename(path, dst)) {
    return send_err(fd, "rename: %s -> %s: %s", path, dst, strerror(errno));
  }
  return send_ok(fd);
}

/* Copies one regular file on the console, at most `max` bytes per request so
 * the client can show progress, cancel and resume between chunks. */
static int
handle_copy(int fd, const char *path, size_t pathlen, uint64_t offset,
            uint64_t max, uint8_t *buf) {
  size_t srclen = strlen(path);
  uint64_t remaining, copied = 0;
  struct stat st, dst_st;
  const char *dst;
  int in, out;

  if(srclen + 1 >= pathlen) {
    return send_err(fd, "copy: expected \"src\\0dst\"");
  }
  dst = path + srclen + 1;
  if(dst[0] != '/') {
    return send_err(fd, "copy: destination must be absolute: \"%s\"", dst);
  }
  if((in = open(path, O_RDONLY)) < 0) {
    return SEND_ERRNO(fd, "open", path);
  }
  if(fstat(in, &st)) {
    int err = errno;
    close(in);
    errno = err;
    return SEND_ERRNO(fd, "stat", path);
  }
  if(!S_ISREG(st.st_mode)) {
    close(in);
    return send_err(fd, "copy: %s: not a regular file", path);
  }
  if(!stat(dst, &dst_st) && dst_st.st_dev == st.st_dev &&
     dst_st.st_ino == st.st_ino) {
    close(in);
    return send_err(fd, "copy: %s: source and destination are the same file",
                    path);
  }
  if(offset > (uint64_t)st.st_size) {
    close(in);
    return send_err(fd, "copy: %s: offset past end of file", path);
  }
  if(mkdir_parent(dst)) {
    int err = errno;
    close(in);
    errno = err;
    return SEND_ERRNO(fd, "mkdir", dst);
  }

  out = offset ? open(dst, O_WRONLY) : open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0777);
  if(out < 0) {
    int err = errno;
    close(in);
    errno = err;
    return SEND_ERRNO(fd, "open", dst);
  }
  if(offset && (fstat(out, &dst_st) || (uint64_t)dst_st.st_size < offset)) {
    close(in);
    close(out);
    return send_err(fd, "copy: %s is shorter than the resume offset", dst);
  }
  if((offset && ftruncate(out, (off_t)offset)) ||
     lseek(in, (off_t)offset, SEEK_SET) < 0 ||
     lseek(out, (off_t)offset, SEEK_SET) < 0) {
    int err = errno;
    close(in);
    close(out);
    errno = err;
    return SEND_ERRNO(fd, "seek", dst);
  }

  remaining = (uint64_t)st.st_size - offset;
  if(max && max < remaining) {
    remaining = max;
  }
  while(remaining) {
    size_t want = remaining < IO_BUF_SIZE ? (size_t)remaining : IO_BUF_SIZE;
    ssize_t n = read(in, buf, want);

    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0) {
      int err = n ? errno : EIO;
      close(in);
      close(out);
      errno = err;
      return SEND_ERRNO(fd, "read", path);
    }
    if(write_full(out, buf, (size_t)n)) {
      int err = errno;
      close(in);
      close(out);
      errno = err;
      return SEND_ERRNO(fd, "write", dst);
    }
    remaining -= (uint64_t)n;
    copied += (uint64_t)n;
  }
  close(in);
  if(close(out)) {
    return SEND_ERRNO(fd, "close", dst);
  }
  return send_res(fd, KIND_FILE, (uint32_t)st.st_mode, copied,
                  (uint64_t)st.st_size);
}

typedef struct {
  uint64_t bytes;
  uint64_t files;
  uint32_t dirs;
} du_t;

/* Does not follow symlinks, and heap-allocates child paths like rm_rf. */
static int
du_walk(const char *path, du_t *du) {
  struct dirent *ent;
  struct stat st;
  DIR *dir;

  if(lstat(path, &st)) {
    return -1;
  }
  if(!S_ISDIR(st.st_mode)) {
    du->bytes += (uint64_t)st.st_size;
    du->files++;
    return 0;
  }
  du->dirs++;
  if(!(dir = opendir(path))) {
    return 0;
  }
  while((ent = readdir(dir))) {
    char *child;

    if(!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) {
      continue;
    }
    if(asprintf(&child, "%s/%s", path, ent->d_name) < 0) {
      closedir(dir);
      errno = ENOMEM;
      return -1;
    }
    du_walk(child, du);
    free(child);
  }
  closedir(dir);
  return 0;
}

static int
handle_du(int fd, const char *path) {
  du_t du = {0};

  if(du_walk(path, &du)) {
    return SEND_ERRNO(fd, "du", path);
  }
  if(du.dirs) {
    du.dirs--;
  }
  return send_res(fd, 0, du.dirs, du.bytes, du.files);
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

/* Speed tests move bytes over the cable without touching storage, so they
 * measure the link (cable, adapter, USB port) and nothing else. */
static uint64_t
now_us(void) {
  struct timespec ts;

  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)ts.tv_nsec / 1000ULL;
}

static int
handle_sink(int fd, uint64_t total, uint8_t *buf) {
  uint64_t remaining = total, start;

  if(total > P5CX_MAX_SPEEDTEST) {
    return send_err(fd, "speed test too large");
  }
  if(send_ok(fd)) {
    return -1;
  }
  start = now_us();
  while(remaining) {
    size_t want = remaining < IO_BUF_SIZE ? (size_t)remaining : IO_BUF_SIZE;
    ssize_t n = read(fd, buf, want);

    if(n < 0 && errno == EINTR) {
      continue;
    }
    if(n <= 0) {
      return -1;
    }
    remaining -= (uint64_t)n;
  }
  return send_res(fd, 0, 0, total, now_us() - start);
}

static int
handle_source(int fd, uint64_t total, uint8_t *buf) {
  uint64_t remaining = total;

  if(total > P5CX_MAX_SPEEDTEST) {
    return send_err(fd, "speed test too large");
  }
  if(send_res(fd, 0, 0, total, 0)) {
    return -1;
  }
  memset(buf, 0, IO_BUF_SIZE);
  while(remaining) {
    size_t n = remaining < IO_BUF_SIZE ? (size_t)remaining : IO_BUF_SIZE;

    if(write_full(fd, buf, n)) {
      return -1;
    }
    remaining -= n;
  }
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

    if(req.cmd != CMD_PING && req.cmd != CMD_SHUTDOWN && req.cmd != CMD_SINK &&
       req.cmd != CMD_SOURCE && path[0] != '/') {
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
    case CMD_SINK:     rc = handle_sink(fd, req.a, buf); break;
    case CMD_SOURCE:   rc = handle_source(fd, req.a, buf); break;
    case CMD_COPY:     rc = handle_copy(fd, path, req.pathlen, req.a, req.b, buf); break;
    case CMD_DU:       rc = handle_du(fd, path); break;
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

static int g_notify = 1;

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
  /* Only the USB link (usblink.c) connects, over loopback; nothing on the
   * console's network can reach the file server. */
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
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
  const char *cable = NULL;
  int c;

  while((c = getopt(argc, argv, "p:qC:h")) != -1) {
    switch(c) {
    case 'p':
      port = (uint16_t)atoi(optarg);
      break;
    case 'q':
      g_notify = 0;
      break;
    case 'C':
      cable = optarg;
      break;
    default:
      printf("usage: %s [-p PORT] [-q] [-C HOST:PORT]\n", argv[0]);
      printf("  -p PORT       file server port on 127.0.0.1 (default %d)\n", P5CX_PORT);
      printf("  -q            no on-screen notifications\n");
      printf("  -C HOST:PORT  desktop build: use the fake USB cable at HOST:PORT\n");
      return EXIT_FAILURE;
    }
  }

  signal(SIGPIPE, SIG_IGN);
  if(plat_init()) {
    return EXIT_FAILURE;
  }

  usb_start(port, version, cable);
  if(g_notify) {
    plat_notify("ps5cable ready: connect the USB-C bridge cable to the PC");
  }

  for(;;) {
    serve(port);
    sleep(3);
  }
  return EXIT_SUCCESS;
}
