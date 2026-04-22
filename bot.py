import asyncio
import logging
import time
from datetime import datetime
import requests
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
import os

# ══════════════════════════════════════════════════════
#   UPBIT KRW SMART SCREENER BOT
#   🔔 PUMP INCOMING  — pump aane wala hai
#   🚀 PUMP DETECTED  — pump abhi ho raha hai
# ══════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = os.environ.get("8623031794:AAH-bQahzOhMhK-PMGyBMi4ktMmSTOJVovg")
TELEGRAM_CHAT_ID   = os.environ.get("7910756984",   "YOUR_CHAT_ID_HERE")

# ──────────────────────────────────────────────
# 🔔 PUMP INCOMING Settings
# Volume build ho rahi hai, price abhi zyada nahi badi
# ──────────────────────────────────────────────
PRE_MIN_REL_VOLUME   = 2.5    # Same as original
PRE_MIN_VOL_CHANGE   = 100.0  # Same as original
PRE_MIN_PRICE_CHANGE = -2.0   # Same as original
PRE_MAX_PRICE_CHANGE = 2.0    # Price abhi 2% se kam — pump nahi hua yet
PRE_COOLDOWN_SEC     = 1800   # 30 min cooldown

# ──────────────────────────────────────────────
# 🚀 PUMP DETECTED Settings
# Price actively move kar rahi hai abhi
# ──────────────────────────────────────────────
PUMP_MIN_REL_VOLUME   = 2.5   # Same as original
PUMP_MIN_VOL_CHANGE   = 100.0 # Same as original
PUMP_MIN_PRICE_CHANGE = 2.0   # Price +2% se upar — pump ho raha hai
PUMP_MAX_PRICE_CHANGE = 5.0   # Same as original max
PUMP_COOLDOWN_SEC     = 3600  # 1 hour cooldown

# ── Bot behavior ──
CHECK_INTERVAL_SEC = 45  # Har 45 sec scan (faster)

UPBIT_BASE = "https://api.upbit.com/v1"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("screener.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Upbit API Functions
# ──────────────────────────────────────────────

def get_all_krw_markets():
    try:
        r = requests.get(f"{UPBIT_BASE}/market/all?isDetails=false", timeout=15)
        r.raise_for_status()
        markets = [m["market"] for m in r.json() if m["market"].startswith("KRW-")]
        log.info(f"Markets found: {len(markets)}")
        return markets
    except Exception as e:
        log.error(f"Markets error: {e}")
        return []


def get_ticker_data(markets):
    results = []
    for i in range(0, len(markets), 100):
        batch = markets[i:i+100]
        try:
            r = requests.get(f"{UPBIT_BASE}/ticker",
                             params={"markets": ",".join(batch)}, timeout=15)
            r.raise_for_status()
            results.extend(r.json())
        except Exception as e:
            log.error(f"Ticker error: {e}")
    return results


def get_candle_data(market, count=8):
    try:
        r = requests.get(f"{UPBIT_BASE}/candles/days",
                         params={"market": market, "count": count}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def get_minute_candles(market, unit=5, count=12):
    """Last 1 hour ke 5-min candles — recent price action check karne ke liye"""
    try:
        r = requests.get(f"{UPBIT_BASE}/candles/minutes/{unit}",
                         params={"market": market, "count": count}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def calculate_relative_volume(market, current_vol_24h):
    """7 din average se Rel Volume calculate karo"""
    if current_vol_24h == 0:
        return 0.0
    try:
        candles = get_candle_data(market, count=8)
        if len(candles) < 2:
            return 0.0
        past_volumes = [c["candle_acc_trade_volume"] for c in candles[1:]]
        avg_vol = sum(past_volumes) / len(past_volumes)
        if avg_vol == 0:
            return 0.0
        return round(current_vol_24h / avg_vol, 2)
    except Exception:
        return 0.0


def calculate_volume_change(market):
    """Aaj ka volume vs kal ka volume"""
    try:
        candles = get_candle_data(market, count=2)
        if len(candles) < 2:
            return 0.0
        today_vol = candles[0]["candle_acc_trade_volume"]
        prev_vol  = candles[1]["candle_acc_trade_volume"]
        if prev_vol == 0:
            return 0.0
        return round(((today_vol - prev_vol) / prev_vol) * 100, 2)
    except Exception:
        return 0.0


def get_recent_price_change(market):
    """Last 1 ghante mein price kitni badi — recent momentum check"""
    try:
        candles = get_minute_candles(market, unit=5, count=12)
        if len(candles) < 2:
            return 0.0
        # Most recent candle close vs 1 hour ago open
        recent_close = candles[0]["trade_price"]
        old_open     = candles[-1]["opening_price"]
        if old_open == 0:
            return 0.0
        return round(((recent_close - old_open) / old_open) * 100, 2)
    except Exception:
        return 0.0


# ──────────────────────────────────────────────
# Screening Logic
# ──────────────────────────────────────────────

def screen_all_coins(tickers):
    """
    Returns two lists:
    - incoming: PUMP INCOMING coins
    - detected: PUMP DETECTED coins
    """
    incoming = []
    detected = []

    for t in tickers:
        try:
            market        = t.get("market", "")
            price_change  = round(t.get("signed_change_rate", 0) * 100, 2)
            current_vol   = t.get("acc_trade_volume_24h", 0)
            current_price = t.get("trade_price", 0)
            base          = market.replace("KRW-", "")

            # Skip very low volume coins (noise filter)
            trade_price_krw = current_price * current_vol
            if trade_price_krw < 500_000_000:  # Less than 500M KRW = skip
                continue

            # Calculate volume metrics
            vol_change = calculate_volume_change(market)
            rel_vol    = calculate_relative_volume(market, current_vol)

            # Both need minimum vol conditions (same as original)
            if rel_vol < 2.5 or vol_change < 100.0:
                continue

            coin_data = {
                "market":       market,
                "base":         base,
                "price":        current_price,
                "price_change": price_change,
                "vol_change":   vol_change,
                "rel_vol":      rel_vol,
                "trade_vol":    round(current_vol, 2),
            }

            # 🔔 PUMP INCOMING — volume hai but price abhi kam hai
            if PRE_MIN_PRICE_CHANGE <= price_change <= PRE_MAX_PRICE_CHANGE:
                # Extra check: last 1 hour mein price zyada nahi badi
                recent_change = get_recent_price_change(market)
                coin_data["recent_change"] = recent_change
                if recent_change <= 1.5:  # Last 1 hour mein 1.5% se kam movement
                    incoming.append(coin_data)

            # 🚀 PUMP DETECTED — price abhi actively move kar rahi hai
            elif PUMP_MIN_PRICE_CHANGE <= price_change <= PUMP_MAX_PRICE_CHANGE:
                detected.append(coin_data)

        except Exception as e:
            log.warning(f"Screen error {t.get('market','?')}: {e}")
            continue

    return incoming, detected


# ──────────────────────────────────────────────
# Message Formatters
# ──────────────────────────────────────────────

def format_incoming(coin):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 *PUMP INCOMING* — Upbit\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Coin:*  `{coin['base']} / KRW`\n"
        f"💰 *Price:*  `₩{coin['price']:,.0f}`\n"
        f"📊 *24h Change:*  `{coin['price_change']:+.2f}%`\n"
        f"⏱ *1h Change:*  `{coin.get('recent_change', 0):+.2f}%`\n"
        f"📈 *Vol Change:*  `+{coin['vol_change']:.2f}%`\n"
        f"⚡ *Rel Volume:*  `{coin['rel_vol']:.2f}x`\n"
        f"📦 *24h Volume:*  `{coin['trade_vol']:,.0f} {coin['base']}`\n"
        f"🏦 *Exchange:*  Upbit\n"
        f"🕐 *Time:*  `{now}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Volume build ho rahi hai — pump aa sakta hai!_\n"
        f"#Upbit #KRW #{coin['base']} #PumpIncoming"
    )


def format_detected(coin):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rv_emoji = "⚡" if coin["rel_vol"] >= 5 else "📊"
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 *PUMP DETECTED* — Upbit\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Coin:*  `{coin['base']} / KRW`\n"
        f"💰 *Price:*  `₩{coin['price']:,.0f}`\n"
        f"🟢 *24h Change:*  `{coin['price_change']:+.2f}%`\n"
        f"🔥 *Vol Change:*  `+{coin['vol_change']:.2f}%`\n"
        f"{rv_emoji} *Rel Volume:*  `{coin['rel_vol']:.2f}x`\n"
        f"📦 *24h Volume:*  `{coin['trade_vol']:,.0f} {coin['base']}`\n"
        f"🏦 *Exchange:*  Upbit\n"
        f"🕐 *Time:*  `{now}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 _Pump abhi active hai — chart check karo!_\n"
        f"#Upbit #KRW #{coin['base']} #PumpDetected"
    )


# ──────────────────────────────────────────────
# Telegram Send with Retry
# ──────────────────────────────────────────────

async def send_message(bot, chat_id, text, retries=3):
    for attempt in range(retries):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
            return True
        except TelegramError as e:
            log.warning(f"Send attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(5)
    return False


# ──────────────────────────────────────────────
# Main Loop
# ──────────────────────────────────────────────

async def run_screener():
    request = HTTPXRequest(connect_timeout=30, read_timeout=30)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)

    # Cooldown trackers
    alerted_incoming = {}  # market → last alert time
    alerted_detected = {}

    log.info("🤖 Smart Screener starting...")

    # Connection test
    for attempt in range(5):
        try:
            me = await bot.get_me()
            log.info(f"✅ Connected: @{me.username}")
            break
        except Exception as e:
            log.warning(f"Connect attempt {attempt+1}/5: {e}")
            await asyncio.sleep(10)
    else:
        log.error("❌ Cannot connect to Telegram.")
        return

    # Startup message
    await send_message(
        bot, TELEGRAM_CHAT_ID,
        (
            "🤖 *Upbit Smart Screener — ONLINE* ✅\n\n"
            "📌 *Two Alert Types:*\n\n"
            "🔔 *PUMP INCOMING*\n"
            f"  • Rel Vol ≥ `2.5x`\n"
            f"  • Vol Change ≥ `+100%`\n"
            f"  • Price Change `-2%` to `+2%`\n"
            f"  • Recent 1h move < `1.5%`\n\n"
            "🚀 *PUMP DETECTED*\n"
            f"  • Rel Vol ≥ `2.5x`\n"
            f"  • Vol Change ≥ `+100%`\n"
            f"  • Price Change `+2%` to `+5%`\n\n"
            f"⏱ Scanning every `45s` — 24/7!"
        )
    )

    log.info("🔍 Screener loop started...")

    while True:
        try:
            cycle_start = time.time()
            log.info("── Scan cycle ──")

            markets = get_all_krw_markets()
            if not markets:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                continue

            tickers = get_ticker_data(markets)
            if not tickers:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                continue

            incoming, detected = screen_all_coins(tickers)
            log.info(f"Incoming: {len(incoming)} | Detected: {len(detected)}")

            now_t = time.time()

            # Send PUMP INCOMING alerts
            for coin in incoming:
                mkt  = coin["market"]
                last = alerted_incoming.get(mkt, 0)
                if (now_t - last) >= PRE_COOLDOWN_SEC:
                    ok = await send_message(bot, TELEGRAM_CHAT_ID, format_incoming(coin))
                    if ok:
                        alerted_incoming[mkt] = now_t
                        log.info(f"🔔 INCOMING: {mkt} | RV={coin['rel_vol']} | PC={coin['price_change']}%")
                    await asyncio.sleep(0.5)

            # Send PUMP DETECTED alerts
            for coin in detected:
                mkt  = coin["market"]
                last = alerted_detected.get(mkt, 0)
                if (now_t - last) >= PUMP_COOLDOWN_SEC:
                    ok = await send_message(bot, TELEGRAM_CHAT_ID, format_detected(coin))
                    if ok:
                        alerted_detected[mkt] = now_t
                        log.info(f"🚀 DETECTED: {mkt} | RV={coin['rel_vol']} | PC={coin['price_change']}%")
                    await asyncio.sleep(0.5)

            elapsed    = time.time() - cycle_start
            sleep_time = max(0, CHECK_INTERVAL_SEC - elapsed)
            log.info(f"Cycle: {elapsed:.1f}s | Next: {sleep_time:.0f}s")
            await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            log.info("🛑 Stopped.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run_screener())
