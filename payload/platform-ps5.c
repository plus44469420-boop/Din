#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <sys/syscall.h>
#include <sys/sysctl.h>

#include <ps5/kernel.h>
#include <ps5/klog.h>

#include "platform.h"

#define THREAD_NAME "ps5cable.elf"

/* Offsets into FreeBSD's struct kinfo_proc as laid out on the PS5 kernel. */
#define KI_PID_OFFSET    72
#define KI_TDNAME_OFFSET 447

typedef struct notify_request {
  char reserved[45];
  char message[3075];
} notify_request_t;

int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);

static pid_t
find_other_instance(void) {
  int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0};
  pid_t self = getpid();
  pid_t found = -1;
  size_t size = 0;
  uint8_t *buf;

  if(sysctl(mib, 4, NULL, &size, NULL, 0)) {
    return -1;
  }
  if(!(buf = malloc(size))) {
    return -1;
  }
  if(sysctl(mib, 4, buf, &size, NULL, 0)) {
    free(buf);
    return -1;
  }

  for(uint8_t *p = buf; p < buf + size;) {
    int structsize = *(int *)p;
    pid_t pid = *(pid_t *)&p[KI_PID_OFFSET];
    const char *tdname = (const char *)&p[KI_TDNAME_OFFSET];

    if(structsize <= 0) {
      break;
    }
    if(pid != self && !strcmp(tdname, THREAD_NAME)) {
      found = pid;
    }
    p += structsize;
  }

  free(buf);
  return found;
}

int
plat_init(void) {
  pid_t pid;

  syscall(SYS_thr_set_name, -1, THREAD_NAME);

  /* Re-sending the payload replaces the running copy instead of failing on
   * "address already in use". */
  while((pid = find_other_instance()) > 0) {
    if(kill(pid, SIGKILL)) {
      plat_log("unable to stop previous instance (pid %d)", pid);
      return -1;
    }
    sleep(1);
  }

  /* Same AuthID ftpsrv uses: lets the process open protected paths and
   * character devices that the default payload credentials cannot. */
  if(kernel_set_ucred_authid(getpid(), 0x4801000000000013L)) {
    plat_log("unable to change AuthID");
    return -1;
  }

  return 0;
}

void
plat_notify(const char *fmt, ...) {
  notify_request_t req;
  va_list args;

  memset(&req, 0, sizeof req);
  va_start(args, fmt);
  vsnprintf(req.message, sizeof req.message, fmt, args);
  va_end(args);

  sceKernelSendNotificationRequest(0, &req, sizeof req, 0);
  plat_log("%s", req.message);
}

void
plat_log(const char *fmt, ...) {
  char line[1024];
  va_list args;

  va_start(args, fmt);
  vsnprintf(line, sizeof line, fmt, args);
  va_end(args);

  puts(line);
  klog_printf("[ps5cable] %s\n", line);
}
