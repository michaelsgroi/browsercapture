#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT=""
URL=""
RAW=false
BACKGROUND=false
STATE_FILE="/tmp/browsercapture.state"

usage() {
    cat <<'USAGE'
Usage: browsercapture.sh [url] [options]
       browsercapture.sh finish

Options:
  --output FILE      HAR output path (default: /tmp/browsercapture-<timestamp>.har)
  --raw              Keep all traffic (static assets, telemetry, browser internals)
  --background       Launch browser and return immediately (use 'finish' to stop)
  -h, --help         Show this help

Commands:
  finish             Stop background browser session and save HAR

If no URL is provided, the browser opens to a blank tab.
By default, the HAR is filtered to remove noise. Use --raw to keep everything.
USAGE
    exit 0
}

# Handle 'finish' command
if [[ $# -eq 1 && "$1" == "finish" ]]; then
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "No active browser session found." >&2
        exit 1
    fi

    # Read state file to get signal file path
    SIGNAL_FILE=$(jq -r '.signal_file' "$STATE_FILE" 2>/dev/null)
    if [[ -z "$SIGNAL_FILE" || "$SIGNAL_FILE" == "null" ]]; then
        echo "Invalid state file." >&2
        exit 1
    fi

    # Create signal file to tell browser to stop
    touch "$SIGNAL_FILE"
    echo "Signaling browser to stop..."

    # Wait for process to finish (up to 10 seconds)
    for i in {1..20}; do
        if [[ ! -f "$STATE_FILE" ]]; then
            break
        fi
        sleep 0.5
    done

    exit 0
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)      OUTPUT="$2"; shift 2 ;;
        --raw)         RAW=true; shift ;;
        --background)  BACKGROUND=true; shift ;;
        -h|--help)     usage ;;
        -*)            echo "Unknown option: $1" >&2; usage ;;
        *)             if [[ "$1" == *://* ]]; then
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
fi

if [[ "$BACKGROUND" == true ]]; then
    CAPTURE_ARGS+=(--background)

    # Create state file with session info
    SIGNAL_FILE="/tmp/browsercapture-signal-$$.tmp"
    jq -n --arg signal "$SIGNAL_FILE" --arg output "$OUTPUT" \
        '{signal_file: $signal, output: $output, pid: '$$'}' > "$STATE_FILE"

    if [[ -n "$URL" ]]; then
        echo "Launching browser for: $URL (background mode)"
    else
        echo "Launching browser (blank tab, background mode)"
    fi
    echo "HAR will be saved to: $OUTPUT"
    echo ""
    echo "When done browsing, run: ./browsercapture.sh finish"
    echo ""

    # Run in background
    (
        python3 "$SCRIPT_DIR/browsercapture.py" "${CAPTURE_ARGS[@]}" "$SIGNAL_FILE"

        if [[ "$RAW" == false ]]; then
            RAW_SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
            python3 "$SCRIPT_DIR/browsercapture.py" filter "$OUTPUT" -o "$OUTPUT"
            SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
            echo "HAR saved: $OUTPUT ($SIZE bytes, filtered from $RAW_SIZE)"
        else
            SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
            echo "HAR saved: $OUTPUT ($SIZE bytes, unfiltered)"
        fi

        # Clean up state file
        rm -f "$STATE_FILE" "$SIGNAL_FILE"
    ) &

    exit 0
else
    if [[ -n "$URL" ]]; then
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
fi
