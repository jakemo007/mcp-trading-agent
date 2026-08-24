# 🚀 MCP Trading Agent — Setup Instructions

> All paths and commands are tailored to your machine. Copy-paste ready.

---

## You Have 3 Options

| Method | Best For | Status on Your PC |
|--------|----------|-------------------|
| **Option A** — Claude Code (Terminal) | Fastest, works right now | ✅ `claude` CLI installed |
| **Option B** — Claude Desktop (GUI) | Best UX, chat interface | ⚠️ Not installed yet |
| **Option C** — MCP Inspector (Testing) | Debug & test tools | ✅ `npx` available |

---

## Option A — Claude Code (Terminal) ⭐ Recommended

This is the fastest path — you already have `claude` installed at `C:\Program Files\nodejs\claude.ps1`.

### Step 1: Register the MCP server

Open PowerShell and run:

```powershell
claude mcp add trading-agent -- C:\Users\gunuj\miniconda3\python.exe C:\Github\ai-company\mcp-trading-agent\server.py
```

### Step 2: Verify it's registered

```powershell
claude mcp list
```

You should see `trading-agent` in the list.

### Step 3: Set the System Prompt

Create a file called `.claude` in your project root with the system prompt:

```powershell
# Copy the system prompt content
python -c "import sys; sys.path.insert(0, 'C:\\Github\\ai-company\\mcp-trading-agent'); from system_prompt import SYSTEM_PROMPT; print(SYSTEM_PROMPT)" > C:\Github\ai-company\.claude
```

Or simply start Claude Code with an inline system instruction:

```powershell
cd C:\Github\ai-company\mcp-trading-agent
claude
```

Then in the Claude Code session, type:

```
intraday NIFTY
```

Claude will automatically call all 3 tools and produce the Daily Market View! 🎯

### Step 4: (Alternative) Add system prompt via CLAUDE.md

Create a `CLAUDE.md` file in your project folder:

```powershell
python -c "import sys; sys.path.insert(0, 'C:\\Github\\ai-company\\mcp-trading-agent'); from system_prompt import SYSTEM_PROMPT; print(SYSTEM_PROMPT)" > C:\Github\ai-company\mcp-trading-agent\CLAUDE.md
```

Now every time you open `claude` from that directory, it auto-loads the system prompt.

---

## Option B — Claude Desktop (GUI)

### Step 1: Install Claude Desktop

Download from: **https://claude.ai/download**

Install it, then sign in with your Anthropic account.

### Step 2: Enable Developer Mode

1. Open Claude Desktop
2. Click the **hamburger menu** (☰) → **Settings**
3. Go to **Developer** tab
4. Toggle **"Enable MCP Servers"** ON

### Step 3: Edit the Config File

Open PowerShell and run:

```powershell
# Create the config directory if it doesn't exist
New-Item -ItemType Directory -Force -Path "C:\Users\gunuj\AppData\Roaming\Claude"

# Create/edit the config file
notepad "C:\Users\gunuj\AppData\Roaming\Claude\claude_desktop_config.json"
```

Paste this exact JSON:

```json
{
  "mcpServers": {
    "trading-agent": {
      "command": "C:\\Users\\gunuj\\miniconda3\\python.exe",
      "args": [
        "C:\\Github\\ai-company\\mcp-trading-agent\\server.py"
      ]
    }
  }
}
```

> [!WARNING]
> Use double backslashes `\\` in all paths. Save and close Notepad.

### Step 4: Restart Claude Desktop

Fully quit Claude Desktop (right-click system tray icon → Quit), then reopen it.

You should see a **🔧 tools icon** in the chat input area — click it to verify `trading-agent` is connected and the 4 tools are visible.

### Step 5: Set the System Prompt

In Claude Desktop, you set the system prompt as a **Project** instruction:

1. Create a new **Project** (left sidebar → "Projects" → "+")
2. Name it "Trading Agent"
3. In the **Project Instructions** box, paste the entire system prompt from:
   - [system_prompt.py](file:///c:/Github/ai-company/mcp-trading-agent/system_prompt.py) — copy everything between the triple-quotes

### Step 6: Use It!

In the chat, type:

```
intraday NIFTY
```

Claude will automatically fire all 3 tools in sequence and produce the full Daily Market View report.

---

## Option C — MCP Inspector (Testing & Debugging)

Use this to test individual tools before connecting to Claude.

### Step 1: Start the server in HTTP mode

```powershell
cd C:\Github\ai-company\mcp-trading-agent
$env:MCP_TRANSPORT="streamable-http"
C:\Users\gunuj\miniconda3\python.exe server.py
```

You'll see:
```
Starting ICT-SMC Trading Agent v1.0.0  (transport=streamable-http)
```

### Step 2: Open the Inspector (new terminal)

```powershell
npx -y @modelcontextprotocol/inspector
```

### Step 3: Connect

In the Inspector web UI that opens:
1. Set transport to **Streamable HTTP**
2. Enter URL: `http://localhost:8000/mcp`
3. Click **Connect**

You'll see all 4 tools listed. Click any tool, fill in parameters, and hit **Run** to test.

---

## Quick Reference — Commands You'll Use

```powershell
# ─── Claude Code (Terminal) ───────────────────────────
claude mcp add trading-agent -- C:\Users\gunuj\miniconda3\python.exe C:\Github\ai-company\mcp-trading-agent\server.py
claude mcp list
claude mcp remove trading-agent   # if you need to re-add

# ─── Start the MCP Server manually ───────────────────
cd C:\Github\ai-company\mcp-trading-agent

# stdio mode (for Claude Desktop/Code — usually auto-started)
C:\Users\gunuj\miniconda3\python.exe server.py

# HTTP mode (for Inspector/testing)
$env:MCP_TRANSPORT="streamable-http"
C:\Users\gunuj\miniconda3\python.exe server.py

# ─── MCP Inspector ───────────────────────────────────
npx -y @modelcontextprotocol/inspector
```

## Example Prompts to Try

Once connected (via any method), try these:

| Command | What Happens |
|---------|-------------|
| `intraday NIFTY` | Full Daily Market View with BSL/SSL + news sentiment |
| `view AAPL` | Same analysis for Apple stock |
| `intraday BTC` | Bitcoin analysis with crypto news |
| `view GOLD` | Gold futures analysis |
| `intraday BANKNIFTY` | Bank NIFTY with liquidity pools |

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Tool not found" in Claude | Run `claude mcp list` to verify registration |
| "No data returned" | Check ticker spelling; try `AAPL` or `NIFTY` |
| Server crashes on start | Run `pip install -r requirements.txt` again |
| Claude Desktop doesn't show tools | Restart Claude Desktop fully (quit from system tray) |
| Inspector can't connect | Make sure server is running in HTTP mode (`MCP_TRANSPORT=streamable-http`) |
