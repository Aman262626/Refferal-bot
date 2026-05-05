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

### Run locally (polling mode)

```bash
export BOT_TOKEN="your-telegram-bot-token"
export CHANNEL_ID="-100xxxxxxxxxx"
python bot.py
```

### Bot Features (all button-based)

Send `/start` to the bot to get the main menu with these buttons:

| Button | Description |
|---|---|
| 🔍 Single Check | Check one card against a Shopify site |
| 📋 Mass Check | Send site URL, then upload .txt file with cards |
| 🌐 Site Check | Check if a Shopify site is alive |
| 📡 Mass Site | Check multiple sites at once |
| 🏦 BIN Lookup | Get card BIN information |
| 🎲 Generate | Generate Luhn-valid cards from BIN |
| ⚡ Auto Check | Continuous BIN checker — runs until stopped |
| 🔥 Auto Hit | All BINs × All Sites parallel checker |
| 🛑 Stop | Stop running session |
| ❓ Help | Feature descriptions and formats |

Every screen has a 🔙 **Back to Menu** button.

### Auto Check Features

- **BIN Library**: 33,000+ BINs from 200+ countries (browse by country, search, or random)
- **Proxy Support**: Optional proxy (HTTP/SOCKS5) for checking
- **Continuous Mode**: Generates & checks cards infinitely until you press Stop
- **Live Status**: Updates every 5 seconds showing scan count, charged, approved, declined, errors
- **Channel Posting**: Charged/Approved/3DS cards auto-posted to your Telegram channel with full BIN info

### Auto Hit Features

- **All BINs × All Sites**: Uses the entire BIN library (33,400 BINs) and all sites from `sites.txt`
- **Parallel Checking**: Each generated card is checked against ALL sites simultaneously
- **Batch Mode**: Picks random BINs, generates 5 cards per BIN, checks each card on every site at once
- **3-5 Second Intervals**: Random delay between batches to avoid rate-limiting
- **Continuous Until Stopped**: Runs until you press Stop or send `/stop`
- **Live Status**: Updates every 5 seconds — current BIN, total scanned, BINs tried, hits
- **Channel Posting**: All Charged/Approved/3DS results posted to channel with site + BIN info

### Deploy on Vercel (Free)

1. Fork/push this repo to GitHub.
2. Go to [Vercel](https://vercel.com/) → **New Project** → Import this repo.
3. Set **Environment Variables**:
   - `BOT_TOKEN` — your Telegram bot token
   - `CHANNEL_ID` — your Telegram channel ID (e.g. `-1003345433105`)
   - `ADMIN_ID` — your Telegram user ID (default: `5451167865`)
4. Deploy!
5. After deploy, visit `https://your-app.vercel.app/setwebhook` once to connect the bot to Telegram.

To disconnect: visit `https://your-app.vercel.app/deletewebhook`.

## Project Structure

| File | Description |
|---|---|
| `bot.py` | Telegram bot logic — button menu, checkers, channel posting |
| `app.py` | Flask webhook handler for Vercel deployment |
| `vercel.json` | Vercel routing config |
| `Test.py` | CLI entry-point with checker and site menus |
| `api.py` | Gateway API helpers (`process_card`, `parse_cc_string`, etc.) |
| `sites.txt` | Default Shopify site list |
| `requirements.txt` | Python dependencies |
