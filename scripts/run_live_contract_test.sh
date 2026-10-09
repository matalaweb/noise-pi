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
# Give the demo device a calibration chain like a real UMIK-1 setup: a calibrated record with a
# sensitivity value and a serial-specific frequency-response file (SYNTHETIC values), published
# as a new configuration revision.
docker compose exec -T app php artisan tinker --execute '
$d = App\Models\Device::where("uuid", "'"$DEVICE"'")->firstOrFail();
$owner = $d->account->users()->first();
$cal = app(App\Services\Devices\ProvenanceRecords::class)->createCalibration($d, ["channel" => "mic-1", "calibration_state" => "calibrated",
    "reference_method" => "SYNTHETIC live contract test", "reference_level_db" => 94, "reference_frequency_hz" => 1000, "sensitivity_dbfs_at_94db" => -18.0], $owner);
$path = tempnam(sys_get_temp_dir(), "umik");
file_put_contents($path, "\"Sens Factor =0.0dB, AGain =18dB, SERNO: SIM-0001\"\n10.0\t-1.5\n100.0\t-0.2\n1000.0\t0.0\n10000.0\t0.8\n20000.0\t-1.2\n");
app(App\Services\Storage\AttachmentStore::class)->store($cal, $path, "SIM-0001_90deg.txt", "text/plain", "frequency_response", $owner);
$svc = app(App\Services\Devices\DeviceConfigurationService::class);
$settings = $svc->settingsFromDocument($d->latestConfiguration()->document);
$settings["channels"][0]["calibration_id"] = $cal->uuid;
$svc->publish($d, $settings, $owner);
echo "calibration chain published\n";' | grep -q "calibration chain published" || { echo "could not publish the calibration chain"; exit 1; }
docker run --rm --user root --network "$NET" -v "$ROOT:/src:ro" -w /tmp \
  -e NOISE_LARAVEL_URL=http://app:8080 -e NOISE_LARAVEL_TOKEN="$TOKEN" -e NOISE_LARAVEL_STORAGE_HOSTS=s3 -e NOISE_LARAVEL_EXPECT_CALIBRATION_FILE=1 \
  -e PYTHONPATH=/src/src:/src/tests -e PYTHONDONTWRITEBYTECODE=1 \
  noise-collector:0.1.0 sh -c 'pip install -q --no-cache-dir pytest pyyaml jsonschema >/dev/null 2>&1; python -m pytest -p no:cacheprovider -q /src/tests/integration/test_laravel_live.py -o testpaths= -rA'
