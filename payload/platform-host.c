/* Desktop build of the platform layer, used to run the server on Linux/macOS
 * for testing the protocol and the PC client without a console. */
#include <stdarg.h>
#include <stdio.h>

#include "platform.h"

int
plat_init(void) {
  return 0;
}

void
plat_notify(const char *fmt, ...) {
  va_list args;

  va_start(args, fmt);
  printf("[notify] ");
  vprintf(fmt, args);
  putchar('\n');
  va_end(args);
  fflush(stdout);
}

void
plat_log(const char *fmt, ...) {
  va_list args;

  va_start(args, fmt);
  vprintf(fmt, args);
  putchar('\n');
  va_end(args);
  fflush(stdout);
}
