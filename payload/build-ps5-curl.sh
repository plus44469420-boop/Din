#!/usr/bin/env bash
# Build a static libcurl + mbedTLS the PS5 payload can link, plus a CA bundle
# header. Installed under payload/.deps (not committed). Re-run by deleting
# that directory.
set -euo pipefail

: "${PS5_PAYLOAD_SDK:?Set PS5_PAYLOAD_SDK to the ps5-payload-sdk install}"
if [[ ! -x "$PS5_PAYLOAD_SDK/bin/prospero-clang" ]]; then
  echo "prospero-clang not found in $PS5_PAYLOAD_SDK/bin" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEPS="$ROOT/.deps/ps5"
PREFIX="$DEPS/prefix"
MBED_VER=3.6.4
CURL_VER=8.12.1

if [[ -f "$PREFIX/lib/libcurl.a" && -f "$PREFIX/include/ca_bundle.h" ]]; then
  exit 0
fi

export PATH="$PS5_PAYLOAD_SDK/bin:$PATH"
export CC=prospero-clang
export AR=prospero-ar
export RANLIB=prospero-ranlib
export NM=prospero-nm

mkdir -p "$DEPS/src"
cd "$DEPS/src"

if [[ ! -f "$PREFIX/lib/libmbedtls.a" ]]; then
  echo "Building mbedTLS $MBED_VER"
  rm -rf "mbedtls-$MBED_VER"
  curl -fsSL -o mbedtls.tar.gz "https://github.com/Mbed-TLS/mbedtls/archive/refs/tags/v$MBED_VER.tar.gz"
  tar -xzf mbedtls.tar.gz
  # The GitHub archive leaves out the framework submodule. The generated
  # sources are already in the tree; a stub dependency keeps make from
  # trying to regenerate them.
  mkdir -p "mbedtls-$MBED_VER/framework/scripts"
  touch "mbedtls-$MBED_VER/framework/scripts/generate_ssl_debug_helpers.py"
  touch "mbedtls-$MBED_VER/library/ssl_debug_helpers_generated.c"
  make -C "mbedtls-$MBED_VER" CC=prospero-clang AR=prospero-ar RANLIB=prospero-ranlib \
    CFLAGS="-Os" lib -j"$(nproc)"
  mkdir -p "$PREFIX/include" "$PREFIX/lib"
  cp -a "mbedtls-$MBED_VER/include/mbedtls" "mbedtls-$MBED_VER/include/psa" "$PREFIX/include/"
  cp "mbedtls-$MBED_VER/library/libmbedtls.a" \
     "mbedtls-$MBED_VER/library/libmbedx509.a" \
     "mbedtls-$MBED_VER/library/libmbedcrypto.a" "$PREFIX/lib/"
fi

if [[ ! -f "$PREFIX/lib/libcurl.a" ]]; then
  echo "Building libcurl $CURL_VER"
  rm -rf "curl-$CURL_VER"
  curl -fsSL -o curl.tar.xz "https://curl.se/download/curl-$CURL_VER.tar.xz"
  tar -xJf curl.tar.xz
  cd "curl-$CURL_VER"
  ./configure --prefix="$PREFIX" \
    --host=x86_64-pc-freebsd \
    --enable-static --disable-shared \
    --with-mbedtls="$PREFIX" \
    --without-openssl --without-gnutls --without-bearssl \
    --without-zlib --without-brotli --without-zstd --without-libpsl \
    --without-libidn2 --without-nghttp2 \
    --disable-ldap --disable-ldaps --disable-rtsp --disable-proxy \
    --disable-dict --disable-telnet --disable-tftp --disable-pop3 \
    --disable-imap --disable-smb --disable-smtp --disable-gopher \
    --disable-mqtt --disable-ftp --disable-file \
    --disable-docs --disable-manual \
    --without-ca-bundle --without-ca-path
  make -j"$(nproc)"
  make install
  cd "$DEPS/src"
fi

if [[ ! -f "$PREFIX/include/ca_bundle.h" ]]; then
  echo "Embedding CA bundle"
  curl -fsSL -o "$DEPS/cacert.pem" https://curl.se/ca/cacert.pem
  python3 - "$DEPS/cacert.pem" "$PREFIX/include/ca_bundle.h" <<'PY'
import pathlib, sys
pem = pathlib.Path(sys.argv[1]).read_bytes()
if not pem.endswith(b"\n"):
    pem += b"\n"
lines = ["#pragma once", "/* Mozilla CA bundle, so the PS5 can verify https links. */",
         "static const char ca_pem[] ="]
def c_string(raw):
    out = []
    for byte in raw:
        if byte == 0x22:
            out.append('\\"')
        elif byte == 0x5C:
            out.append("\\\\")
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)

for raw in pem.splitlines():
    lines.append(f'"{c_string(raw)}\\n"')
lines.append(";")
pathlib.Path(sys.argv[2]).write_text("\n".join(lines) + "\n")
PY
fi

echo "curl for the PS5 payload is in $PREFIX"
