/* ps5cable wire protocol.
 *
 * Every request and response starts with a fixed 24-byte header. All integers
 * are little-endian (both the PS5 and the PC are x86-64, and the Python client
 * packs explicitly with '<').
 *
 *   request:  magic[4]="P5CX" cmd:u8 flags:u8 pathlen:u16 a:u64 b:u64
 *             followed by `pathlen` bytes of UTF-8 path (not NUL-terminated)
 *   response: status:u8 kind:u8 reserved:u16 mode:u32 a:u64 b:u64
 *
 * On status == ST_ERR, `a` holds the length of a UTF-8 error message that
 * immediately follows the header. A connection may carry any number of
 * requests back to back.
 */
#pragma once

#include <stdint.h>

#define P5CX_MAGIC        "P5CX"
#define P5CX_PROTO_VER    1
#define P5CX_PORT         9113
#define P5CX_MAX_PATH     4096

enum {
  CMD_PING     = 1,  /* -> a=proto version, b=len of version string that follows */
  CMD_LIST     = 2,  /* -> a=len of entry blob that follows */
  CMD_STAT     = 3,  /* -> kind, mode, a=size, b=mtime */
  CMD_MKDIR    = 4,  /* mkdir -p */
  CMD_DELETE   = 5,  /* flags: F_RECURSIVE */
  CMD_RENAME   = 6,  /* path = "src\0dst" */
  CMD_PUT      = 7,  /* a=total size, b=offset to resume at; two responses */
  CMD_GET      = 8,  /* a=offset -> a=bytes that follow, b=total size */
  CMD_DF       = 9,  /* -> a=free bytes, b=total bytes */
  CMD_SHUTDOWN = 10, /* stop the payload */
};

enum {
  F_RECURSIVE = 1 << 0,
  F_MKDIRS    = 1 << 1,
};

enum {
  ST_OK  = 0,
  ST_ERR = 1,
};

enum {
  KIND_FILE  = 0,
  KIND_DIR   = 1,
  KIND_OTHER = 2,
};

/* LIST entry: kind:u8 size:u64 mtime:i64 namelen:u16 name[namelen] */

#pragma pack(push, 1)
typedef struct {
  char     magic[4];
  uint8_t  cmd;
  uint8_t  flags;
  uint16_t pathlen;
  uint64_t a;
  uint64_t b;
} p5cx_req_t;

typedef struct {
  uint8_t  status;
  uint8_t  kind;
  uint16_t reserved;
  uint32_t mode;
  uint64_t a;
  uint64_t b;
} p5cx_res_t;
#pragma pack(pop)

_Static_assert(sizeof(p5cx_req_t) == 24, "request header must be 24 bytes");
_Static_assert(sizeof(p5cx_res_t) == 24, "response header must be 24 bytes");
