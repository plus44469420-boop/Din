#pragma once

/* Called once at startup, before any sockets are opened. Returns 0 on success. */
int plat_init(void);

/* On-screen toast on the PS5; stdout on the host build. */
void plat_notify(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* stdout plus the PS5 kernel log (readable with klogsrv / `nc ps5 3232`). */
void plat_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
