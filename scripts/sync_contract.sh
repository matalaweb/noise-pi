#!/usr/bin/env bash
# Re-vendor the web app's device API contract into contract/upstream (review the diff afterwards).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="${1:-$ROOT/../my-neighbor-sucks}"
rm -rf "$ROOT/contract/upstream"
mkdir -p "$ROOT/contract/upstream"
cp -r "$WEB/docs/openapi/." "$ROOT/contract/upstream/"
(cd "$WEB" && git rev-parse HEAD && git status --porcelain docs/openapi app) > "$ROOT/contract/upstream/SOURCE"
echo "vendored $(head -1 "$ROOT/contract/upstream/SOURCE"); run pytest tests/contract"
