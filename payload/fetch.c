#include <curl/curl.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include "fetch.h"
#include "platform.h"
#include "proto.h"

#ifdef P5CX_EMBED_CA
#include "ca_bundle.h"
#endif

#define UA "PS5Cable/0.2"

typedef struct {
  int client;
  int file;
  int status;
  int accepted;
  int client_dead;
  int need_restart;
  uint64_t offset;
  uint64_t written;
  uint64_t total;
  uint64_t content_length;
  int have_length;
  int have_total;
  const char *dest;
  uint8_t flags;
  char failmsg[512];
} fetch_ctx;

static void
init_curl(void) {
  curl_global_init(CURL_GLOBAL_DEFAULT);
}

static int
client_gone(int fd) {
  struct pollfd pfd = {.fd = fd, .events = POLLIN};
  char b;
  int rc = poll(&pfd, 1, 0);

  if(rc < 0) {
    return errno != EINTR;
  }
  if(rc == 0) {
    return 0;
  }
  if(pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
    return 1;
  }
  if(pfd.revents & POLLIN) {
    ssize_t n = recv(fd, &b, 1, MSG_PEEK | MSG_DONTWAIT);
    if(n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
      return 1;
    }
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

static void
note_download(const char *path) {
  static time_t last;
  const char *base = strrchr(path, '/');
  time_t now = time(NULL);

  base = base && base[1] ? base + 1 : path;
  if(now - last < 8) {
    plat_log("fetch: %s", path);
    return;
  }
  last = now;
  plat_notify("PS5 Cable: downloading %s", base);
}

static int
bad_url(const char *url) {
  const unsigned char *p;

  if(strncmp(url, "https://", 8) && strncmp(url, "http://", 7)) {
    return 1;
  }
  if(strlen(url) < 8 || strlen(url) > 8000) {
    return 1;
  }
  for(p = (const unsigned char *)url; *p; p++) {
    if(*p <= 32 || *p == 127) {
      return 1;
    }
  }
  return 0;
}

/* Names include the colon. Anything after it is the value. */
static int
allowed_header(const char *line) {
  static const char *names[] = {"Referer:", "Cookie:", "User-Agent:", "Authorization:", NULL};
  int i;

  for(i = 0; names[i]; i++) {
    if(!strncasecmp(line, names[i], strlen(names[i]))) {
      return 1;
    }
  }
  return 0;
}

static int
prepare_file(fetch_ctx *ctx) {
  int file;

  if((ctx->flags & F_MKDIRS) && mkdir_parent(ctx->dest)) {
    snprintf(ctx->failmsg, sizeof ctx->failmsg, "mkdir: %s: %s", ctx->dest, strerror(errno));
    return -1;
  }
  if(ctx->offset == 0) {
    file = open(ctx->dest, O_WRONLY | O_CREAT | O_TRUNC, 0777);
  } else {
    file = open(ctx->dest, O_WRONLY);
    if(file >= 0 && (lseek(file, (off_t)ctx->offset, SEEK_SET) < 0 ||
                     ftruncate(file, (off_t)ctx->offset))) {
      snprintf(ctx->failmsg, sizeof ctx->failmsg, "seek: %s: %s", ctx->dest, strerror(errno));
      close(file);
      return -1;
    }
  }
  if(file < 0) {
    snprintf(ctx->failmsg, sizeof ctx->failmsg, "open: %s: %s", ctx->dest, strerror(errno));
    return -1;
  }
  ctx->file = file;
  return 0;
}

static size_t
header_cb(char *ptr, size_t size, size_t nmemb, void *userdata) {
  fetch_ctx *ctx = userdata;
  size_t n = size * nmemb;
  char line[2048];
  size_t copy = n < sizeof line - 1 ? n : sizeof line - 1;
  int blank;

  if(client_gone(ctx->client)) {
    ctx->client_dead = 1;
    return 0;
  }
  memcpy(line, ptr, copy);
  line[copy] = '\0';
  blank = (n == 2 && ptr[0] == '\r' && ptr[1] == '\n') || (n == 1 && ptr[0] == '\n');

  if(n >= 5 && !strncasecmp(line, "HTTP/", 5)) {
    char *sp = strchr(line, ' ');
    ctx->status = sp ? atoi(sp + 1) : 0;
    ctx->have_length = 0;
    ctx->have_total = 0;
    ctx->content_length = 0;
    ctx->total = 0;
    return n;
  }
  if(!strncasecmp(line, "Content-Length:", 15)) {
    ctx->content_length = strtoull(line + 15, NULL, 10);
    ctx->have_length = 1;
    return n;
  }
  if(!strncasecmp(line, "Content-Range:", 14)) {
    char *slash = strchr(line, '/');
    if(slash && slash[1] && slash[1] != '*') {
      ctx->total = strtoull(slash + 1, NULL, 10);
      ctx->have_total = 1;
    }
    return n;
  }
  if(!blank || ctx->accepted) {
    return n;
  }
  if(ctx->status == 200 && ctx->offset > 0) {
    /* Range was ignored. Abort this attempt and download the whole file. */
    ctx->need_restart = 1;
    return 0;
  }
  if(ctx->status != 200 && ctx->status != 206) {
    return n;
  }
  if(!ctx->have_total) {
    ctx->total = ctx->have_length ? ctx->offset + ctx->content_length : 0;
  }
  if(prepare_file(ctx)) {
    return 0;
  }
  if(send_res(ctx->client, KIND_FILE, 0, ctx->total, ctx->offset)) {
    ctx->client_dead = 1;
    return 0;
  }
  ctx->accepted = 1;
  note_download(ctx->dest);
  return n;
}

static size_t
write_cb(char *ptr, size_t size, size_t nmemb, void *userdata) {
  fetch_ctx *ctx = userdata;
  size_t n = size * nmemb;

  if(client_gone(ctx->client)) {
    ctx->client_dead = 1;
    return 0;
  }
  if(!ctx->accepted) {
    return n;
  }
  if(write_full(ctx->file, ptr, n)) {
    snprintf(ctx->failmsg, sizeof ctx->failmsg, "write: %s: %s", ctx->dest, strerror(errno));
    return 0;
  }
  ctx->written += (uint64_t)n;
  return n;
}

static int
xferinfo(void *userdata, curl_off_t dltotal, curl_off_t dlnow,
         curl_off_t ultotal, curl_off_t ulnow) {
  fetch_ctx *ctx = userdata;

  (void)dltotal;
  (void)dlnow;
  (void)ultotal;
  (void)ulnow;
  if(client_gone(ctx->client)) {
    ctx->client_dead = 1;
    return 1;
  }
  return 0;
}

static int
split_request(char *msg, size_t len, const char **url, const char **hdrs) {
  char *u = memchr(msg, '\0', len);
  char *h;

  if(!u || u == msg || (size_t)(u - msg) + 1 >= len) {
    return -1;
  }
  *url = u + 1;
  if(**url == '\0') {
    return -1;
  }
  h = memchr(u + 1, '\0', len - (size_t)(u + 1 - msg));
  *hdrs = (h && (size_t)(h - msg) + 1 < len) ? h + 1 : NULL;
  return 0;
}

/* 0 and *out set (possibly NULL). -1 rejects the header block. */
static int
build_headers(const char *hdrs, struct curl_slist **out) {
  struct curl_slist *list = NULL;
  const char *p = hdrs;
  int count = 0;

  *out = NULL;
  if(!p || !p[0]) {
    return 0;
  }
  while(*p && count < 8) {
    const char *nl = strchr(p, '\n');
    size_t n = nl ? (size_t)(nl - p) : strlen(p);
    char line[2048];
    struct curl_slist *next;

    if(n >= sizeof line) {
      curl_slist_free_all(list);
      return -1;
    }
    memcpy(line, p, n);
    line[n] = '\0';
    if(n && line[n - 1] == '\r') {
      line[n - 1] = '\0';
    }
    if(line[0]) {
      if(strchr(line, '\r') || !allowed_header(line)) {
        curl_slist_free_all(list);
        return -1;
      }
      next = curl_slist_append(list, line);
      if(!next) {
        curl_slist_free_all(list);
        return -1;
      }
      list = next;
      count++;
    }
    if(!nl) {
      break;
    }
    p = nl + 1;
  }
  *out = list;
  return 0;
}

static CURLcode
perform(CURL *curl, fetch_ctx *ctx, const char *url, struct curl_slist *headers,
        char *errbuf) {
  CURLcode res;

  curl_easy_setopt(curl, CURLOPT_URL, url);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
  curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
  curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 10L);
  curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_PROXY, "");
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 20L);
  curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1L);
  curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, 120L);
  curl_easy_setopt(curl, CURLOPT_USERAGENT, UA);
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "http,https");
  curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, "http,https");
  curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_cb);
  curl_easy_setopt(curl, CURLOPT_HEADERDATA, ctx);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, ctx);
  curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, xferinfo);
  curl_easy_setopt(curl, CURLOPT_XFERINFODATA, ctx);
  curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
  curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, errbuf);
  curl_easy_setopt(curl, CURLOPT_RESUME_FROM_LARGE, (curl_off_t)ctx->offset);
#ifdef P5CX_EMBED_CA
  {
    struct curl_blob blob = {
      .data = (void *)ca_pem,
      .len = sizeof ca_pem - 1,
      .flags = CURL_BLOB_NOCOPY,
    };
    curl_easy_setopt(curl, CURLOPT_CAINFO_BLOB, &blob);
  }
#endif

  res = curl_easy_perform(curl);
  if(ctx->need_restart && !ctx->accepted && !ctx->client_dead) {
    ctx->need_restart = 0;
    ctx->offset = 0;
    ctx->status = 0;
    ctx->failmsg[0] = '\0';
    errbuf[0] = '\0';
    curl_easy_setopt(curl, CURLOPT_RESUME_FROM_LARGE, (curl_off_t)0);
    res = curl_easy_perform(curl);
  }
  return res;
}

int
handle_fetch(int fd, char *msg, size_t len, uint8_t flags, uint64_t offset) {
  static pthread_once_t once = PTHREAD_ONCE_INIT;
  const char *url = NULL;
  const char *hdrs = NULL;
  struct stat st;
  struct curl_slist *headers = NULL;
  fetch_ctx ctx;
  CURL *curl = NULL;
  char errbuf[CURL_ERROR_SIZE];
  int rc = 0;
  CURLcode cres;

  pthread_once(&once, init_curl);
  memset(&ctx, 0, sizeof ctx);
  ctx.client = fd;
  ctx.file = -1;
  ctx.offset = offset;
  ctx.flags = flags;
  errbuf[0] = '\0';

  if(split_request(msg, len, &url, &hdrs)) {
    return send_err(fd, "fetch: expected a destination and an http(s) link");
  }
  ctx.dest = msg;
  if(msg[0] != '/' || strstr(msg, "/../") || strpbrk(msg, "\r\n") ||
     strlen(msg) >= P5CX_MAX_PATH) {
    return send_err(fd, "fetch: destination must be an absolute file path");
  }
  if(bad_url(url)) {
    return send_err(fd, "fetch: link must be http or https");
  }
  if(!stat(msg, &st) && S_ISDIR(st.st_mode)) {
    return send_err(fd, "fetch: %s is a directory", msg);
  }
  if(offset) {
    if(stat(msg, &st) || !S_ISREG(st.st_mode) || (uint64_t)st.st_size < offset) {
      return send_err(fd, "fetch: %s is shorter than the resume offset", msg);
    }
  }
  if(build_headers(hdrs, &headers)) {
    return send_err(fd, "fetch: rejected a request header");
  }

  curl = curl_easy_init();
  if(!curl) {
    curl_slist_free_all(headers);
    return send_err(fd, "fetch: out of memory");
  }

  cres = perform(curl, &ctx, url, headers, errbuf);
  if(ctx.file >= 0 && close(ctx.file) && !ctx.failmsg[0]) {
    snprintf(ctx.failmsg, sizeof ctx.failmsg, "close: %s: %s", ctx.dest, strerror(errno));
  }
  ctx.file = -1;

  if(ctx.client_dead) {
    rc = -1;
  } else if(!ctx.accepted) {
    if(ctx.failmsg[0]) {
      rc = send_err(fd, "%s", ctx.failmsg);
    } else if(ctx.status >= 400) {
      rc = send_err(fd, "download failed: HTTP %d", ctx.status);
    } else if(cres != CURLE_OK) {
      rc = send_err(fd, "download failed: %s",
                    errbuf[0] ? errbuf : curl_easy_strerror(cres));
    } else {
      rc = send_err(fd, "download failed");
    }
  } else if(ctx.failmsg[0] || cres != CURLE_OK) {
    if(client_gone(fd)) {
      rc = -1;
    } else if(ctx.failmsg[0]) {
      rc = send_err(fd, "%s", ctx.failmsg);
    } else {
      rc = send_err(fd, "download failed: %s",
                    errbuf[0] ? errbuf : curl_easy_strerror(cres));
    }
  } else {
    rc = send_res(fd, KIND_FILE, 0, ctx.written, ctx.offset + ctx.written);
  }

  curl_slist_free_all(headers);
  curl_easy_cleanup(curl);
  return rc;
}
