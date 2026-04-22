# BrowserCapture

A standalone tool that launches a browser, records your interactions as a HAR file.

## How It Works

1. Run `./browsercapture.sh [url]`
2. Chrome opens with HAR recording enabled via Playwright
3. You do whatever you need to do in the browser (login, navigate, call APIs, etc.)
4. Press Enter in the terminal when done
5. HAR file is saved to `/tmp/browsercapture-<timestamp>.har` (or a path you specify)

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

Browser opens to a blank tab, you navigate wherever you want, press Enter when done:

```bash
./browsercapture.sh
```

Open directly to a URL:

```bash
./browsercapture.sh https://myapp.example.com/dashboard
```

Save to a specific file:

```bash
./browsercapture.sh https://example.com --output session.har
```

Keep all traffic including static assets, telemetry, and browser internals:

```bash
./browsercapture.sh --raw
```

By default the HAR is filtered to remove noise (static assets, analytics, browser internals, OPTIONS preflights, etc.). Use `--raw` to keep everything.

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
> **Claude:** *calls browsercapture tool, browser opens, you interact, press Enter*
>
> **Claude:** I can see the auth flow and 3 API endpoints. Here's a Kotlin client using OkHttp... *(generates code)*

### As a Claude Code slash command

Add a custom slash command by creating `.claude/commands/capture.md` in your project:

```markdown
Launch a browser capture session at $ARGUMENTS, wait for the user to interact, then generate a Kotlin HTTP client from the captured HAR traffic. Use the browsercapture tool at /path/to/browsercapture.
```

Then use it in Claude Code:

```
/project:capture https://myapp.example.com/api
```