#!/usr/bin/env bash
# Run tests/integration/test_laravel_live.py against the web app's local Docker stack.
#
#   scripts/run_live_contract_test.sh [path-to-web-app]
#
# What this changes: provisions one new SYNTHETIC demo device (noise:demo:provision) in the web
# app's local development database; the test then uploads synthetic readings/events/audio for it.
# The collector runs in a throwaway container on the stack's network so presigned S3 URLs
# (http://s3:9000/...) resolve exactly as the server signs them.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="${1:-$ROOT/../my-neighbor-sucks}"
cd "$WEB"
NET="$(docker compose ps --format '{{.Networks}}' app | head -1)"
OUT="$(docker compose exec -T app php artisan noise:demo:provision --account='Collector live contract test' 2>/dev/null)"
TOKEN="$(grep -Eo 'nmd_[A-Za-z0-9]+' <<<"$OUT" | head -1)"
DEVICE="$(grep -E '^\| Device ' <<<"$OUT" | grep -Eo '[0-9a-f]{8}-[0-9a-f-]{27}' | head -1)"
[[ -n "$TOKEN" && -n "$DEVICE" ]] || { echo "could not provision a demo device"; exit 1; }
# The collector registers its own SYNTHETIC calibrated chain (with a frequency-response file).
docker run --rm --user root --network "$NET" -v "$ROOT:/src:ro" -w /tmp \
  -e NOISE_LARAVEL_URL=http://app:8080 -e NOISE_LARAVEL_TOKEN="$TOKEN" -e NOISE_LARAVEL_STORAGE_HOSTS=s3 \
  -e PYTHONPATH=/src/src:/src/tests -e PYTHONDONTWRITEBYTECODE=1 \
  "${IMAGE:-noise-collector:0.1.0}" sh -c 'pip install -q --no-cache-dir pytest pyyaml jsonschema >/dev/null 2>&1; python -m pytest -p no:cacheprovider -q /src/tests/integration/test_laravel_live.py -o testpaths= -rA'
