#pragma once

#include <stddef.h>
#include <stdint.h>

/* Helpers shared with server.c. */
int send_res(int fd, uint8_t kind, uint32_t mode, uint64_t a, uint64_t b);
int send_err(int fd, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
int mkdir_parent(const char *path);

/* CMD_FETCH. `msg` is "dest\\0url" or "dest\\0url\\0headers", `len` bytes,
 * not counting the extra NUL the caller writes at msg[len].
 * Two responses on success: one when the download is accepted (a = total
 * size or 0 if unknown, b = offset we start writing at), then one when it
 * finishes (a = bytes written, b = final size). */
int handle_fetch(int fd, char *msg, size_t len, uint8_t flags, uint64_t offset);
