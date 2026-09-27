#!/usr/bin/env bash
# Refresh the bundled Google discovery documents (the mock's source of truth).
set -euo pipefail
dir="$(cd "$(dirname "$0")/.." && pwd)/src/gmail_mock/discovery"
curl -fsSL 'https://gmail.googleapis.com/$discovery/rest?version=v1' | python3 -m json.tool --indent 1 > "$dir/gmail_v1.json"
curl -fsSL 'https://people.googleapis.com/$discovery/rest?version=v1' | python3 -m json.tool --indent 1 > "$dir/people_v1.json"
python3 - "$dir" <<'PY'
import json, sys
for name in ("gmail_v1", "people_v1"):
    doc = json.load(open(f"{sys.argv[1]}/{name}.json"))
    print(f"{name}: revision {doc['revision']}")
PY
