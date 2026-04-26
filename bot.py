import asyncio
import logging
import time
from datetime import datetime
import requests
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ============================================================
#  ██████╗ ██████╗ ███╗   ██╗███████╗██╗ ██████╗
# ██╔════╝██╔═══██╗████╗  ██║██╔════╝██║██╔════╝
# ██║     ██║   ██║██╔██╗ ██║█████╗  ██║██║  ███╗
# ██║     ██║   ██║██║╚██╗██║██╔══╝  ██║██║   ██║
# ╚██████╗╚██████╔╝██║ ╚████║██║     ██║╚██████╔╝
#  ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝ ╚═════╝
#  UPBIT KRW SCREENER BOT — by Your Setup
# ============================================================

# ──────────────────────────────────────────────
# ✅  CONFIG — Sirf yahan changes karo
# ──────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = "8623031794:AAH-bQahzOhMhK-PMGyBMi4ktMmSTOJVovg"   # @BotFather se lo
TELEGRAM_CHAT_ID   = "-1003901790552"     # @userinfobot se lo

# Screening filters
MIN_REL_VOLUME     = 2.5      # Relative Volume minimum
MIN_PRICE_CHANGE   = -2.0     # Price change 24h minimum (%)
MAX_PRICE_CHANGE   = 5.0      # Price change 24h maximum (%)
MIN_VOLUME_CHANGE  = 100.0    # Volume change 24h minimum (%)

# Bot behavior
CHECK_INTERVAL_SEC = 60       # Har kitne seconds mein check kare (60 = 1 min)
ALERT_COOLDOWN_SEC = 3600     # Same coin ko kitne der baad dobara alert kare (1 hour)

# ──────────────────────────────────────────────
# Logging setup
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
# Upbit API functions
# ──────────────────────────────────────────────

UPBIT_BASE = "https://api.upbit.com/v1"

def get_all_krw_markets() -> list[str]:
    """Get all KRW markets from Upbit."""
    url = f"{UPBIT_BASE}/market/all?isDetails=false"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        markets = [m["market"] for m in r.json() if m["market"].startswith("KRW-")]
        log.info(f"Total KRW markets found: {len(markets)}")
        return markets
    except Exception as e:
        log.error(f"Error fetching markets: {e}")
        return []


def get_ticker_data(markets: list[str]) -> list[dict]:
    """Fetch 24h ticker data for given markets (batch of 100 max)."""
    results = []
    batch_size = 100
    for i in range(0, len(markets), batch_size):
        batch = markets[i:i + batch_size]
        params = {"markets": ",".join(batch)}
        try:
            r = requests.get(f"{UPBIT_BASE}/ticker", params=params, timeout=10)
            r.raise_for_status()
            results.extend(r.json())
        except Exception as e:
            log.error(f"Ticker fetch error (batch {i}): {e}")
    return results


def calculate_relative_volume(ticker: dict) -> float:
    """
    Upbit doesn't provide historical avg volume directly.
    We estimate Relative Volume using:
      rel_vol = acc_trade_volume_24h / (acc_trade_volume_24h / (change_rate+1))
    But a more practical approach:
      We compare current volume against the 'typical' volume
      by fetching candle data for past 7 days.
    For efficiency, we use a simpler proxy:
      Upbit provides acc_trade_volume (current period) but no baseline.
    
    PRACTICAL METHOD used here:
      We fetch 7x daily candles and compute average daily volume,
      then compare today's 24h volume against that average.
    """
    market = ticker.get("market", "")
    current_vol = ticker.get("acc_trade_volume_24h", 0)

    if current_vol == 0:
        return 0.0

    try:
        # Get last 7 days of daily candles
        url = f"{UPBIT_BASE}/candles/days"
        params = {"market": market, "count": 8}  # 8 to exclude today partially
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        candles = r.json()

        if len(candles) < 2:
            return 0.0

        # Exclude the most recent candle (today, incomplete) → use [1:]
        past_volumes = [c["candle_acc_trade_volume"] for c in candles[1:]]
        if not past_volumes:
            return 0.0

        avg_vol = sum(past_volumes) / len(past_volumes)
        if avg_vol == 0:
            return 0.0

        rel_vol = current_vol / avg_vol
        return round(rel_vol, 2)

    except Exception as e:
        log.warning(f"RelVol calc error for {market}: {e}")
        return 0.0


def screen_coins(tickers: list[dict]) -> list[dict]:
    """Apply all filters and return matching coins."""
    matched = []

    for t in tickers:
        try:
            market        = t.get("market", "")
            price_change  = round(t.get("signed_change_rate", 0) * 100, 2)  # to %
            trade_vol_24h = t.get("acc_trade_volume_24h", 0)
            prev_vol      = t.get("prev_closing_price", 0)   # proxy
            current_price = t.get("trade_price", 0)

            # ── Filter 1: Price change in range ──
            if not (MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE):
                continue

            # ── Filter 2: Volume change ──
            # Upbit gives acc_trade_volume_24h but no direct prev 24h vol.
            # We use: volume_change = (current_vol - estimated_prev) / estimated_prev * 100
            # Proxy: yesterday's candle volume
            try:
                url = f"{UPBIT_BASE}/candles/days"
                r   = requests.get(url, params={"market": market, "count": 2}, timeout=8)
                r.raise_for_status()
                candles = r.json()
                if len(candles) >= 2:
                    prev_day_vol  = candles[1]["candle_acc_trade_volume"]
                    today_vol_est = candles[0]["candle_acc_trade_volume"]
                    if prev_day_vol > 0:
                        vol_change = ((today_vol_est - prev_day_vol) / prev_day_vol) * 100
                    else:
                        vol_change = 0
                else:
                    vol_change = 0
            except Exception:
                vol_change = 0

            if vol_change < MIN_VOLUME_CHANGE:
                continue

            # ── Filter 3: Relative Volume ──
            rel_vol = calculate_relative_volume(t)
            if rel_vol < MIN_REL_VOLUME:
                continue

            # ── All filters passed ──
            base_currency = market.replace("KRW-", "")
            matched.append({
                "market":       market,
                "base":         base_currency,
                "quote":        "KRW",
                "price":        current_price,
                "price_change": price_change,
                "vol_change":   round(vol_change, 2),
                "rel_vol":      rel_vol,
                "trade_vol":    round(trade_vol_24h, 2),
            })

        except Exception as e:
            log.warning(f"Screening error for {t.get('market','?')}: {e}")
            continue

    return matched


# ──────────────────────────────────────────────
# Telegram message formatter
# ──────────────────────────────────────────────

def format_alert(coin: dict) -> str:
    price_emoji  = "🟢" if coin["price_change"] >= 0 else "🔴"
    vol_emoji    = "🔥" if coin["vol_change"] >= 200 else "📈"
    rv_emoji     = "⚡" if coin["rel_vol"] >= 5 else "📊"
    now          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚨 *SIGNAL DETECTED* — Upbit\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Coin:*  `{coin['base']} / KRW`\n"
        f"💰 *Price:*  `₩{coin['price']:,.0f}`\n"
        f"{price_emoji} *24h Change:*  `{coin['price_change']:+.2f}%`\n"
        f"{vol_emoji} *Vol Change:*  `+{coin['vol_change']:.2f}%`\n"
        f"{rv_emoji} *Rel Volume:*  `{coin['rel_vol']:.2f}x`\n"
        f"📦 *24h Volume:*  `{coin['trade_vol']:,.2f} {coin['base']}`\n"
        f"🏦 *Exchange:*  Upbit\n"
        f"🕐 *Time:*  `{now}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"#Upbit #KRW #{coin['base']} #Screener"
    )
    return msg


# ──────────────────────────────────────────────
# Main bot loop
# ──────────────────────────────────────────────

async def run_screener():
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    alerted: dict[str, float] = {}  # market → last alert timestamp

    # Startup message
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "🤖 *Upbit KRW Screener — ONLINE*\n\n"
                f"📌 Filters Active:\n"
                f"  • Rel Volume  ≥ `{MIN_REL_VOLUME}x`\n"
                f"  • Price Change  `{MIN_PRICE_CHANGE}%` to `{MAX_PRICE_CHANGE}%`\n"
                f"  • Volume Change  ≥ `+{MIN_VOLUME_CHANGE}%`\n"
                f"  • Exchange: Upbit\n"
                f"  • Quote: KRW\n\n"
                f"⏱ Checking every `{CHECK_INTERVAL_SEC}s` — 24/7 active!"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        log.info("✅ Startup message sent to Telegram.")
    except TelegramError as e:
        log.error(f"Startup message failed: {e}")

    log.info("🔍 Screener loop started...")

    while True:
        try:
            cycle_start = time.time()
            log.info("── New scan cycle ──")

            markets = get_all_krw_markets()
            if not markets:
                log.warning("No markets fetched, retrying next cycle.")
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                continue

            tickers = get_ticker_data(markets)
            if not tickers:
                log.warning("No ticker data, retrying next cycle.")
                await asyncio.sleep(CHECK_INTERVAL_SEC)
                continue

            matched = screen_coins(tickers)

            log.info(f"Matched coins this cycle: {len(matched)}")

            for coin in matched:
                mkt   = coin["market"]
                now_t = time.time()
                last  = alerted.get(mkt, 0)

                if (now_t - last) >= ALERT_COOLDOWN_SEC:
                    msg = format_alert(coin)
                    try:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=msg,
                            parse_mode=ParseMode.MARKDOWN
                        )
                        alerted[mkt] = now_t
                        log.info(f"🚨 Alert sent: {mkt} | RV={coin['rel_vol']} | PC={coin['price_change']}% | VC={coin['vol_change']}%")
                    except TelegramError as e:
                        log.error(f"Send failed for {mkt}: {e}")
                    await asyncio.sleep(0.5)   # small delay between messages
                else:
                    remaining = int(ALERT_COOLDOWN_SEC - (now_t - last))
                    log.info(f"⏭ Skipped {mkt} (cooldown: {remaining}s left)")

            elapsed = time.time() - cycle_start
            sleep_time = max(0, CHECK_INTERVAL_SEC - elapsed)
            log.info(f"Cycle done in {elapsed:.1f}s. Next scan in {sleep_time:.0f}s.")
            await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            log.info("🛑 Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run_screener())
