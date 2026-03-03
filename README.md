# ⚔️ Fight Night Bot — Setup Guide

A Discord bot for managing your AOE4 Fight Night queue. Tracks win streaks, handles multiple tables, and keeps an all-time Hall of Fame.

---

## Quick Setup

### 1. Create a Discord Bot

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
5. Click **Reset Token** and copy your token
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Messages/View Channels`, `Embed Links`, `Mention Everyone`
7. Open the generated URL and invite the bot to your server

### 2. Install & Run

```bash
# Install dependencies
pip install -r requirements.txt

# Set your token as an environment variable (recommended)
export DISCORD_TOKEN="your_token_here"

# Run the bot
python bot.py
```

Or on Windows:
```cmd
set DISCORD_TOKEN=your_token_here
python bot.py
```

### 3. Configure (optional)

Open `bot.py` and edit the config section at the top:

| Variable | Default | Description |
|---|---|---|
| `FIGHT_NIGHT_CHANNEL_ID` | `None` | Lock bot responses to one channel. Set to your channel's ID (right-click channel → Copy ID). If `None`, bot responds wherever commands are used. |
| `SECOND_TABLE_THRESHOLD` | `12` | Queue size that auto-opens Table 2 |
| `WIN_STREAK_TARGET` | `3` | Wins in a row for a HOF entry |
| `ADMIN_ROLE_NAME` | `"Moderator"` | Role name allowed to use `!fn` admin commands |

---

## Commands

| Command | Who | Description |
|---|---|---|
| `!join` | Anyone | Add yourself to the queue |
| `!leave` | Anyone | Remove yourself from the queue |
| `!win @player` | Active players | Report who won the game |
| `!queue` | Anyone | Show tables and queue |
| `!hof` | Anyone | Show all-time Hall of Fame |
| `!fn reset` | Admin | Clear everything and start over |
| `!fn removetable <1\|2>` | Admin | Remove a stalled table |

---

## How It Works

- **First 2 players** to `!join` start **Table 1** automatically
- The **winner stays** as champion, the **next person in queue** becomes the challenger
- If the champion wins **3 in a row**: the bot announces it, logs it to the Hall of Fame, and the next 2 in queue start fresh
- When the **queue hits 12 players**, **Table 2** opens automatically with the next 2 in line
- Hall of Fame is saved to `hall_of_fame.json` and persists between bot restarts

---

## Tips

- To get a channel ID: Enable Developer Mode in Discord settings → right-click your channel → Copy ID
- The `hall_of_fame.json` file is created automatically on first use
- If a game gets stuck, an admin can use `!fn removetable` to clear it

---

## Running 24/7

To keep the bot running, consider hosting it on:
- [Railway.app](https://railway.app) — free tier available, easy deploys
- [Fly.io](https://fly.io)
- A cheap VPS (e.g. DigitalOcean, Hetzner)

For Railway: just set `DISCORD_TOKEN` as an environment variable in the dashboard and point it at this repo.
