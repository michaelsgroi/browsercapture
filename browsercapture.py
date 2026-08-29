#!/usr/bin/env python3
"""BrowserCapture — launch a browser, record traffic as HAR, optionally serve via MCP."""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
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

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-csrftoken",
    "x-xsrf-token",
}

AUTH_WARNING = (
    "Contains live browser credentials and request data. Do not share or commit this file."
)


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


def filter_har(har, truncate_bodies=False):
    entries = har.get("log", {}).get("entries", [])
    filtered = []
    for entry in entries:
        if _is_noise(entry):
            continue
        if truncate_bodies:
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


def _safe_headers(headers):
    """Return headers that are safe for the normal, shareable HAR."""
    return [
        {"name": name, "value": value}
        for name, value in headers.items()
        if name.lower() not in SENSITIVE_HEADER_NAMES
    ]


def _har_cookies(headers, header_name):
    """Convert Cookie/Set-Cookie headers into HAR cookie objects."""
    cookies = []
    for header in headers:
        if header["name"].lower() != header_name.lower():
            continue
        parsed = SimpleCookie()
        parsed.load(header["value"])
        for morsel in parsed.values():
            cookies.append({"name": morsel.key, "value": morsel.value})
    return cookies


def _header_array(headers):
    """Normalize Playwright's NameValue dictionaries for JSON output."""
    return [{"name": header["name"], "value": header["value"]} for header in headers]


def _complete_headers(message):
    """Get wire headers, preserving duplicates when Playwright supports it."""
    try:
        return _header_array(message.headers_array())
    except Exception:
        return [
            {"name": name, "value": value}
            for name, value in message.all_headers().items()
        ]


def _default_auth_output(har_output):
    stem, extension = os.path.splitext(har_output)
    if extension.lower() == ".har":
        return f"{stem}.auth.json"
    return f"{har_output}.auth.json"


def _write_private_json(path, value):
    """Write credential-bearing JSON with owner-only permissions."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as auth_file:
            fd = None
            json.dump(value, auth_file, indent=2)
            auth_file.write("\n")
    finally:
        if fd is not None:
            os.close(fd)


def replay_auth_request(auth_path, request_id):
    """Replay one captured request without displaying its credential values."""
    with open(auth_path) as auth_file:
        auth_data = json.load(auth_file)

    captured = next(
        (item for item in auth_data.get("requests", []) if item.get("requestId") == request_id),
        None,
    )
    if captured is None:
        raise ValueError(f"request ID {request_id} not found in credential file")

    excluded = {"content-length", "host", "connection", "transfer-encoding"}
    headers = {
        item["name"]: item["value"]
        for item in captured.get("headers", [])
        if item["name"].lower() not in excluded and not item["name"].startswith(":")
    }
    post_data = captured.get("postData")
    data = post_data.encode("utf-8") if post_data is not None else None
    replay_request = urllib.request.Request(
        captured["url"],
        data=data,
        headers=headers,
        method=captured["method"],
    )
    try:
        with urllib.request.urlopen(replay_request) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


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


class _TargetClosedFilter(logging.Filter):
    def filter(self, record):
        return "TargetClosedError" not in record.getMessage()


logging.getLogger("asyncio").addFilter(_TargetClosedFilter())


def capture(url=None, output=None, signal_file=None, capture_auth=True, auth_output=None):
    if output is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = f"/tmp/{ts}.har"
    if capture_auth:
        auth_output = auth_output or _default_auth_output(output)
        if os.path.abspath(auth_output) == os.path.abspath(output):
            raise ValueError("auth output must be different from HAR output")

    stop_event = threading.Event()

    if signal_file:
        signal_thread = threading.Thread(
            target=_wait_for_signal_file, args=(signal_file, stop_event), daemon=True
        )
        signal_thread.start()
    else:
        enter_thread = threading.Thread(target=_wait_for_enter, args=(stop_event,), daemon=True)
        enter_thread.start()

    p = sync_playwright().start()
    user_data_dir = tempfile.mkdtemp(prefix="browsercapture_")

    # Don't use record_har_* — context.close() hangs waiting for Chrome to flush
    # the HAR, which never completes reliably. Capture via event listeners instead.
    context = p.chromium.launch_persistent_context(
        user_data_dir,
        headless=False,
        channel="chrome",
        viewport={"width": 1400, "height": 900},
    )

    har_entries = []
    auth_requests = []
    har_lock = threading.Lock()

    def on_requestfinished(request):
        try:
            started = datetime.now(timezone.utc).isoformat()
            response = request.response()
            if response is None:
                return
            try:
                body_bytes = response.body()
                body_text = body_bytes.decode("utf-8", errors="replace")
                body_size = len(body_bytes)
            except Exception:
                body_text = ""
                body_size = -1

            post_data = None
            if request.post_data:
                post_data = {
                    "mimeType": request.headers.get("content-type", ""),
                    "text": request.post_data,
                }

            request_headers = request.headers
            response_headers = response.headers

            entry = {
                "startedDateTime": started,
                "time": -1,
                "request": {
                    "method": request.method,
                    "url": request.url,
                    "httpVersion": "HTTP/1.1",
                    "headers": _complete_headers(request),
                    "queryString": [],
                    "cookies": _har_cookies(_complete_headers(request), "cookie"),
                    "headersSize": -1,
                    "bodySize": len(request.post_data.encode()) if request.post_data else 0,
                    **({"postData": post_data} if post_data else {}),
                },
                "response": {
                    "status": response.status,
                    "statusText": response.status_text,
                    "httpVersion": "HTTP/1.1",
                    "headers": _complete_headers(response),
                    "cookies": _har_cookies(_complete_headers(response), "set-cookie"),
                    "content": {
                        "size": body_size,
                        "mimeType": response.headers.get("content-type", ""),
                        **({"text": body_text} if body_text else {}),
                    },
                    "redirectURL": response.headers.get("location", ""),
                    "headersSize": -1,
                    "bodySize": body_size,
                },
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
            }

            auth_request = None
            if capture_auth:
                auth_request = {
                    "requestId": None,
                    "startedDateTime": started,
                    "method": request.method,
                    "url": request.url,
                    "headers": _complete_headers(request),
                    **({"postData": request.post_data} if request.post_data else {}),
                }
            with har_lock:
                request_id = len(har_entries)
                entry["_browsercaptureRequestId"] = request_id
                if auth_request is not None:
                    auth_request["requestId"] = request_id
                    auth_requests.append(auth_request)
                har_entries.append(entry)
        except Exception:
            pass

    context.on("requestfinished", on_requestfinished)

    page = context.pages[0] if context.pages else context.new_page()

    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})

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

    # Must pump the Playwright event loop while waiting, otherwise requestfinished
    # events never fire during browsing (they queue up and only flush on close).
    while not stop_event.is_set() and not browser_closed.is_set():
        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    with har_lock:
        snapshot = list(har_entries)

    har = {
        "log": {
            "version": "1.2",
            "creator": {"name": "browsercapture", "version": "0.1.0"},
            "comment": "This HAR may contain live browser credentials.",
            "entries": snapshot,
        }
    }
    with open(output, "w") as f:
        json.dump(har, f, indent=2)

    if capture_auth:
        storage_state = context.storage_state()
        with har_lock:
            auth_snapshot = list(auth_requests)
        auth_data = {
            "version": 1,
            "warning": AUTH_WARNING,
            "harFile": os.path.abspath(output),
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "storageState": storage_state,
            "requests": auth_snapshot,
        }
        _write_private_json(auth_output, auth_data)

    if not browser_closed.is_set():
        try:
            context.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass
    else:
        try:
            p.stop()
        except Exception:
            pass

    print(f"HAR saved: {output} ({len(snapshot)} entries)", file=sys.stderr)
    if capture_auth:
        print(
            f"Credentials saved: {auth_output} (mode 0600; contains live credentials)",
            file=sys.stderr,
        )
    return output


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
    output = args.get("output", f"/tmp/{ts}.har")

    cmd = [sys.executable, __file__, "capture", "--output", output]
    if "url" in args:
        cmd += ["--url", args["url"]]
    if "auth_output" in args:
        cmd += ["--auth-output", args["auth_output"]]

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode != 0:
        return _error_response(req["id"], -32000, f"Capture failed with exit code {result.returncode}")

    with open(output) as f:
        har = json.load(f)
    har = filter_har(har, truncate_bodies=True)

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
    har = filter_har(har, truncate_bodies=True)

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
                    "auth_output": {
                        "type": "string",
                        "description": "Credential file path (optional)",
                    },
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
    cap.add_argument("--auth-output", default=None, help="Credential JSON output path")
    cap.add_argument("signal_file", nargs="?", default=None, help="Signal file path for background mode")

    filt = sub.add_parser("filter", help="Filter noise from a HAR file")
    filt.add_argument("input", help="HAR file to filter")
    filt.add_argument("--output", "-o", default=None, help="Output file (default: stdout)")

    replay = sub.add_parser("replay", help="Replay one request from a protected auth file")
    replay.add_argument("auth_file", help="Credential JSON created alongside a capture")
    replay.add_argument("--request-id", required=True, type=int, help="HAR request ID to replay")

    sub.add_parser("mcp", help="Run as MCP server")

    args = parser.parse_args()

    if args.command == "capture" or args.command is None:
        result = capture(
            url=getattr(args, "url", None),
            output=getattr(args, "output", None),
            signal_file=getattr(args, "signal_file", None),
            capture_auth=True,
            auth_output=getattr(args, "auth_output", None),
        )
        if result is None:
            sys.exit(1)
    elif args.command == "filter":
        filter_har_file(args.input, args.output)
    elif args.command == "replay":
        try:
            status = replay_auth_request(args.auth_file, args.request_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
        print(f"Replay status: {status}")
    elif args.command == "mcp":
        serve_mcp()


if __name__ == "__main__":
    main()
