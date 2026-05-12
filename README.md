# OpenCode Telegram Bridge

A Telegram bot that bridges messages to [OpenCode](https://opencode.ai), enabling you to chat with OpenCode from your phone via Telegram.

Uses the free `minimax-m2.5-free` model — no API keys required.

## Architecture

```
Telegram → bot.py → OpenCode serve (localhost:4096)
              ↓
         sessions.db (SQLite — per-user session state)
         block_patterns.py (hardline/dangerous command detection)
         formatter.py (Markdown → Telegram MDV2 + chunking)
```

## Prerequisites

- Python 3.14+
- OpenCode installed (`~/.opencode/bin/opencode`)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (send `/start` to [@userinfobot](https://t.me/userinfobot))

## Setup

### 1. Clone and create virtual environment

```bash
git clone git@github.com:xekuted/opencode-telegram-bridge.git
cd opencode-telegram-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ALLOWED_USERS=your_user_id_here
OPENCODE_HOST=127.0.0.1
OPENCODE_PORT=4096
OPENCODE_DEFAULT_MODEL=minimax-m2.5-free
```

### 3. Start OpenCode server

```bash
opencode serve --port 4096
```

Or for auto-restart, install the systemd service (see below).

### 4. Start the bot

```bash
source venv/bin/activate
python -m bot
```

Or use the convenience script:

```bash
bash start.sh
```

## Commands

| Command | Description |
|---------|-------------|
| `/new`, `/reset` | Start a fresh session (deletes old session) |
| `/abort`, `/stop` | Abort the current running request |
| `/share` | Generate a shareable link for the current session |
| `/model` | List available models |
| `/model <name>` | Switch to a different model |
| `/status` | Show current session info |
| `/help` | Show help |

Send any other message to chat with OpenCode.

## Systemd Services (recommended)

For auto-start on boot and crash recovery, install the systemd units:

### 1. Install the service installer

```bash
python systemd.py
systemctl --user daemon-reload
systemctl --user enable --now opencode-serve.service
systemctl --user enable --now opencode-bridge-telegram.service
```

### 2. Uninstall

```bash
systemctl --user stop opencode-bridge-telegram.service opencode-serve.service
systemctl --user disable opencode-bridge-telegram.service opencode-bridge-telegram.target
rm ~/.config/systemd/user/opencode-serve.service
rm ~/.config/systemd/user/opencode-bridge-telegram.service
rm ~/.config/systemd/user/opencode-bridge.target
systemctl --user daemon-reload
```

### 3. View logs

```bash
journalctl --user -u opencode-bridge-telegram.service -f
journalctl --user -u opencode-serve.service -f
```

## Security

### Block patterns

The bridge runs AI response text through a hardline/dangerous command filter (copied from Hermes Agent's approval system) before sending to Telegram. This is a safety net — the actual sandboxing depends on OpenCode's configuration.

Hardline blocks include: recursive delete of root/home, filesystem format, dd to raw block devices, fork bombs, system shutdown/reboot.

Dangerous commands (require user approval): recursive rm, chmod 777, chown to root, SQL DROP without WHERE, git reset --hard, curl|sh, etc.

### Per-user isolation

Each Telegram user ID gets its own OpenCode session. The bridge maintains a SQLite database mapping user IDs to session IDs.

## File overview

| File | Purpose |
|------|---------|
| `bot.py` | Telegram handlers, command routing, message pipeline |
| `opencode_client.py` | Async httpx client for OpenCode REST API |
| `session_store.py` | SQLite session store (user → opencode session) |
| `formatter.py` | Markdown to Telegram MDV2, table rendering, UTF-16 chunking |
| `block_patterns.py` | Hardline/dangerous command pattern detection |
| `systemd.py` | Service installer for systemd user units |
| `start.sh` | Quick launcher script |
| `requirements.txt` | Python dependencies |

## Troubleshooting

### Bot not responding
```bash
# Check services are running
systemctl --user status opencode-serve.service
systemctl --user status opencode-bridge-telegram.service

# Check OpenCode is listening
curl http://127.0.0.1:4096/global/health

# View bot logs
journalctl --user -u opencode-bridge-telegram.service -f
```

### Permission denied on startup
```bash
chmod +x start.sh
```

### Change allowed users
Edit `TELEGRAM_ALLOWED_USERS` in `.env` (comma-separated user IDs). Requires bot restart to take effect.