#!/usr/bin/env bash
# Upload a recording to a running service, wait for the transcript and print it.
#
# Usage: scripts/smoke_test.sh [base-url] [file]
# Needs curl and python3 (used only to read JSON).
set -euo pipefail

base_url="${1:-http://localhost:8000}"
file="${2:-tests/data/jfk.mp3}"

json_field() { python3 -c "import json, sys; print(json.load(sys.stdin)['$1'])"; }

job_path=$(
  curl --fail --silent --show-error --form "file=@${file}" \
    --dump-header - --output /dev/null "${base_url}/api/transcriptions" |
    awk 'tolower($1) == "location:" { print $2 }' | tr -d '\r'
)
echo "Accepted: ${base_url}${job_path}" >&2

for _ in $(seq 1 600); do
  job=$(curl --fail --silent --show-error "${base_url}${job_path}")
  case "$(printf '%s' "$job" | json_field status)" in
    completed)
      printf '%s' "$job" | json_field text
      exit 0
      ;;
    failed)
      echo "Transcription failed: $(printf '%s' "$job" | json_field error)" >&2
      exit 1
      ;;
  esac
  sleep 1
done

echo "Timed out waiting for the transcript" >&2
exit 1
