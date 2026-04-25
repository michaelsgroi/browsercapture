#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT=""
URL=""
RAW=false

usage() {
    cat <<'USAGE'
Usage: browsercapture.sh [url] [options]

Options:
  --output FILE    HAR output path (default: /tmp/browsercapture-<timestamp>.har)
  --raw            Keep all traffic (static assets, telemetry, browser internals)
  -h, --help       Show this help

If no URL is provided, the browser opens to a blank tab.
By default, the HAR is filtered to remove noise. Use --raw to keep everything.
USAGE
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)   OUTPUT="$2"; shift 2 ;;
        --raw)      RAW=true; shift ;;
        -h|--help)  usage ;;
        -*)         echo "Unknown option: $1" >&2; usage ;;
        *)          if [[ "$1" == *://* ]]; then
                        URL="$1"
                    else
                        OUTPUT="$1"
                        [[ "$OUTPUT" != *.har ]] && OUTPUT="$OUTPUT.har"
                    fi
                    shift ;;
    esac
done

if [[ -z "$OUTPUT" ]]; then
    OUTPUT="/tmp/browsercapture-$(date -u +%Y%m%dT%H%M%SZ).har"
fi

CAPTURE_ARGS=(capture --output "$OUTPUT")
if [[ -n "$URL" ]]; then
    CAPTURE_ARGS+=(--url "$URL")
    echo "Launching browser for: $URL"
else
    echo "Launching browser (blank tab)"
fi
echo "HAR will be saved to: $OUTPUT"
echo ""

python3 "$SCRIPT_DIR/browsercapture.py" "${CAPTURE_ARGS[@]}"

if [[ "$RAW" == false ]]; then
    RAW_SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
    python3 "$SCRIPT_DIR/browsercapture.py" filter "$OUTPUT" -o "$OUTPUT"
    SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
    echo "HAR saved: $OUTPUT ($SIZE bytes, filtered from $RAW_SIZE)"
else
    SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
    echo "HAR saved: $OUTPUT ($SIZE bytes, unfiltered)"
fi
