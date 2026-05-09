#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║           GOLDWATCH ALERT BOT — Telegram Edition             ║
║   XAU/USD (Twelve Data) + BTC/USDT (Binance)                 ║
║   Strategies: Silver Bullet · CISD · CHoCH+OB                ║
║   Filters:    VWAP · ORB · HTF Bias                          ║
║   Alerts:     Telegram with split position plan              ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install requests schedule

RUN:
  python3 goldwatch_bot.py

KEEP RUNNING ON MAC (prevents sleep):
  caffeinate -i python3 goldwatch_bot.py
"""

import os
import requests
import schedule
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ══════════════════════════════════════════════════════════════
#  CONFIG — reads from environment variables (GitHub Secrets)
#           falls back to hardcoded values for local use
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',   '8749055255:AAEYOoJ2eOLwBxcU5M0NZ51LldyHFxMG8pQ')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '6812301705')
TWELVE_DATA_KEY  = os.environ.get('TWELVE_DATA_KEY',  'da7995eb639b49bd8a976ecb6a80f16f')

# Google Sheets config
SPREADSHEET_ID   = '1JCJqRfANlV9afimJeW1jnFAJi27oTqjPWwy7b0LdjfY'
SERVICE_ACCOUNT_EMAIL = 'goldwatch-bot@goldwatch-alert.iam.gserviceaccount.com'
# Service account JSON key — read from GitHub Secret
GS_SERVICE_JSON  = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')

# ══════════════════════════════════════════════════════════════
#  ACCOUNT SIZING — $1,000 account · 0.5% risk · Split position
# ══════════════════════════════════════════════════════════════
XAU_SL_DIST  = 2.00   # Gold SL distance in dollars
BTC_SL_DIST  = 200    # BTC SL distance in dollars
TP1_MULT     = 2      # 1:2 RR — Position A closes here
TP2_MULT     = 3      # 1:3 RR — Position B closes here
RISK_USD     = 5.00   # Max loss per trade

XAU_POS_A    = 0.013  # Gold lots — closes at TP1
XAU_POS_B    = 0.012  # Gold lots — runs to TP2
BTC_POS_A    = 0.013  # BTC — closes at TP1
BTC_POS_B    = 0.012  # BTC — runs to TP2

# ══════════════════════════════════════════════════════════════
#  STRATEGY SWITCHES
# ══════════════════════════════════════════════════════════════
ENABLE_SILVER_BULLET = True
ENABLE_CISD          = True
ENABLE_CHOCH_OB      = True
ENABLE_XAU           = True
ENABLE_BTC           = True

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('GoldWatch')

# ══════════════════════════════════════════════════════════════
#  COOLDOWN — prevent duplicate alerts within 10 minutes
# ══════════════════════════════════════════════════════════════
alert_cooldown = {}  # key → timestamp
COOLDOWN_SECS  = 600  # 10 minutes

def is_cooled(key):
    now = time.time()
    if key in alert_cooldown:
        if now - alert_cooldown[key] < COOLDOWN_SECS:
            return False  # still in cooldown
    alert_cooldown[key] = now
    return True  # cooled, fire alert

# ══════════════════════════════════════════════════════════════
#  TIME HELPERS — WIB (UTC+7)
# ══════════════════════════════════════════════════════════════
WIB = timezone(timedelta(hours=7))

def now_wib():
    return datetime.now(WIB)

def wib_str():
    return now_wib().strftime('%H:%M:%S WIB')

def wib_hour():
    return now_wib().hour

def today_wib_str():
    return now_wib().strftime('%Y-%m-%d')

# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════
def send_telegram(message):
    """Send a Telegram message. Returns True on success."""
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info(f'Telegram sent: {message[:60]}…')
            return True
        else:
            log.warning(f'Telegram error {resp.status_code}: {resp.text}')
    except Exception as e:
        log.error(f'Telegram exception: {e}')
    return False

# ══════════════════════════════════════════════════════════════
#  GOOGLE SHEETS — Service Account Auth + Write
# ══════════════════════════════════════════════════════════════

_gs_token = None
_gs_token_expiry = 0

def get_gs_token():
    """Get a valid Google OAuth2 access token using service account JWT."""
    global _gs_token, _gs_token_expiry
    now = time.time()

    # Return cached token if still valid (expires in 1 hour, refresh 5 min early)
    if _gs_token and now < _gs_token_expiry - 300:
        return _gs_token

    if not GS_SERVICE_JSON:
        log.warning('GOOGLE_SERVICE_ACCOUNT_JSON not set — Sheets sync disabled')
        return None

    try:
        import base64
        import hmac
        import hashlib
        import struct

        # Parse service account JSON
        sa = json.loads(GS_SERVICE_JSON)
        private_key_pem = sa['private_key']
        client_email    = sa['client_email']

        # Build JWT header + payload
        iat = int(now)
        exp = iat + 3600
        scope = 'https://www.googleapis.com/auth/spreadsheets'
        token_uri = 'https://oauth2.googleapis.com/token'

        header  = {'alg': 'RS256', 'typ': 'JWT'}
        payload = {
            'iss': client_email,
            'scope': scope,
            'aud': token_uri,
            'iat': iat,
            'exp': exp,
        }

        def b64url(data):
            if isinstance(data, dict):
                data = json.dumps(data, separators=(',', ':')).encode()
            return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

        signing_input = f'{b64url(header)}.{b64url(payload)}'.encode()

        # Sign with RSA-SHA256 using cryptography library
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            private_key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None
            )
            signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        except ImportError:
            # Fallback: use subprocess openssl if cryptography not available
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix='.pem', mode='w', delete=False) as f:
                f.write(private_key_pem)
                key_path = f.name
            result = subprocess.run(
                ['openssl', 'dgst', '-sha256', '-sign', key_path],
                input=signing_input, capture_output=True
            )
            signature = result.stdout
            import os as _os; _os.unlink(key_path)

        jwt_token = f'{signing_input.decode()}.{b64url(signature)}'

        # Exchange JWT for access token
        resp = requests.post(token_uri, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': jwt_token,
        }, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            _gs_token = data['access_token']
            _gs_token_expiry = now + data.get('expires_in', 3600)
            log.info('Google Sheets token obtained ✓')
            return _gs_token
        else:
            log.warning(f'GS token error: {resp.status_code} {resp.text}')
            return None

    except Exception as e:
        log.error(f'GS auth error: {e}')
        return None


def gs_request(method, path, body=None):
    """Make an authenticated Google Sheets API request."""
    token = get_gs_token()
    if not token:
        return None
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}{path}'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.request(
            method, url, headers=headers,
            json=body, timeout=15
        )
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            log.warning(f'Sheets API {resp.status_code}: {resp.text[:200]}')
            return None
    except Exception as e:
        log.error(f'Sheets request error: {e}')
        return None


def ensure_sheet_tab(tab_name):
    """Create the sheet tab with headers if it doesn't exist."""
    # Check existing sheets
    meta = gs_request('GET', '')
    if not meta:
        return False
    existing = [s['properties']['title'] for s in meta.get('sheets', [])]
    if tab_name not in existing:
        # Create the tab
        gs_request('POST', ':batchUpdate', {
            'requests': [{'addSheet': {'properties': {'title': tab_name}}}]
        })
        # Add headers
        gs_request('PUT',
            f'/values/{tab_name}!A1:L1?valueInputOption=RAW',
            {'values': [['Time (WIB)', 'Pair', 'Strategy', 'Direction',
                         'Entry', 'SL', 'TP1', 'TP2', 'BE',
                         'HTF Align', 'VWAP+ORB', 'Status']]}
        )
        log.info(f'Created sheet tab: {tab_name}')
    return True


def write_signal_to_sheets(pair, strategy, direction, entry,
                            sl, tp1, tp2, be, htf_align, vwap_orb):
    """Append a signal row to the appropriate sheet tab."""
    if not GS_SERVICE_JSON:
        return  # Sheets sync not configured

    tab = 'XAU_Signals' if pair == 'XAU/USD' else 'BTC_Signals'

    try:
        ensure_sheet_tab(tab)

        def fp(p):
            return f'{p:.2f}' if pair == 'XAU/USD' else f'{p:.0f}'

        row = [
            wib_str(),          # Time (WIB)
            pair,               # Pair
            strategy,           # Strategy
            direction,          # Direction
            fp(entry),          # Entry
            fp(sl),             # SL
            fp(tp1),            # TP1
            fp(tp2),            # TP2
            fp(be),             # BE (breakeven)
            htf_align,          # HTF Align
            vwap_orb,           # VWAP+ORB verdict
            'OPEN',             # Status
        ]

        result = gs_request('POST',
            f'/values/{tab}!A:L:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS',
            {'values': [row]}
        )
        if result:
            log.info(f'Signal written to Sheets ({tab}) ✓')
        else:
            log.warning('Failed to write signal to Sheets')

    except Exception as e:
        log.error(f'write_signal_to_sheets error: {e}')

def format_alert(pair, strategy, direction, entry, sl, tp1, tp2,
                 be, htf_bias, vwap_orb, pos_a, pos_b,
                 a_gain, b_gain, note=''):
    """Format a rich Telegram alert message."""
    arrow  = '🟢' if direction == 'LONG'  else '🔴'
    sl_lbl = '🛑'
    tp1_lbl= '🎯'
    tp2_lbl= '🏆'

    # Format prices
    def fp(p):
        return f'${p:,.2f}' if pair == 'XAU/USD' else f'${p:,.0f}'

    htf_line  = f'📊 HTF Bias: {htf_bias}' if htf_bias else ''
    vorb_line = f'📍 {vwap_orb}'           if vwap_orb else ''
    note_line = f'📝 {note}'               if note     else ''

    msg = f"""{arrow} <b>{pair} — {strategy}</b>
<b>Direction: {direction}</b>

<b>Entry:</b>  {fp(entry)}
{sl_lbl} <b>SL:</b>    {fp(sl)}
   ↳ Move to BE {fp(be)} after TP1

{tp1_lbl} <b>A½ TP1:</b> {fp(tp1)}  → close {pos_a} → +${a_gain:.2f}
{tp2_lbl} <b>B½ TP2:</b> {fp(tp2)}  → close {pos_b} → +${b_gain:.2f}
💰 <b>Max gain: +${a_gain+b_gain:.2f}</b>  |  Max loss: -${RISK_USD:.2f}
{htf_line}
{vorb_line}
{note_line}
🕐 {wib_str()}"""

    return msg.strip()

# ══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════

def fetch_twelve_data(symbol, interval, outputsize=50):
    """Fetch OHLC candles from Twelve Data. Returns list oldest→newest."""
    if not TWELVE_DATA_KEY:
        log.warning('Twelve Data API key not set — skipping XAU/USD')
        return []
    url = (f'https://api.twelvedata.com/time_series'
           f'?symbol={symbol}&interval={interval}'
           f'&outputsize={outputsize}&apikey={TWELVE_DATA_KEY}')
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get('status') == 'error' or 'values' not in data:
            log.warning(f'Twelve Data error: {data.get("message", data)}')
            return []
        candles = []
        for v in reversed(data['values']):  # API: newest first → reverse
            candles.append({
                'datetime': v['datetime'],
                'open':  float(v['open']),
                'high':  float(v['high']),
                'low':   float(v['low']),
                'close': float(v['close']),
            })
        return candles
    except Exception as e:
        log.error(f'Twelve Data fetch error: {e}')
        return []


def fetch_binance(symbol, interval, limit=50):
    """
    Fetch BTC/USDT OHLC candles.
    Primary:  Bybit (not blocked in Indonesia)
    Fallback: Bitget, then OKX
    All return oldest→newest.
    """
    # Map Binance interval format to each exchange format
    interval_map = {
        '1m':'1','3m':'3','5m':'5','15m':'15','30m':'30',
        '1h':'60','4h':'240','1d':'D',
    }
    bybit_interval = interval_map.get(interval, '5')

    # ── Bybit (usually accessible in Indonesia) ──
    try:
        url = (f'https://api.bybit.com/v5/market/kline'
               f'?category=spot&symbol=BTCUSDT'
               f'&interval={bybit_interval}&limit={limit}')
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=15, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get('result', {}).get('list', [])
            if rows:
                # Bybit returns newest first → reverse
                candles = []
                for k in reversed(rows):
                    candles.append({
                        'datetime': datetime.fromtimestamp(
                            int(k[0])/1000, WIB).strftime('%Y-%m-%d %H:%M'),
                        'open':  float(k[1]),
                        'high':  float(k[2]),
                        'low':   float(k[3]),
                        'close': float(k[4]),
                    })
                log.info(f'BTC data from Bybit ({len(candles)} candles)')
                return candles
    except Exception as e:
        log.warning(f'Bybit failed: {e}')

    # ── Bitget fallback ──
    try:
        bitget_interval_map = {
            '5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D'
        }
        bg_interval = bitget_interval_map.get(interval, '5m')
        url = (f'https://api.bitget.com/api/v2/spot/market/candles'
               f'?symbol=BTCUSDT&granularity={bg_interval}&limit={limit}')
        resp = requests.get(url, timeout=15, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get('data', [])
            if rows:
                candles = []
                for k in reversed(rows):
                    candles.append({
                        'datetime': datetime.fromtimestamp(
                            int(k[0])/1000, WIB).strftime('%Y-%m-%d %H:%M'),
                        'open':  float(k[1]),
                        'high':  float(k[2]),
                        'low':   float(k[3]),
                        'close': float(k[4]),
                    })
                log.info(f'BTC data from Bitget ({len(candles)} candles)')
                return candles
    except Exception as e:
        log.warning(f'Bitget failed: {e}')

    # ── OKX fallback ──
    try:
        okx_interval_map = {
            '5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D'
        }
        ok_interval = okx_interval_map.get(interval, '5m')
        url = (f'https://www.okx.com/api/v5/market/candles'
               f'?instId=BTC-USDT&bar={ok_interval}&limit={limit}')
        resp = requests.get(url, timeout=15, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            rows = data.get('data', [])
            if rows:
                candles = []
                for k in reversed(rows):
                    candles.append({
                        'datetime': datetime.fromtimestamp(
                            int(k[0])/1000, WIB).strftime('%Y-%m-%d %H:%M'),
                        'open':  float(k[1]),
                        'high':  float(k[2]),
                        'low':   float(k[3]),
                        'close': float(k[4]),
                    })
                log.info(f'BTC data from OKX ({len(candles)} candles)')
                return candles
    except Exception as e:
        log.warning(f'OKX failed: {e}')

    log.error('All BTC data sources failed — BTC unavailable this cycle')
    return []

# ══════════════════════════════════════════════════════════════
#  BIAS COMPUTATION — EMA21 + Swing Structure
# ══════════════════════════════════════════════════════════════

def calc_ema(candles, period=21):
    k = 2 / (period + 1)
    ema = candles[0]['close']
    result = [ema]
    for c in candles[1:]:
        ema = c['close'] * k + ema * (1 - k)
        result.append(ema)
    return result

def compute_bias(candles):
    """Returns 'bull', 'bear', or 'neut' based on EMA21 + swing structure."""
    if len(candles) < 10:
        return 'neut'
    ema_vals = calc_ema(candles)
    ema_now  = ema_vals[-1]
    ema_prev = ema_vals[-2]
    last_close = candles[-1]['close']

    # Swing highs/lows over last 20 candles
    sl = candles[-20:]
    highs, lows = [], []
    for i in range(2, len(sl) - 2):
        c = sl[i]
        if (c['high'] > sl[i-1]['high'] and c['high'] > sl[i-2]['high'] and
                c['high'] > sl[i+1]['high'] and c['high'] > sl[i+2]['high']):
            highs.append(c['high'])
        if (c['low'] < sl[i-1]['low'] and c['low'] < sl[i-2]['low'] and
                c['low'] < sl[i+1]['low'] and c['low'] < sl[i+2]['low']):
            lows.append(c['low'])

    bull_pts = bear_pts = 0
    if ema_now > ema_prev: bull_pts += 1
    else: bear_pts += 1
    if last_close > ema_now: bull_pts += 1
    else: bear_pts += 1
    if len(highs) >= 2:
        if highs[-1] > highs[-2]: bull_pts += 1
        else: bear_pts += 1
    if len(lows) >= 2:
        if lows[-1] > lows[-2]: bull_pts += 1
        else: bear_pts += 1

    if bull_pts >= 3: return 'bull'
    if bear_pts >= 3: return 'bear'
    return 'neut'

def bias_label(b4h, b1h):
    arrows = {'bull': '▲', 'bear': '▼', 'neut': '—'}
    return (f"4H {arrows[b4h]} {b4h.upper()} | "
            f"1H {arrows[b1h]} {b1h.upper()}")

def htf_aligned(direction, b4h, b1h):
    """Returns alignment label for the signal direction."""
    d = 'bull' if direction == 'LONG' else 'bear'
    h4 = b4h == d
    h1 = b1h == d
    if h4 and h1:  return '✅ 4H + 1H ALIGNED'
    if h4 or h1:   return f'⚠️ Partial ({"4H" if h4 else "1H"})'
    if b4h == 'neut' and b1h == 'neut': return '➖ HTF neutral'
    return '❌ AGAINST HTF bias'

# ══════════════════════════════════════════════════════════════
#  VWAP + ORB
# ══════════════════════════════════════════════════════════════

def calc_vwap(candles):
    """Session VWAP (equal-weight typical price from today WIB)."""
    today = today_wib_str()
    today_c = [c for c in candles if c['datetime'].startswith(today)]
    if len(today_c) < 2:
        return None
    cum = 0
    vwap_vals = []
    for i, c in enumerate(today_c):
        tp = (c['high'] + c['low'] + c['close']) / 3
        cum += tp
        vwap_vals.append(cum / (i + 1))
    return vwap_vals[-1] if vwap_vals else None

def get_vwap_bias(candles):
    vwap = calc_vwap(candles)
    if vwap is None:
        return 'at', None
    price = candles[-1]['close']
    diff  = (price - vwap) / vwap * 100
    if diff > 0.05:  return 'above', vwap
    if diff < -0.05: return 'below', vwap
    return 'at', vwap

def calc_orb(candles):
    """Opening Range: high/low of 14:00 WIB (London) and 19:00 WIB (NY)."""
    sessions = [('London', 14), ('NY', 19)]
    orbs = []
    for name, hour in sessions:
        rc = [c for c in candles
              if c['datetime'][11:13] == str(hour).zfill(2)
              and c['datetime'][:10] == today_wib_str()]
        if rc:
            orbs.append({
                'name': name,
                'high': max(c['high'] for c in rc),
                'low':  min(c['low']  for c in rc),
            })
    return orbs

def get_orb_context(candles):
    orbs = calc_orb(candles)
    if not orbs:
        return None
    price = candles[-1]['close']
    for orb in orbs:
        buf = orb['high'] * 0.001
        if price > orb['high'] + buf:
            return f"Above {orb['name']} ORB ({orb['high']:.2f})"
        if price < orb['low'] - buf:
            return f"Below {orb['name']} ORB ({orb['low']:.2f})"
        return f"Inside {orb['name']} ORB range"
    return None

def vwap_orb_verdict(direction, candles):
    """Returns combined VWAP+ORB confirmation label."""
    vbias, _ = get_vwap_bias(candles)
    orb_ctx   = get_orb_context(candles)
    lines = []

    if direction == 'LONG':
        if vbias == 'above':  lines.append(('aligned', 'Price above VWAP'))
        elif vbias == 'below': lines.append(('against', 'Price below VWAP'))
        else:                  lines.append(('neutral', 'At VWAP'))
    else:
        if vbias == 'below':  lines.append(('aligned', 'Price below VWAP'))
        elif vbias == 'above': lines.append(('against', 'Price above VWAP'))
        else:                  lines.append(('neutral', 'At VWAP'))

    if orb_ctx:
        is_aligned = (direction == 'LONG' and 'Above' in orb_ctx) or \
                     (direction == 'SHORT' and 'Below' in orb_ctx)
        is_inside  = 'Inside' in orb_ctx
        if is_aligned:  lines.append(('aligned', orb_ctx))
        elif is_inside: lines.append(('partial', orb_ctx))
        else:           lines.append(('against', orb_ctx))

    aligned = sum(1 for s, _ in lines if s == 'aligned')
    against = sum(1 for s, _ in lines if s == 'against')

    details = ' | '.join(t for _, t in lines)
    if aligned >= 2:   return f'⭐ VWAP+ORB CONFIRMED ({details})'
    if aligned >= 1 and against == 0: return f'🔶 Partial ({details})'
    if against >= 1:   return f'⛔ Against VWAP/ORB ({details})'
    return f'➖ No session data ({details})'

# ══════════════════════════════════════════════════════════════
#  LEVEL CALCULATION
# ══════════════════════════════════════════════════════════════

def calc_levels(entry, direction, sl_dist):
    if direction == 'LONG':
        return {
            'sl':  entry - sl_dist,
            'tp1': entry + sl_dist * TP1_MULT,
            'tp2': entry + sl_dist * TP2_MULT,
            'be':  entry,
        }
    else:
        return {
            'sl':  entry + sl_dist,
            'tp1': entry - sl_dist * TP1_MULT,
            'tp2': entry - sl_dist * TP2_MULT,
            'be':  entry,
        }

# ══════════════════════════════════════════════════════════════
#  STRATEGY 1 — ICT Silver Bullet
#  Only runs during 22:00–22:59 WIB (10 PM WIB = 10 AM EST)
# ══════════════════════════════════════════════════════════════

def run_silver_bullet(candles, pair, sl_dist, pos_a, pos_b):
    if not ENABLE_SILVER_BULLET:
        return []
    if wib_hour() != 22:
        return []

    n = len(candles)
    if n < 15:
        return []

    alerts = []
    scan_start = max(11, n - 20)

    for i in range(scan_start, n - 2):
        prior   = candles[max(0, i - 10):i]
        lo_low  = min(c['low']  for c in prior)
        hi_high = max(c['high'] for c in prior)
        sweep   = candles[i]
        reversal= candles[i + 1]

        # Bullish sweep + FVG
        if sweep['low'] < lo_low and reversal['close'] > reversal['open'] and i >= 1:
            prev = candles[i - 1]
            nxt  = candles[i + 1]
            if prev['high'] < nxt['low']:
                fvg_lo, fvg_hi = prev['high'], nxt['low']
                price = candles[-1]['close']
                if fvg_lo <= price <= fvg_hi:
                    key = f'SB|LONG|{pair}|{round(price, 1)}'
                    if is_cooled(key):
                        alerts.append(('LONG', price, 'Silver Bullet LONG',
                                       f'FVG zone {fvg_lo:.2f}–{fvg_hi:.2f}'))

        # Bearish sweep + FVG
        if sweep['high'] > hi_high and reversal['close'] < reversal['open'] and i >= 1:
            prev = candles[i - 1]
            nxt  = candles[i + 1]
            if prev['low'] > nxt['high']:
                fvg_lo, fvg_hi = nxt['high'], prev['low']
                price = candles[-1]['close']
                if fvg_lo <= price <= fvg_hi:
                    key = f'SB|SHORT|{pair}|{round(price, 1)}'
                    if is_cooled(key):
                        alerts.append(('SHORT', price, 'Silver Bullet SHORT',
                                       f'FVG zone {fvg_lo:.2f}–{fvg_hi:.2f}'))
    return alerts

# ══════════════════════════════════════════════════════════════
#  STRATEGY 2 — CISD + Confluence
# ══════════════════════════════════════════════════════════════

def check_confluence(direction, candles):
    n = len(candles)
    prior = candles[max(0, n - 10):n - 5]
    last5 = candles[max(0, n - 5):]

    # a) Liquidity sweep in last 5 candles
    if prior:
        ph = max(c['high'] for c in prior)
        pl = min(c['low']  for c in prior)
        for c in last5:
            if (c['high'] > ph and c['close'] < ph) or \
               (c['low']  < pl and c['close'] > pl):
                return True, 'LiqSweep'

    # b) Order Block touch
    ob = find_order_block(direction, candles)
    if ob:
        price = candles[-1]['close']
        if ob['low'] <= price <= ob['high']:
            return True, 'OB Touch'

    # c) FVG in last 3 candles
    for i in range(max(1, n - 3), n - 1):
        prev = candles[i - 1]
        nxt  = candles[i + 1] if i + 1 < n else None
        if nxt:
            if prev['high'] < nxt['low']:
                return True, 'FVG'
            if prev['low'] > nxt['high']:
                return True, 'FVG'

    return False, None

def find_order_block(direction, candles):
    n = len(candles)
    if direction == 'bull':
        for i in range(n - 4, 0, -1):
            if candles[i]['close'] < candles[i]['open']:
                run = sum(1 for j in range(i+1, min(i+4, n))
                          if candles[j]['close'] > candles[j]['open'])
                if run >= 3:
                    return {'low': candles[i]['low'], 'high': candles[i]['high']}
    else:
        for i in range(n - 4, 0, -1):
            if candles[i]['close'] > candles[i]['open']:
                run = sum(1 for j in range(i+1, min(i+4, n))
                          if candles[j]['close'] < candles[j]['open'])
                if run >= 3:
                    return {'low': candles[i]['low'], 'high': candles[i]['high']}
    return None

def run_cisd(candles, pair):
    if not ENABLE_CISD:
        return []
    n = len(candles)
    if n < 8:
        return []

    alerts = []
    cur = candles[-1]

    # Bullish CISD: 2+ consecutive bearish → close above first bearish open
    bear_count = 0
    first_bear_idx = -1
    for i in range(n - 2, max(0, n - 12), -1):
        if candles[i]['close'] < candles[i]['open']:
            bear_count += 1
            first_bear_idx = i
        else:
            break
    if bear_count >= 2 and first_bear_idx >= 0 and \
            cur['close'] > candles[first_bear_idx]['open']:
        hit, conf_type = check_confluence('bull', candles)
        if hit:
            price = cur['close']
            key = f'CISD|LONG|{pair}|{round(price, 0)}'
            if is_cooled(key):
                alerts.append(('LONG', price,
                                f'CISD LONG + {conf_type}', ''))

    # Bearish CISD: 2+ consecutive bullish → close below first bullish open
    bull_count = 0
    first_bull_idx = -1
    for i in range(n - 2, max(0, n - 12), -1):
        if candles[i]['close'] > candles[i]['open']:
            bull_count += 1
            first_bull_idx = i
        else:
            break
    if bull_count >= 2 and first_bull_idx >= 0 and \
            cur['close'] < candles[first_bull_idx]['open']:
        hit, conf_type = check_confluence('bear', candles)
        if hit:
            price = cur['close']
            key = f'CISD|SHORT|{pair}|{round(price, 0)}'
            if is_cooled(key):
                alerts.append(('SHORT', price,
                                f'CISD SHORT + {conf_type}', ''))

    return alerts

# ══════════════════════════════════════════════════════════════
#  STRATEGY 3 — CHoCH + Order Block
# ══════════════════════════════════════════════════════════════

def run_choch_ob(candles, pair):
    if not ENABLE_CHOCH_OB:
        return []
    n = len(candles)
    if n < 10:
        return []

    lookback = min(30, n)
    sl       = candles[n - lookback:]

    swing_highs, swing_lows = [], []
    for i in range(2, len(sl) - 2):
        c = sl[i]
        if (c['high'] > sl[i-1]['high'] and c['high'] > sl[i-2]['high'] and
                c['high'] > sl[i+1]['high'] and c['high'] > sl[i+2]['high']):
            swing_highs.append({'idx': (n - lookback) + i, 'price': c['high']})
        if (c['low'] < sl[i-1]['low'] and c['low'] < sl[i-2]['low'] and
                c['low'] < sl[i+1]['low'] and c['low'] < sl[i+2]['low']):
            swing_lows.append({'idx': (n - lookback) + i, 'price': c['low']})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return []

    alerts  = []
    cp      = candles[-1]['close']
    rsh, psh = swing_highs[-1], swing_highs[-2]
    rsl, psl = swing_lows[-1],  swing_lows[-2]

    # Bullish CHoCH: lower highs + lower lows → price breaks above recent SH
    if rsh['price'] < psh['price'] and rsl['price'] < psl['price'] and cp > rsh['price']:
        ob = find_last_bearish_before(rsh['idx'], candles)
        if ob and ob['low'] <= cp <= ob['high']:
            key = f'CHOCH|LONG|{pair}|{round(cp, 0)}'
            if is_cooled(key):
                alerts.append(('LONG', cp, 'CHoCH+OB LONG',
                                f"OB zone {ob['low']:.2f}–{ob['high']:.2f}"))

    # Bearish CHoCH: higher highs + higher lows → price breaks below recent SL
    if rsh['price'] > psh['price'] and rsl['price'] > psl['price'] and cp < rsl['price']:
        ob = find_last_bullish_before(rsl['idx'], candles)
        if ob and ob['low'] <= cp <= ob['high']:
            key = f'CHOCH|SHORT|{pair}|{round(cp, 0)}'
            if is_cooled(key):
                alerts.append(('SHORT', cp, 'CHoCH+OB SHORT',
                                f"OB zone {ob['low']:.2f}–{ob['high']:.2f}"))

    return alerts

def find_last_bearish_before(idx, candles):
    for i in range(idx - 1, -1, -1):
        if candles[i]['close'] < candles[i]['open']:
            return {'low': candles[i]['low'], 'high': candles[i]['high']}
    return None

def find_last_bullish_before(idx, candles):
    for i in range(idx - 1, -1, -1):
        if candles[i]['close'] > candles[i]['open']:
            return {'low': candles[i]['low'], 'high': candles[i]['high']}
    return None

# ══════════════════════════════════════════════════════════════
#  MAIN SCAN — runs every 60 seconds
# ══════════════════════════════════════════════════════════════

# Store HTF candles (refreshed every 5 min)
htf_cache = {
    'xau_1h': [], 'xau_4h': [],
    'btc_1h': [], 'btc_4h': [],
    'last_htf': 0,
}

def refresh_htf():
    """Refresh 1H and 4H candles every 5 minutes."""
    now = time.time()
    if now - htf_cache['last_htf'] < 300:
        return
    log.info('Refreshing HTF candles…')
    if ENABLE_XAU and TWELVE_DATA_KEY:
        time.sleep(0.8)
        htf_cache['xau_1h'] = fetch_twelve_data('XAU/USD', '1h', 50)
        time.sleep(0.8)
        htf_cache['xau_4h'] = fetch_twelve_data('XAU/USD', '4h', 50)
    if ENABLE_BTC:
        htf_cache['btc_1h'] = fetch_binance('BTCUSDT', '1h', 50)
        htf_cache['btc_4h'] = fetch_binance('BTCUSDT', '4h', 50)
    htf_cache['last_htf'] = now

def scan():
    """Main scan — fetch 5m data and run all strategies."""
    log.info(f'Scanning… [{wib_str()}]')
    refresh_htf()

    # ── XAU/USD ──
    if ENABLE_XAU and TWELVE_DATA_KEY:
        try:
            time.sleep(0.8)
            xau_5m = fetch_twelve_data('XAU/USD', '5min', 50)
            if xau_5m:
                price = xau_5m[-1]['close']
                log.info(f'XAU/USD: ${price:.2f}')

                # HTF bias
                b4h = compute_bias(htf_cache['xau_4h']) if htf_cache['xau_4h'] else 'neut'
                b1h = compute_bias(htf_cache['xau_1h']) if htf_cache['xau_1h'] else 'neut'

                # Run strategies
                all_alerts = []
                all_alerts += run_silver_bullet(xau_5m, 'XAU/USD', XAU_SL_DIST, XAU_POS_A, XAU_POS_B)
                all_alerts += run_cisd(xau_5m, 'XAU/USD')
                all_alerts += run_choch_ob(xau_5m, 'XAU/USD')

                for direction, entry, strat_name, note in all_alerts:
                    lvl    = calc_levels(entry, direction, XAU_SL_DIST)
                    a_gain = RISK_USD * TP1_MULT * 0.5
                    b_gain = RISK_USD * TP2_MULT * 0.5
                    vo     = vwap_orb_verdict(direction, xau_5m)
                    htf    = f'{bias_label(b4h, b1h)} | {htf_aligned(direction, b4h, b1h)}'
                    pos_a  = f'{XAU_POS_A} lot'
                    pos_b  = f'{XAU_POS_B} lot'
                    msg    = format_alert(
                        'XAU/USD', strat_name, direction,
                        entry, lvl['sl'], lvl['tp1'], lvl['tp2'], lvl['be'],
                        htf, vo, pos_a, pos_b, a_gain, b_gain, note
                    )
                    send_telegram(msg)
                    # Write to Google Sheets so HTML app can load it
                    write_signal_to_sheets(
                        'XAU/USD', strat_name, direction,
                        entry, lvl['sl'], lvl['tp1'], lvl['tp2'], lvl['be'],
                        htf_aligned(direction, b4h, b1h), vo
                    )
                    time.sleep(1)
        except Exception as e:
            log.error(f'XAU scan error: {e}')

    # ── BTC/USDT ──
    if ENABLE_BTC:
        try:
            btc_5m = fetch_binance('BTCUSDT', '5m', 50)
            if btc_5m:
                price = btc_5m[-1]['close']
                log.info(f'BTC/USDT: ${price:,.0f}')

                b4h = compute_bias(htf_cache['btc_4h']) if htf_cache['btc_4h'] else 'neut'
                b1h = compute_bias(htf_cache['btc_1h']) if htf_cache['btc_1h'] else 'neut'

                all_alerts = []
                all_alerts += run_silver_bullet(btc_5m, 'BTC/USDT', BTC_SL_DIST, BTC_POS_A, BTC_POS_B)
                all_alerts += run_cisd(btc_5m, 'BTC/USDT')
                all_alerts += run_choch_ob(btc_5m, 'BTC/USDT')

                for direction, entry, strat_name, note in all_alerts:
                    lvl    = calc_levels(entry, direction, BTC_SL_DIST)
                    a_gain = RISK_USD * TP1_MULT * 0.5
                    b_gain = RISK_USD * TP2_MULT * 0.5
                    vo     = vwap_orb_verdict(direction, btc_5m)
                    htf    = f'{bias_label(b4h, b1h)} | {htf_aligned(direction, b4h, b1h)}'
                    pos_a  = f'{BTC_POS_A} BTC'
                    pos_b  = f'{BTC_POS_B} BTC'
                    msg    = format_alert(
                        'BTC/USDT', strat_name, direction,
                        entry, lvl['sl'], lvl['tp1'], lvl['tp2'], lvl['be'],
                        htf, vo, pos_a, pos_b, a_gain, b_gain, note
                    )
                    send_telegram(msg)
                    # Write to Google Sheets so HTML app can load it
                    write_signal_to_sheets(
                        'BTC/USDT', strat_name, direction,
                        entry, lvl['sl'], lvl['tp1'], lvl['tp2'], lvl['be'],
                        htf_aligned(direction, b4h, b1h), vo
                    )
                    time.sleep(1)
        except Exception as e:
            log.error(f'BTC scan error: {e}')

# ══════════════════════════════════════════════════════════════
#  STARTUP + SCHEDULER
# ══════════════════════════════════════════════════════════════

def startup_message():
    """Send a startup notification to Telegram."""
    pairs = []
    if ENABLE_XAU: pairs.append('XAU/USD' + ('' if TWELVE_DATA_KEY else ' ⚠️ (no key)'))
    if ENABLE_BTC: pairs.append('BTC/USDT')
    strats = []
    if ENABLE_SILVER_BULLET: strats.append('Silver Bullet (22:00 WIB)')
    if ENABLE_CISD:          strats.append('CISD + Confluence')
    if ENABLE_CHOCH_OB:      strats.append('CHoCH + Order Block')
    msg = f"""🚀 <b>GOLDWATCH BOT STARTED</b>

📌 <b>Pairs:</b> {' | '.join(pairs)}
🎯 <b>Strategies:</b>
  • {chr(10)+'  • '.join(strats)}
💰 <b>Account:</b> $1,000 · 0.5% risk · Split position
⏱ <b>Scan interval:</b> Every 60 seconds
🕐 <b>Started:</b> {wib_str()}

Bot is now running 24/7.
You will be alerted here when a signal fires."""
    send_telegram(msg)

if __name__ == '__main__':
    log.info('=' * 60)
    log.info('  GOLDWATCH BOT starting…')
    log.info('=' * 60)

    # Detect if running in GitHub Actions (single-run mode)
    # GitHub Actions sets the CI environment variable
    IS_GITHUB_ACTIONS = os.environ.get('GITHUB_ACTIONS', 'false') == 'true'

    if not TWELVE_DATA_KEY:
        log.warning('⚠  TWELVE_DATA_KEY is empty — XAU/USD disabled')

    if IS_GITHUB_ACTIONS:
        # ── GITHUB ACTIONS MODE ──
        # GitHub runs this script every 5 minutes via cron
        # We just do one scan and exit — no infinite loop needed
        log.info('Mode: GitHub Actions (single scan)')
        refresh_htf()
        scan()
        log.info('Scan complete — exiting.')
    else:
        # ── LOCAL / MAC MODE ──
        # Continuous loop, scans every 60 seconds
        log.info('Mode: Local continuous (every 60s)')
        startup_message()
        refresh_htf()
        scan()
        schedule.every(60).seconds.do(scan)
        log.info('Scheduler running. Press Ctrl+C to stop.')
        while True:
            schedule.run_pending()
            time.sleep(1)
