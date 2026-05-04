import os
import json
import asyncio
import logging
from flask import Flask, Request, Response, request
from telegram import Update

app = Flask(__name__)
logger = logging.getLogger(__name__)

_tg_app = None


def get_tg_app():
    global _tg_app
    if _tg_app is None:
        from bot import build_app
        _tg_app = build_app()
    return _tg_app


@app.route("/")
def index():
    return "Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    tg_app = get_tg_app()

    update = Update.de_json(request.get_json(force=True), tg_app.bot)

    async def process():
        async with tg_app:
            await tg_app.process_update(update)

    asyncio.run(process())
    return Response("ok", status=200)


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
