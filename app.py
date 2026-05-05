import os
import asyncio
import logging
from flask import Flask, Response, request
from telegram import Update

app = Flask(__name__)
logger = logging.getLogger(__name__)

_tg_app = None
_initialized = False


async def _init_app():
    global _tg_app, _initialized
    if _tg_app is None:
        from bot import build_app
        _tg_app = build_app()
    if not _initialized:
        await _tg_app.initialize()
        _initialized = True
    return _tg_app


@app.route("/")
def index():
    return "Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        tg_app = loop.run_until_complete(_init_app())
        update = Update.de_json(request.get_json(force=True), tg_app.bot)
        loop.run_until_complete(tg_app.process_update(update))

        return Response("ok", status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return Response("error", status=200)


@app.route("/setwebhook")
def set_webhook():
    import requests as req

    bot_token = os.environ["BOT_TOKEN"]
    webhook_url = os.environ.get("WEBHOOK_URL", request.host_url.rstrip("/"))
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook?url={webhook_url}/webhook"
    r = req.get(url)
    return Response(r.text, status=r.status_code, content_type="application/json")


@app.route("/deletewebhook")
def delete_webhook():
    import requests as req

    bot_token = os.environ["BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    r = req.get(url)
    return Response(r.text, status=r.status_code, content_type="application/json")
