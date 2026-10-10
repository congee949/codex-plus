#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
node_headers=${NODE_HEADERS_DIR:-}
if [ -z "$node_headers" ]; then
  for candidate in "$HOME"/Library/Caches/node-gyp/*/include/node; do
    if [ -f "$candidate/node_api.h" ]; then
      node_headers=$candidate
    fi
  done
fi
if [ ! -f "$node_headers/node_api.h" ]; then
  printf '%s\n' 'Set NODE_HEADERS_DIR to a directory containing node_api.h.' >&2
  exit 1
fi
temporary=$(mktemp "$project_dir/assets/local-app-tools-peer.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
clang++ -std=c++17 -Wall -Wextra -Werror -bundle -undefined dynamic_lookup \
  -DNAPI_VERSION=8 -DNODE_GYP_MODULE_NAME=local_peer -I"$node_headers" \
  "$project_dir/assets/local-app-tools-peer.cpp" \
  -framework Security -framework CoreFoundation -lbsm -o "$temporary"
codesign --force --sign - "$temporary"
codesign --verify --strict "$temporary"
mv "$temporary" "$project_dir/assets/local-app-tools-peer.node"
