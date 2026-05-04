# Refferal-bot

Shopify site checker and card testing tool — available as a CLI and a Telegram bot.

## Setup

```bash
pip install -r requirements.txt
```

## CLI Usage

```bash
python Test.py
```

## Telegram Bot

### Run locally

```bash
export BOT_TOKEN="your-telegram-bot-token"
export CHANNEL_ID="-100xxxxxxxxxx"
python bot.py
```

### Bot Commands

| Command | Description |
|---|---|
| `/start` | Main menu with inline buttons |
| `/chk cc\|mm\|yy\|cvv site_url` | Check a single card against a site |
| `/mass site_url` | Mass check — reply to a .txt file of cards |
| `/site url` | Check if a Shopify site is alive |
| `/msite` | Mass site check — reply to a .txt file of sites |
| `/bin 123456` | BIN lookup |
| `/help` | Show command list |

### Deploy on Render

1. Push this repo to GitHub.
2. Go to [Render Dashboard](https://dashboard.render.com/) → **New → Blueprint**.
3. Connect this repo and Render will read `render.yaml`.
4. Set the environment variables:
   - `BOT_TOKEN` — your Telegram bot token
   - `CHANNEL_ID` — your Telegram channel ID (e.g. `-1003345433105`)
5. Deploy!

Or manually: **New → Background Worker → Python** → set build command `pip install -r requirements.txt`, start command `python bot.py`, and add the env vars above.

## Project Structure

| File | Description |
|---|---|
| `bot.py` | Telegram bot with all checker/site commands and channel posting |
| `Test.py` | CLI entry-point with checker and site menus |
| `api.py` | Gateway API helpers (`process_card`, `parse_cc_string`, etc.) |
| `sites.txt` | Default Shopify site list |
| `requirements.txt` | Python dependencies |
| `render.yaml` | Render deployment blueprint |
| `Procfile` | Process file for Render worker |
| `runtime.txt` | Python version for Render |
