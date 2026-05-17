# BrowserCapture

A standalone tool that launches a browser, records your interactions as a HAR file.

## Quickstart

```bash
# Clone and setup
git clone <this-repo>
cd browsercapture
pip install playwright
playwright install chrome

# Run it
./browsercapture.sh

# Browser opens → navigate around → press Enter when done
# HAR saved to /tmp/browsercapture-<timestamp>.har
```

**That's it!** The HAR file contains all HTTP traffic, filtered to show just the important stuff (APIs, XHR, etc.).

### What's included in this repo

- `browsercapture.sh` - Main shell wrapper
- `browsercapture.py` - Python script using Playwright
- `fetch_leaderboard.py` - Example: fetch live golf leaderboard from ESPN API
- `monitor_leaderboard.py` - Example: monitor leaderboard for changes
- `check_changes.py` - Example: check changes and speak them (macOS)

## How It Works

### Interactive Mode (Default)
1. Run `./browsercapture.sh [url]`
2. Chrome opens with HAR recording enabled via Playwright
3. You do whatever you need to do in the browser (login, navigate, call APIs, etc.)
4. Press Enter in the terminal when done
5. HAR file is saved to `/tmp/browsercapture-<timestamp>.har` (or a path you specify)

### Background Mode (for automation/Claude)
1. Run `./browsercapture.sh --background [url]`
2. Browser opens and script returns immediately
3. You do whatever you need to do in the browser
4. Run `./browsercapture.sh finish` when done
5. HAR file is saved and filtered automatically

## Prerequisites

- Python 3.8+
- Google Chrome installed

### Install Playwright

```bash
pip install playwright
playwright install chromium
```

## Usage

### As a shell script

**Interactive mode** - Browser opens to a blank tab, you navigate wherever you want, press Enter when done:

```bash
./browsercapture.sh
```

Open directly to a URL:

```bash
./browsercapture.sh https://myapp.example.com/dashboard
```

**Background mode** - For use with automation or when running through Claude:

```bash
# Start capture
./browsercapture.sh --background

# Do your browsing...

# When done, finish and save
./browsercapture.sh finish
```

Background mode with a URL:

```bash
./browsercapture.sh --background https://myapp.example.com
# ... browse ...
./browsercapture.sh finish
```

Save to a named file (`.har` extension added automatically):

```bash
./browsercapture.sh my_har_file
```

Open a URL and save to a specific file:

```bash
./browsercapture.sh https://example.com --output session.har
```

Keep all traffic including static assets, telemetry, and browser internals:

```bash
./browsercapture.sh --raw
```

By default the HAR is filtered to remove noise (static assets, analytics, browser internals, OPTIONS preflights, etc.). Use `--raw` to keep everything.

## Common Use Cases

**Reverse engineering APIs**
```bash
# Capture traffic from a web app
./browsercapture.sh https://app.example.com
# Browse around, make API calls, then press Enter
# Inspect the HAR to see endpoints, headers, auth tokens, request/response format
```

**Testing authentication flows**
```bash
./browsercapture.sh --background https://myapp.com/login
# Complete the login flow in the browser
./browsercapture.sh finish
# HAR now contains all the OAuth/SAML/auth dance
```

**Analyzing API performance**
```bash
./browsercapture.sh --raw https://slowsite.com
# The HAR includes timing data for every request
```

**Generating client code**
Use with Claude Code to automatically generate HTTP client code from captured traffic (see MCP section below).

### As an MCP server in Claude Code

You can register BrowserCapture as an MCP server so Claude can launch a browser capture session as a tool call. Add this to your Claude Code settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "browsercapture": {
      "command": "python3",
      "args": ["/absolute/path/to/browsercapture/browsercapture.py", "mcp"]
    }
  }
}
```

This exposes two tools to Claude:

- **`browsercapture`** — launches the browser, records a HAR, and returns the filtered HAR content. Claude can then generate whatever client code you ask for.
- **`filter_har`** — filters an existing HAR file on disk, returning the cleaned content.

Example conversation with Claude after setting up the MCP server:

> **You:** Go to https://myapp.example.com, let me log in, then write me a Kotlin HTTP client for the API calls you see.
>
> **Claude:** *calls browsercapture tool, browser opens, you interact*
>
> **You:** *After browsing, you press Enter in the terminal or tell Claude you're done*
>
> **Claude:** I can see the auth flow and 3 API endpoints. Here's a Kotlin client using OkHttp... *(generates code)*

**Note:** When using browsercapture through Claude Code directly (not as MCP), Claude can use `--background` mode:

> **You:** Launch browsercapture in background mode
>
> **Claude:** *runs `./browsercapture.sh --background`, browser opens*
>
> **You:** *Navigate, login, interact with the site...*
>
> **You:** Done, capture it
>
> **Claude:** *runs `./browsercapture.sh finish`, HAR is saved*

### As a Claude Code slash command

Add a custom slash command by creating `.claude/commands/capture.md` in your project:

```markdown
Launch a browser capture session at $ARGUMENTS, wait for the user to interact, then generate a Kotlin HTTP client from the captured HAR traffic. Use the browsercapture tool at /path/to/browsercapture.
```

Then use it in Claude Code:

```
/project:capture https://myapp.example.com/api
```

## Example Scripts

This repo includes example scripts demonstrating HAR analysis:

### Golf Leaderboard (ESPN API)

**Fetch current leaderboard:**
```bash
python3 fetch_leaderboard.py
# Shows top 10 from current PGA tournament
```

**Monitor for changes:**
```bash
python3 monitor_leaderboard.py --interval 1
# Checks every minute, reports position changes
```

**Check and speak changes (macOS):**
```bash
python3 check_changes.py --wait 1
# Checks twice (1 min apart), uses 'say' command to speak results
```

These scripts show how to:
- Extract API endpoints from captured HAR files
- Parse JSON responses
- Monitor live data sources
- Build simple API clients

Use them as templates for your own API reverse engineering projects!