#!/usr/bin/env python3
"""BrowserCapture — launch a browser, record traffic as HAR, optionally serve via MCP."""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# --- HAR filtering ---

STATIC_EXTENSIONS = re.compile(
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map|webp|avif)(\?|#|$)",
    re.IGNORECASE,
)

IGNORED_SCHEMES = ("chrome-extension://", "devtools://", "data:", "blob:")

ANALYTICS_DOMAINS = {
    "google-analytics.com",
    "googletagmanager.com",
    "analytics.google.com",
    "segment.io",
    "cdn.segment.com",
    "api.segment.io",
    "mixpanel.com",
    "hotjar.com",
    "fullstory.com",
    "sentry.io",
    "browser-intake-datadoghq.com",
    "rum.browser-intake-datadoghq.com",
    "newrelic.com",
    "bam.nr-data.net",
    "js-agent.newrelic.com",
}

MAX_BODY_SIZE = 8000


def _is_noise(entry):
    url = entry.get("request", {}).get("url", "")

    for scheme in IGNORED_SCHEMES:
        if url.startswith(scheme):
            return True

    if STATIC_EXTENSIONS.search(url):
        return True

    host = urlparse(url).hostname or ""
    for domain in ANALYTICS_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return True

    if entry.get("request", {}).get("method", "") == "OPTIONS":
        return True

    status = entry.get("response", {}).get("status", 0)
    body_size = entry.get("response", {}).get("content", {}).get("size", 0)
    if status == 204 and (body_size or 0) == 0:
        return True

    return False


def _truncate(text, max_size=MAX_BODY_SIZE):
    if len(text) > max_size:
        return text[:max_size] + f"\n... [truncated, {len(text)} bytes total]"
    return text


def filter_har(har):
    entries = har.get("log", {}).get("entries", [])
    filtered = []
    for entry in entries:
        if _is_noise(entry):
            continue
        if "response" in entry and "content" in entry["response"]:
            content = entry["response"]["content"]
            if "text" in content:
                content["text"] = _truncate(content["text"])
        if "request" in entry and "postData" in entry["request"]:
            pd = entry["request"]["postData"]
            if "text" in pd:
                pd["text"] = _truncate(pd["text"])
        filtered.append(entry)

    har["log"]["entries"] = filtered
    return har


def filter_har_file(input_path, output_path=None):
    with open(input_path) as f:
        har = json.load(f)

    original_count = len(har.get("log", {}).get("entries", []))
    har = filter_har(har)
    filtered_count = len(har["log"]["entries"])

    print(f"Filtered: {original_count} -> {filtered_count} entries", file=sys.stderr)

    output = json.dumps(har, indent=2)
    if output_path:
        with open(output_path, "w") as f:
            f.write(output)
    else:
        print(output)


# --- Browser capture ---

def _wait_for_enter(event):
    try:
        input()
    except EOFError:
        pass
    event.set()


def _wait_for_signal_file(signal_file, event, check_interval=0.5):
    """Poll for signal file existence."""
    while not event.is_set():
        if os.path.exists(signal_file):
            event.set()
            return
        threading.Event().wait(check_interval)


def capture(url=None, output=None, signal_file=None):
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = f"/tmp/browsercapture-{ts}.har"

    stop_event = threading.Event()

    if signal_file:
        # Background mode: poll for signal file
        signal_thread = threading.Thread(
            target=_wait_for_signal_file, args=(signal_file, stop_event), daemon=True
        )
        signal_thread.start()
    else:
        # Interactive mode: wait for Enter
        enter_thread = threading.Thread(target=_wait_for_enter, args=(stop_event,), daemon=True)
        enter_thread.start()

    p = sync_playwright().start()
    user_data_dir = tempfile.mkdtemp(prefix="browsercapture_")

    context = p.chromium.launch_persistent_context(
        user_data_dir,
        headless=False,
        channel="chrome",
        viewport={"width": 1400, "height": 900},
        record_har_path=output,
    )

    page = context.pages[0] if context.pages else context.new_page()
    if url:
        page.goto(url, wait_until="domcontentloaded")

    browser_closed = threading.Event()

    def on_close():
        browser_closed.set()
        stop_event.set()

    context.on("close", lambda _: on_close())

    if url:
        print(f"Browser open at: {url}", file=sys.stderr)
    else:
        print("Browser open. Navigate wherever you like.", file=sys.stderr)

    if signal_file:
        print(f"Waiting for finish signal...", file=sys.stderr)
    else:
        print("Press Enter here when done.", file=sys.stderr)

    stop_event.wait()

    if not browser_closed.is_set():
        context.close()
        p.stop()
        print(f"HAR saved: {output}", file=sys.stderr)
        return output
    else:
        p.stop()
        print("Browser was closed directly — HAR file was not saved.", file=sys.stderr)
        if not signal_file:
            print("Next time, press Enter here instead of closing the browser.", file=sys.stderr)
        return None


# --- MCP server ---

def _read_jsonrpc():
    headers = {}
    while True:
        line = sys.stdin.readline()
        if not line or line.strip() == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    return json.loads(sys.stdin.read(length))


def _write_jsonrpc(msg):
    body = json.dumps(msg)
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n{body}")
    sys.stdout.flush()


def _error_response(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _mcp_do_capture(req, args):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.get("output", f"/tmp/browsercapture-{ts}.har")

    cmd = [sys.executable, __file__, "capture", "--output", output]
    if "url" in args:
        cmd += ["--url", args["url"]]

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode != 0:
        return _error_response(req["id"], -32000, f"Capture failed with exit code {result.returncode}")

    with open(output) as f:
        har = json.load(f)
    har = filter_har(har)

    return {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {"content": [{"type": "text", "text": json.dumps(har, indent=2)}]},
    }


def _mcp_do_filter(req, args):
    path = args["path"]
    if not os.path.exists(path):
        return _error_response(req["id"], -32602, f"File not found: {path}")

    with open(path) as f:
        har = json.load(f)
    har = filter_har(har)

    return {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {"content": [{"type": "text", "text": json.dumps(har, indent=2)}]},
    }


def serve_mcp():
    tools = [
        {
            "name": "browsercapture",
            "description": "Launch a browser, record all HTTP traffic as a HAR file while the user interacts, then return the filtered HAR content. The user must press Enter in the terminal when done. If no URL is provided, opens a blank tab.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open (optional, blank tab if omitted)"},
                    "output": {"type": "string", "description": "HAR output file path (optional)"},
                },
            },
        },
        {
            "name": "filter_har",
            "description": "Filter an existing HAR file, removing static assets, telemetry, and noise. Returns the cleaned HAR content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the HAR file to filter"},
                },
                "required": ["path"],
            },
        },
    ]

    while True:
        msg = _read_jsonrpc()
        if msg is None:
            break

        method = msg.get("method", "")

        if method == "initialize":
            _write_jsonrpc({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "browsercapture", "version": "0.1.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _write_jsonrpc({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
        elif method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if tool_name == "browsercapture":
                _write_jsonrpc(_mcp_do_capture(msg, args))
            elif tool_name == "filter_har":
                _write_jsonrpc(_mcp_do_filter(msg, args))
            else:
                _write_jsonrpc(_error_response(msg["id"], -32602, f"Unknown tool: {tool_name}"))
        elif msg.get("id") is not None:
            _write_jsonrpc(_error_response(msg["id"], -32601, f"Unknown method: {method}"))


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="BrowserCapture")
    sub = parser.add_subparsers(dest="command")

    cap = sub.add_parser("capture", help="Launch browser and record HAR")
    cap.add_argument("--url", default=None, help="URL to open (default: blank tab)")
    cap.add_argument("--output", default=None, help="HAR output path")
    cap.add_argument("--background", action="store_true", help="Background mode (poll for signal file)")
    cap.add_argument("signal_file", nargs="?", default=None, help="Signal file path for background mode")

    filt = sub.add_parser("filter", help="Filter noise from a HAR file")
    filt.add_argument("input", help="HAR file to filter")
    filt.add_argument("--output", "-o", default=None, help="Output file (default: stdout)")

    sub.add_parser("mcp", help="Run as MCP server")

    args = parser.parse_args()

    if args.command == "capture" or args.command is None:
        result = capture(
            url=getattr(args, "url", None),
            output=getattr(args, "output", None),
            signal_file=getattr(args, "signal_file", None),
        )
        if result is None:
            sys.exit(1)
    elif args.command == "filter":
        filter_har_file(args.input, args.output)
    elif args.command == "mcp":
        serve_mcp()


if __name__ == "__main__":
    main()
