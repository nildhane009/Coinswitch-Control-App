import builtins
import os
import re
import json
import time
import shutil
import textwrap
import threading
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import socketio
from cryptography.hazmat.primitives.asymmetric import ed25519


# ============================================================
# CONFIGURATION
# ============================================================

INTERVAL = "5"          # 5-minute candles

# ================================================================
# MASTER SAFETY SWITCH
#
#   False -> paper trading only. Signals, SL, exits, PnL are all
#            simulated in memory. NOTHING is sent to CoinSwitch.
#   True  -> REAL orders are placed with REAL money on your
#            CoinSwitch PRO futures account (fixed 10x leverage,
#            MARKET orders). Do not flip this to True until you
#            have watched the bot run in paper mode and are
#            comfortable with its behaviour.
# ================================================================
# ============================================================
# ACCOUNT / EXECUTION MODES
# ============================================================
# PAPER_MODE:
#   True  -> simulated trades using paper_capital
#   False -> paper trading disabled
#
# LIVE_MODE:
#   True  -> real CoinSwitch Futures orders
#   False -> live execution disabled
#
# ORDERS_ENABLED:
#   True  -> execution is allowed according to the selected mode
#   False -> no order execution at all
#
# PAPER_MODE and LIVE_MODE must never be True together.
# ============================================================

ORDERS_ENABLED = False
PAPER_MODE = False
LIVE_MODE = False

if PAPER_MODE and LIVE_MODE:
    raise RuntimeError("PAPER_MODE and LIVE_MODE cannot both be True.")



# Leverage applied to every symbol before its first trade (fixed,
# per your instruction). Must be <= the symbol's max_leverage -
# since watchlist_refresh_loop() only keeps symbols whose
# max_leverage is >= TRADE_LEVERAGE, this fixed value is always valid.
TRADE_LEVERAGE = 10

# Maximum allowed loss on trading capital when the fixed SL is hit.
# Raw price risk is multiplied by TRADE_LEVERAGE. >30% is rejected.
MAX_SL_RISK_PCT = 30.0

# LIVE protection / recovery safety
EMERGENCY_SL_PCT = 10.0

# TRAILING STOP
# Profit thresholds are measured as POSITION ROI / CAPITAL PnL, i.e.
# unrealised PnL divided by the actual position margin. This keeps the
# 5% / 10% / 2.5% rules tied to capital rather than raw price movement.
#
# Stage 1: once +5% ROI is reached, trailing is ARMED.
#           If +10% ROI is NOT reached and ROI falls back to +2.5%, exit.
# Stage 2: once +10% ROI is reached, the trailing protection is activated
#           with an initial locked floor at +10% ROI.
#           Above +10%, the existing 70/30 peak ratchet may move the stop
#           higher, but it can never move below the +10% ROI floor.
TRAILING_SL_ENABLED = True
TRAILING_ARM_PCT = 5.0
TRAILING_FALLBACK_PCT = 2.5
TRAILING_ACTIVATION_PCT = 10.0
TRAILING_RETRACEMENT_PCT = 30.0
PROTECTION_VERIFY_SECONDS = 15
PROTECTION_RETRY_SECONDS = 5
POSITION_RECOVERY_INTERVAL = 10
LIVE_POSITION_SYNC_INTERVAL = 2.0
LIVE_ORDER_POLL_SECONDS = 0.75
LIVE_ORDER_POLL_ATTEMPTS = 12
RECOVERY_LOCK = False
LIVE_POSITION_SYNC_THREAD = None

PRINT_INTERVAL = 5.0

# ============================================================
# BACKGROUND OUTPUT ROUTING
#
# Keep the dashboard/keyboard clean on the terminal.
# Main thread + dashboard + keyboard remain visible.
# Other worker-thread prints are written to a persistent log.
# ============================================================

BACKGROUND_LOG_FILE = ".coinswitch_v2_background.log"
VISIBLE_OUTPUT_THREADS = {
    "MainThread",
    "dashboard-keyboard",
    "display_loop",
}

_original_print = builtins.print
_background_print_lock = threading.RLock()


def print(*args, **kwargs):
    """
    Route background-thread console output to a log file while
    keeping dashboard and keyboard output visible on terminal.
    """
    try:
        thread_name = threading.current_thread().name

        if thread_name in VISIBLE_OUTPUT_THREADS:
            return _original_print(*args, **kwargs)

        timestamp = datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S%z"
        )

        try:
            rendered = " ".join(str(arg) for arg in args)
        except Exception:
            rendered = repr(args)

        line = f"[{timestamp}] [{thread_name}] {rendered}"

        # Respect simple newline/end usage while keeping the log readable.
        end = kwargs.get("end", "\n")

        with _background_print_lock:
            with open(
                BACKGROUND_LOG_FILE,
                "a",
                encoding="utf-8",
            ) as log:
                log.write(line + end)
                log.flush()

    except Exception:
        # Output routing must NEVER break trading logic.
        try:
            _original_print(*args, **kwargs)
        except Exception:
            pass



# Dashboard box width (terminal columns). Auto-detects the real terminal
# size (handy on a phone terminal app, e.g. Termux on a Realme P3 5G) and
# clamps it to a comfortable mobile-portrait range so the box never
# overflows or wraps ugly mid-word.
DASHBOARD_MIN_WIDTH = 40
DASHBOARD_MAX_WIDTH = 60

# --------------------------------------------------------------
# WATCHLIST (built from the HFT scanner snapshot - see
# watchlist_refresh_loop() / load_hft_watchlist())
# --------------------------------------------------------------

# How often (seconds) the watchlist is re-scanned.
WATCHLIST_REFRESH_SECONDS = 4 * 60 * 60   # 4 hours


# --------------------------------------------------------------
# CAPITAL ALLOCATION
# --------------------------------------------------------------
# The exchange-reported/effective account capital is the source of
# truth in LIVE_MODE. Only MAX_ALLOCATION_PCT of that capital may be
# deployed as position margin.
#
# Each NEW entry may use at most ENTRY_ALLOCATION_PCT of the
# MAX_ALLOCATION_PCT deployment budget as margin. Example: $1,000
# account -> $500 maximum deployment and at most $50 margin per entry.
# At 10x leverage that $50 margin corresponds to up to $500 notional.
#
# The allocation limits are applied to MARGIN, not full notional.
# --------------------------------------------------------------

TOTAL_CAPITAL = 10000.0        # fallback / initial paper capital
MAX_ALLOCATION_PCT = 50.0      # maximum total MARGIN deployment
ENTRY_ALLOCATION_PCT = 10.0    # maximum MARGIN allocation per new entry
MAX_CONCURRENT_POSITIONS = None  # no fixed maximum position count

# Paper-mode fee rate per executed side, applied to notional.
# Live mode never uses this value; it records exchange-reported fees.
PAPER_FEE_RATE = 0.00065

# C = LOW CAPITAL MODE: remove 50% deployment cap; use min_qty
# and select the highest affordable leverage up to 40x.
LOW_CAPITAL_MODE = False
LOW_CAPITAL_LEVERAGES = (10, 20, 40)

# HFT scanner snapshot produced by hft_final_watchlist_v5.py
HFT_WATCHLIST_FILE = "hft_final_watchlist_v5.json"
HFT_REFRESH_SECONDS = 30.0
HFT_MIN_QUALITY = {"CONFIRMED", "WATCH"}
HFT_ALLOW_WATCH_FALLBACK = True

# Funding protection: no NEW entry inside this blackout window.
# CoinSwitch perpetual funding is treated as a 4-hour cycle here.
# Set these UTC times to the exchange's actual funding schedule if it differs.
FUNDING_SCHEDULE_UTC = [(0, 0), (4, 0), (8, 0), (12, 0), (16, 0), (20, 0)]
FUNDING_BLACKOUT_MINUTES = 5

# Account balance refresh. The execution API balance is the source of truth
# for capital used/available calculations; the old hardcoded 10000 is fallback only.
BALANCE_REFRESH_SECONDS = 30.0

MAX_DEPLOYABLE_CAPITAL = TOTAL_CAPITAL * (MAX_ALLOCATION_PCT / 100.0)

# --------------------------------------------------------------
# WEBSOCKET (candles only - ticker/live price comes from REST)
# --------------------------------------------------------------

WS_URL = "wss://ws.coinswitch.co/"
NAMESPACE = "/exchange_2"
SOCKETIO_PATH = "/pro/realtime-rates-socket/futures/exchange_2"

# --------------------------------------------------------------
# REST API
# --------------------------------------------------------------

BASE_URL = "https://coinswitch.co"

# Fill these in with your own CoinSwitch PRO API key pair
# (Profile -> API Trading on CoinSwitch PRO). Both are hex strings.
API_KEY = os.environ.get("COINSWITCH_API_KEY", "")
SECRET_KEY = os.environ.get("COINSWITCH_SECRET_KEY", "")

# How often (seconds) to poll the REST all-pairs ticker for live
# prices of every symbol in the watchlist.
TICKER_POLL_SECONDS = 2.0


# ============================================================
# WEBSOCKET CLIENT
# ============================================================

sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,
    reconnection_delay=2,
    reconnection_delay_max=10,
)


# ============================================================
# SHARED STATE
# ============================================================

state_lock = threading.Lock()

ws_connected = False

# The symbols we currently want NEW entries on (refreshed every
# WATCHLIST_REFRESH_SECONDS).
watchlist = set()

# Every symbol we've ever subscribed a KLine stream for. This is a
# superset of `watchlist` - a symbol stays here (and keeps getting
# its candles/price updated) even after it drops out of the
# watchlist, for as long as it still has an open position, so an
# open trade is never abandoned mid-flight.
subscribed_symbols = set()

# Per-symbol state. Keys are symbol strings (e.g. "BTCUSDT").
# See make_symbol_state() for the shape of each value.
symbol_states = {}

# Simple trade log (kept in memory since orders are disabled).
# Each entry also carries a "symbol" key.
trade_log = []

# Persistent trading history/state. Paper and live are intentionally separate.
STATE_DIR = Path(os.environ.get("COINSWITCH_STATE_DIR", str(Path.home() / ".coinswitch_v1")))
PAPER_LOG_FILE = STATE_DIR / "paper_trade_log.json"
LIVE_LOG_FILE = STATE_DIR / "live_trade_log.json"
PAPER_STATE_FILE = STATE_DIR / "paper_capital_state.json"
SESSION_STARTED_AT = time.time()

paper_capital = TOTAL_CAPITAL
paper_trade_log = []
live_trade_log = []

last_watchlist_refresh = None

# Per-symbol trading rules from Get Instrument Info: max_leverage
# and min_base_quantity. Refreshed alongside the watchlist.
instrument_info = {}

# Symbols for which we've successfully set TRADE_LEVERAGE (only
# relevant when ORDERS_ENABLED is True - leverage must be set
# before the first order on a symbol).
leverage_set_symbols = set()
configured_leverage_by_symbol = {}

# HFT scanner state. The scanner NEVER creates an entry signal. It only
# confirms/prioritizes an entry generated by the existing 5m crossover.
hft_priority = {}
hft_loaded_at = None
hft_file_mtime = None
entry_priority_symbol = None
entry_priority_score = None

# Account/equity cache used for margin accounting and dashboard display.
account_balance = None
account_available_balance = None
account_balance_updated_at = None


# ============================================================
# HELPERS
# ============================================================

def to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def to_int(value):
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def utc_string(timestamp_ms):
    if timestamp_ms is None:
        return "N/A"

    try:
        dt = datetime.fromtimestamp(
            timestamp_ms / 1000.0,
            tz=timezone.utc,
        )

        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    except Exception:
        return "N/A"


def now_string():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def make_symbol_state():
    """
    Fresh per-symbol state dict. Every symbol we track gets one
    of these (candle history, signal search state, position).
    """

    return {
        "current_candle": None,
        "previous_closed_candle": None,

        "live_price": None,
        "previous_live_price": None,

        "signal": "WAIT",
        "entry_price": None,
        "stop_loss": None,
        "signal_candle_start": None,
        "signal_triggered": False,

        "in_position": False,
        "position_closing": False,   # True while a close order is in flight
        "position_side": None,       # "BUY" or "SELL"
        "position_entry": None,
        "position_sl": None,
        "position_size": None,       # USDT notional (qty * entry price)
        "position_qty": None,        # base-asset quantity (min_qty for the symbol)
        "position_open_time": None,
        "position_margin": None,
        "capital_used_pct": None,
        "scanner_score": None,
        "scanner_quality": None,
        "scanner_bias": None,
        "entry_fee": 0.0,

        # LIVE exchange protection / reconciliation
        "exchange_order_id": None,
        "actual_entry": None,
        "actual_qty": None,
        "actual_leverage": None,
        "strategy_sl": None,
        "emergency_sl": None,
        "active_sl": None,
        "sl_order_id": None,
        "sl_status": "NONE",
        "protection_type": "NONE",
        "protection_verified_at": None,
        "protection_last_check": None,
        "recovery_status": "NORMAL",
        "mark_price": None,
        "exchange_unrealised_pnl": None,
        "liquidation_price": None,
        "sl_distance_pct": None,
        "sl_capital_risk_pct": None,

        # TRAILING STOP state
        "trailing_active": False,
        "trailing_armed": False,
        "trailing_stage": "NONE",
        "trailing_peak": None,
        "trailing_sl": None,
        "trailing_last_order_id": None,
    }


def ensure_symbol_state(symbol):
    """
    Must be called under state_lock. Creates a fresh state entry
    for `symbol` if one doesn't already exist.
    """

    if symbol not in symbol_states:
        symbol_states[symbol] = make_symbol_state()


# ============================================================
# PERSISTENT PAPER/LIVE TRADE STATE
# ============================================================

def _load_json_file(path, default):
    try:
        if not path.exists():
            return default
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data is not None else default
    except Exception as e:
        print(f"[Persistence] Could not load {path}: {e}")
        return default


def _atomic_write_json(path, data):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"[Persistence] Could not save {path}: {e}")
        return False


def load_persistent_state():
    """Restore paper capital and separate paper/live histories on startup."""
    global paper_capital, paper_trade_log, live_trade_log, trade_log

    paper_trade_log = _load_json_file(PAPER_LOG_FILE, [])
    live_trade_log = _load_json_file(LIVE_LOG_FILE, [])
    saved_capital = _load_json_file(PAPER_STATE_FILE, {})

    if isinstance(saved_capital, dict) and to_float(saved_capital.get("paper_capital")) is not None:
        paper_capital = float(saved_capital["paper_capital"])
    else:
        paper_capital = TOTAL_CAPITAL

    # Keep the existing in-memory trade_log API used by the strategy.
    trade_log = []

    print(f"[Persistence] Paper capital: {paper_capital:.6f}")
    print(f"[Persistence] Paper history: {len(paper_trade_log)} trades")
    print(f"[Persistence] Live history : {len(live_trade_log)} trades")


def save_paper_capital():
    _atomic_write_json(PAPER_STATE_FILE, {
        "paper_capital": paper_capital,
        "updated_at": time.time(),
    })


def _fee_number(value):
    value = to_float(value)
    return abs(value) if value is not None else None


def extract_actual_fee(payload):
    """Extract an actual fee/commission amount when the exchange returns it in the order payload."""
    if payload is None:
        return None
    keys = {
        "fee", "fees", "trading_fee", "transaction_fee", "commission",
        "commission_amount", "fee_amount", "executed_fee", "total_fee"
    }
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k).lower() in keys:
                if isinstance(v, dict):
                    for sub in ("amount", "value", "total", "fee", "commission"):
                        n = _fee_number(v.get(sub))
                        if n is not None:
                            return n
                else:
                    n = _fee_number(v)
                    if n is not None:
                        return n
        for v in payload.values():
            n = extract_actual_fee(v)
            if n is not None:
                return n
    elif isinstance(payload, list):
        for item in payload:
            n = extract_actual_fee(item)
            if n is not None:
                return n
    return None


def extract_actual_execution_price(payload):
    """Extract the exchange-reported average execution/fill price."""
    if payload is None:
        return None

    keys = {
        "avg_execution_price",
        "average_execution_price",
        "avg_fill_price",
        "average_fill_price",
        "execution_price",
        "fill_price",
    }

    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k).lower() in keys:
                n = to_float(v)
                if n is not None and n > 0:
                    return n

        for v in payload.values():
            n = extract_actual_execution_price(v)
            if n is not None:
                return n

    elif isinstance(payload, list):
        for item in payload:
            n = extract_actual_execution_price(item)
            if n is not None:
                return n

    return None


def rolling_24h_records(history):
    cutoff = time.time() - 24 * 60 * 60
    return [r for r in history if to_float(r.get("closed_at")) is not None and float(r["closed_at"]) >= cutoff]


def rolling_24h_metrics(history):
    rows = rolling_24h_records(history)
    gross = sum(float(r.get("pnl_usdt") or 0.0) for r in rows)
    fees = sum(float(r.get("fee") or 0.0) for r in rows)
    net = gross - fees
    return rows, gross, fees, net


# ============================================================
# REST API (Ed25519-signed requests)
# ============================================================

def sign_request(method, path, params=None):
    """
    Build the headers and final URL path for an authenticated
    CoinSwitch request (Ed25519 signing), per CoinSwitch PRO's
    API Trading docs.
    """

    method = method.upper()

    if params:
        sep = "&" if "?" in path else "?"
        path = path + sep + urllib.parse.urlencode(params)

    decoded_path = urllib.parse.unquote_plus(path)

    epoch = str(int(time.time() * 1000))

    message = method + decoded_path + epoch

    secret = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(SECRET_KEY)
    )

    signature = secret.sign(message.encode("utf-8")).hex()

    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH": epoch,
    }

    return headers, decoded_path


def fetch_all_pairs_ticker():
    """
    GET /trade/api/v2/futures/all-pairs/ticker

    Returns a dict: { "BTCUSDT": {"price": .., "bid": .., "ask": ..,
    "mark": .., "timestamp": .., "pct24h": ..}, ... } on success,
    or None on any failure. Never raises.
    """

    try:

        headers, path = sign_request(
            "GET",
            "/trade/api/v2/futures/all-pairs/ticker",
            params={"exchange": "EXCHANGE_2"},
        )

        response = requests.get(
            BASE_URL + path,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        payload = response.json()

        raw = payload.get("data")

        if not isinstance(raw, dict):
            return None

        result = {}

        for symbol, ticker in raw.items():

            if not isinstance(ticker, dict):
                continue

            price = to_float(ticker.get("last_price"))

            if price is None:
                continue

            result[symbol] = {
                "price": price,
                "bid": to_float(ticker.get("best_bid_price")),
                "ask": to_float(ticker.get("best_ask_price")),
                "mark": to_float(ticker.get("mark_price")),
                "timestamp": to_int(ticker.get("timestamp")),
                "pct24h": to_float(ticker.get("price_24h_pcnt")),
            }

        return result

    except Exception as e:

        print(f"[REST All-Pairs Ticker Error] {e}")
        return None


def fetch_instrument_info():
    """
    GET /trade/api/v2/futures/instrument_info

    Returns a dict: { "BTCUSDT": {"max_leverage": .., "min_qty": ..}, ... }
    on success, or None on any failure. Never raises.
    """

    try:

        headers, path = sign_request(
            "GET",
            "/trade/api/v2/futures/instrument_info",
            params={"exchange": "EXCHANGE_2"},
        )

        response = requests.get(
            BASE_URL + path,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        payload = response.json()

        raw = payload.get("data")

        if not isinstance(raw, dict):
            return None

        result = {}

        for symbol, info in raw.items():

            if not isinstance(info, dict):
                continue

            max_leverage = to_float(info.get("max_leverage"))
            min_qty = to_float(info.get("min_base_quantity"))

            if max_leverage is None or min_qty is None:
                continue

            result[symbol] = {
                "max_leverage": max_leverage,
                "min_qty": min_qty,
            }

        return result

    except Exception as e:

        print(f"[REST Instrument Info Error] {e}")
        return None


# ============================================================
# REAL ORDER PLACEMENT (only actually called when ORDERS_ENABLED)
# ============================================================


def set_symbol_leverage(symbol, leverage=None):
    """
    Set the requested leverage for a symbol.
    Normal mode defaults to TRADE_LEVERAGE (10x).
    LOW CAPITAL MODE uses the selected 10x/20x/40x candidate.
    """
    try:
        selected = int(float(
            leverage if leverage is not None else TRADE_LEVERAGE
        ))

        if selected <= 0:
            return False

        headers, path = sign_request(
            "POST", "/trade/api/v2/futures/leverage"
        )

        body = {
            "symbol": symbol,
            "exchange": "EXCHANGE_2",
            "leverage": selected,
        }

        response = requests.post(
            BASE_URL + path,
            headers=headers,
            json=body,
            timeout=10,
        )

        response.raise_for_status()

        leverage_set_symbols.add(symbol)
        configured_leverage_by_symbol[symbol] = selected

        print(f"[Leverage] {symbol} set to {selected}x")
        return True

    except Exception as e:
        print(f"[Leverage Error] ({symbol}) {e}")
        return False

def place_market_order(symbol, side, quantity, reduce_only=False):
    """
    POST /trade/api/v2/futures/order (MARKET order).

    side        — "BUY" or "SELL"
    quantity    — base-asset quantity
    reduce_only — True when this order is meant to CLOSE an
                  existing position (never opens a new/opposite one)

    Returns the parsed response dict on success, or None on any
    failure. Never raises.
    """

    try:

        headers, path = sign_request(
            "POST", "/trade/api/v2/futures/order"
        )

        body = {
            "exchange": "EXCHANGE_2",
            "symbol": symbol,
            "side": side,
            "order_type": "MARKET",
            "quantity": quantity,
        }

        if reduce_only:
            body["reduce_only"] = True

        response = requests.post(
            BASE_URL + path,
            headers=headers,
            json=body,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        print(f"[Order] {symbol} {side} qty={quantity} reduce_only={reduce_only} -> {data}")

        return data

    except requests.Timeout as e:

        # A timeout after submission is NOT equivalent to "order failed".
        # The exchange may have accepted/executed the order. Never resend
        # automatically. Reconciliation must determine the real state.
        print(f"[Order AMBIGUOUS] ({symbol} {side} qty={quantity}) timeout: {e}")
        return {"_ambiguous": True, "_error": "timeout"}

    except Exception as e:

        print(f"[Order Error] ({symbol} {side} qty={quantity}) {e}")
        if "response" in locals():
            print(f"[Order HTTP] status={response.status_code}")
            print(f"[Order Response] {response.text}")
        return None


# ============================================================
# HISTORICAL KLINES (REST) - used to seed a newly-added symbol's
# "previous closed candle" immediately, instead of waiting for
# it to arrive organically over the live WebSocket stream.
# ============================================================

def fetch_recent_klines(symbol, limit=3):
    """
    GET /trade/api/v2/futures/klines

    Returns a list of candle dicts (sorted oldest -> newest), each
    with: start_time, close_time, open, high, low, close, volume.
    Returns None on any failure. Never raises.
    """

    try:

        headers, path = sign_request(
            "GET",
            "/trade/api/v2/futures/klines",
            params={
                "symbol": symbol.lower(),
                "exchange": "EXCHANGE_2",
                "interval": INTERVAL,
                "limit": limit,
            },
        )

        response = requests.get(
            BASE_URL + path,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        payload = response.json()

        raw = payload.get("data")

        if not isinstance(raw, list):
            return None

        candles = []

        for row in raw:

            start_time = to_int(row.get("start_time"))
            close_time = to_int(row.get("close_time"))
            o = to_float(row.get("o"))
            h = to_float(row.get("h"))
            l = to_float(row.get("l"))
            c = to_float(row.get("c"))
            v = to_float(row.get("volume"))

            if start_time is None or o is None or h is None or l is None or c is None:
                continue

            candles.append(
                {
                    "start_time": start_time,
                    "close_time": close_time,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                }
            )

        candles.sort(key=lambda x: x["start_time"])

        return candles

    except Exception as e:

        print(f"[REST Klines Error] ({symbol}) {e}")
        return None


def seed_symbol_history(symbol):
    """
    Fetches the last few candles for `symbol` via REST and, if the
    symbol doesn't already have candle data (from the live
    WebSocket stream), seeds its previous_closed_candle (and, if
    available, current_candle too) so the strategy can evaluate
    signals immediately instead of waiting 5-10 minutes for two
    live candles to pass.
    """

    candles = fetch_recent_klines(symbol, limit=3)

    if not candles:
        return

    now_ms = int(time.time() * 1000)

    # A candle counts as fully closed only if its close_time has
    # actually passed - never trust the last row blindly, since
    # some APIs include the still-forming candle as the last row.
    closed = [
        c for c in candles
        if c["close_time"] is not None and c["close_time"] <= now_ms
    ]

    if not closed:
        return

    previous = closed[-1]

    # Whatever candle (if any) starts after `previous` is the
    # current, still-forming candle.
    current_candidate = None

    for c in candles:
        if c["start_time"] > previous["start_time"]:
            current_candidate = c
            break

    def strip(c):
        return {
            "start_time": c["start_time"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c["volume"],
        }

    with state_lock:

        if symbol not in symbol_states:
            return

        state = symbol_states[symbol]

        # Only seed if the live WebSocket hasn't already populated
        # this - never overwrite live data with a stale REST call.
        if state["previous_closed_candle"] is None:
            state["previous_closed_candle"] = strip(previous)

        if state["current_candle"] is None and current_candidate is not None:
            state["current_candle"] = strip(current_candidate)

    print(
        f"[Seed] {symbol}: previous candle loaded from REST "
        f"(O:{previous['open']:.6f} H:{previous['high']:.6f} "
        f"L:{previous['low']:.6f} C:{previous['close']:.6f})"
    )


# ============================================================
# WATCHLIST (Top Gainers / Top Losers)
# ============================================================

def fetch_futures_balance():
    """
    Fetch the actual CoinSwitch Futures USDT wallet balance.

    Returns:
        {
            "total": total USDT balance,
            "available": available USDT balance,
        }

    The Futures wallet endpoint is:
        GET /trade/api/v2/futures/wallet_balance
    """

    try:
        headers, path = sign_request(
            "GET",
            "/trade/api/v2/futures/wallet_balance",
        )

        response = requests.get(
            BASE_URL + path,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        data = payload.get("data")

        if not isinstance(data, dict):
            print("[Futures Balance Error] Invalid wallet response")
            return None

        base_asset_balances = data.get("base_asset_balances")

        if not isinstance(base_asset_balances, list):
            print("[Futures Balance Error] base_asset_balances not found")
            return None

        for item in base_asset_balances:
            if not isinstance(item, dict):
                continue

            if str(item.get("base_asset", "")).upper() != "USDT":
                continue

            balances = item.get("balances")

            if not isinstance(balances, dict):
                print("[Futures Balance Error] USDT balances not found")
                return None

            total = to_float(
                balances.get("total_balance")
            )

            available = to_float(
                balances.get("total_available_balance")
            )

            if total is None:
                print("[Futures Balance Error] total_balance not found")
                return None

            if available is None:
                print(
                    "[Futures Balance Error] "
                    "total_available_balance not found"
                )
                return None

            blocked = to_float(
                balances.get("total_blocked_balance")
            )
            position_margin = to_float(
                balances.get("total_position_margin")
            )
            open_order_margin = to_float(
                balances.get("total_open_order_margin")
            )

            print(
                f"[Futures Balance] "
                f"USDT total={total:.6f} "
                f"available={available:.6f} "
                f"blocked={blocked if blocked is not None else 0.0:.6f} "
                f"position_margin={position_margin if position_margin is not None else 0.0:.6f} "
                f"open_order_margin={open_order_margin if open_order_margin is not None else 0.0:.6f}"
            )

            return {
                "total": total,
                "available": available,
            }

        print("[Futures Balance Error] USDT wallet row not found")
        return None

    except Exception as e:
        print(f"[Futures Balance Error] {e}")

        if "response" in locals():
            print(f"[Futures Balance HTTP] status={response.status_code}")
            print(f"[Futures Balance Response] {response.text}")

        return None

def refresh_account_balance():
    global account_balance, account_available_balance, account_balance_updated_at
    data = fetch_futures_balance()
    if data is None:
        return False
    with state_lock:
        account_balance = data["total"]
        account_available_balance = data["available"]
        account_balance_updated_at = time.time()
    return True


def effective_total_capital():
    if LIVE_MODE:
        value = account_balance
        return value if value is not None and value > 0 else 0.0

    if PAPER_MODE:
        return paper_capital

    return 0.0


def effective_available_capital():
    if LIVE_MODE:
        value = account_available_balance
        if value is not None and value >= 0:
            return value
        return effective_total_capital()

    if PAPER_MODE:
        return paper_capital

    return 0.0



def max_deployable_margin():
    capital = effective_total_capital()
    if LOW_CAPITAL_MODE:
        return capital
    return capital * (MAX_ALLOCATION_PCT / 100.0)

def funding_status(now=None):
    """Return funding blackout status using the configured 4-hour schedule."""
    if now is None:
        now = datetime.now(timezone.utc)

    candidates = []
    for day_offset in (-1, 0, 1):
        day = now.date()
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        base = base + timedelta(days=day_offset)
        for hour, minute in FUNDING_SCHEDULE_UTC:
            candidates.append(base.replace(hour=hour, minute=minute, second=0, microsecond=0))

    nearest = min(candidates, key=lambda x: abs((x - now).total_seconds()))
    delta_seconds = (now - nearest).total_seconds()
    blackout_seconds = FUNDING_BLACKOUT_MINUTES * 60
    blocked = abs(delta_seconds) <= blackout_seconds
    next_funding = min((x for x in candidates if x > now), default=None)
    return blocked, nearest, next_funding, delta_seconds


def load_hft_watchlist(force=False):
    """Load the scanner snapshot. Scanner data is an entry-priority layer only."""
    global hft_priority, hft_loaded_at, hft_file_mtime
    path = Path(HFT_WATCHLIST_FILE)
    if not path.exists():
        return False

    try:
        mtime = path.stat().st_mtime
        if not force and hft_file_mtime == mtime:
            return True

        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for key in ("final_long", "final_short", "top_10_long", "top_10_short"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
        if not rows:
            rows = payload.get("all_results") or []
        new_priority = {}

        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue

            momentum = row.get("momentum") or {}
            market = row.get("market") or {}
            orderbook = row.get("orderbook") or {}
            execution = row.get("execution") or {}

            move_5m = to_float(momentum.get("momentum_5m_pct")) or 0.0
            move_1m = to_float(momentum.get("momentum_1m_pct")) or 0.0
            score = to_float(row.get("score")) or 0.0
            quality = str(row.get("quality", "")).upper()
            if not quality:
                quality = "CONFIRMED" if row.get("confirmed") else "WATCH"

            # V5 uses direction-adjusted weighted orderbook strength in WOB.
            wob = to_float(row.get("weighted_orderbook"))
            if wob is None:
                wob = to_float(row.get("wob"))
            if wob is None:
                wob = 0.0

            bias = str(row.get("signal", "")).upper()
            if bias not in {"LONG", "SHORT"}:
                bias = "LONG" if move_5m > 0 else "SHORT" if move_5m < 0 else "MIXED"

            new_priority[symbol] = {
                "score": score,
                "quality": quality,
                "bias": bias,
                "move_5m": move_5m,
                "move_1m": move_1m,
                "wob": wob,
                "turnover_24h": to_float(market.get("turnover_24h")) or 0.0,
                "execution_score": to_float(execution.get("execution_score")) or 0.0,
            }

        with state_lock:
            hft_priority = new_priority
            hft_loaded_at = time.time()
            hft_file_mtime = mtime
        return True

    except Exception as e:
        print(f"[HFT Scanner] Load error: {e}")
        return False


def hft_entry_gate(symbol, side):
    """Scanner confirmation/priority gate. Does not create signals."""
    info = hft_priority.get(symbol)
    if not info:
        return False, -1.0, "NO_SCANNER_DATA"

    quality = info["quality"]
    if quality not in HFT_MIN_QUALITY:
        return False, info["score"], f"QUALITY_{quality or 'UNKNOWN'}"

    expected_bias = "LONG" if side == "BUY" else "SHORT"
    if info["bias"] not in {expected_bias}:
        return False, info["score"], f"SCANNER_CONFLICT_{info['bias']}"

    if not HFT_ALLOW_WATCH_FALLBACK and quality != "CONFIRMED":
        return False, info["score"], "WATCH_NOT_ALLOWED"

    return True, info["score"], quality


def hft_scanner_loop():
    while True:
        load_hft_watchlist()
        time.sleep(HFT_REFRESH_SECONDS)


def balance_poll_loop():
    while True:
        if LIVE_MODE:
            refresh_account_balance()
        time.sleep(BALANCE_REFRESH_SECONDS)


def subscribe_kline(symbol):
    """
    Subscribes to the KLine WebSocket stream for `symbol` at
    INTERVAL minutes, and marks it as subscribed. Safe to call
    more than once for the same symbol (subscribing again is a
    harmless no-op on the server side).
    """

    try:

        sio.emit(
            "FETCH_CANDLESTICK_CS_PRO",
            {
                "event": "subscribe",
                "pair": f"{symbol}_{INTERVAL}",
            },
            namespace=NAMESPACE,
        )

    except Exception as e:

        print(f"[WebSocket] Kline subscribe error ({symbol}): {e}")


def subscribe_ticker(symbol):
    """
    Subscribes to the official Futures Ticker WebSocket stream
    (event FETCH_TICKER_INFO_CS_PRO) for `symbol`. Unlike KLines,
    the Ticker event uses the plain BASEUSDT pair (no interval
    suffix). Safe to call more than once for the same symbol.

    This runs ALONGSIDE the existing REST ticker_poll_loop() (which
    keeps polling fetch_all_pairs_ticker() as before) rather than
    replacing it - WS pushes are event-driven and typically faster,
    REST polling stays as an always-on fallback in case the socket
    drops or a particular symbol's WS push is delayed.
    """

    try:

        sio.emit(
            "FETCH_TICKER_INFO_CS_PRO",
            {
                "event": "subscribe",
                "pair": symbol,
            },
            namespace=NAMESPACE,
        )

    except Exception as e:

        print(f"[WebSocket] Ticker subscribe error ({symbol}): {e}")


def watchlist_refresh_loop():
    """Build the active watchlist from the HFT scanner snapshot."""
    global watchlist, last_watchlist_refresh, instrument_info

    while True:
        load_hft_watchlist(force=True)
        instrument_data = fetch_instrument_info()

        if instrument_data:
            # Only symbols present in the scanner snapshot are eligible.
            combined = sorted(
                hft_priority.keys(),
                key=lambda s: (
                    0 if hft_priority[s]["quality"] == "CONFIRMED" else 1,
                    -hft_priority[s]["score"],
                ),
            )

            combined = [
                s for s in combined
                if instrument_data.get(s, {}).get("max_leverage", 0) >= TRADE_LEVERAGE
            ]

            with state_lock:
                instrument_info = instrument_data
                new_symbols = [s for s in combined if s not in subscribed_symbols]
                for symbol in new_symbols:
                    ensure_symbol_state(symbol)
                    subscribed_symbols.add(symbol)
                watchlist = set(combined)

            for symbol in new_symbols:
                subscribe_kline(symbol)
                subscribe_ticker(symbol)
                seed_symbol_history(symbol)
                time.sleep(0.5)
                if LIVE_MODE and ORDERS_ENABLED:
                    success = set_symbol_leverage(symbol)
                    if success:
                        with state_lock:
                            leverage_set_symbols.add(symbol)

            last_watchlist_refresh = time.time()
            print()
            print("*" * 68)
            print(">>> HFT WATCHLIST REFRESHED <<<")
            print("*" * 68)
            print(f"Scanner file       : {HFT_WATCHLIST_FILE}")
            print(f"Scanner candidates : {len(hft_priority)}")
            print(f"Active watchlist   : {len(combined)}")
            print(f"New subscriptions  : {len(new_symbols)}")
            print("*" * 68)
        else:
            print("[Watchlist] Instrument info unavailable; keeping current watchlist.")

        time.sleep(HFT_REFRESH_SECONDS)


# ============================================================
# POSITION MANAGEMENT
# ============================================================
def calculate_raw_pnl_pct(entry, price, side):
    """Return raw price-movement PnL percentage before leverage."""
    if entry is None or entry == 0 or price is None:
        return 0.0
    if side == "BUY":
        return (price - entry) / entry * 100.0
    if side == "SELL":
        return (entry - price) / entry * 100.0
    return 0.0


def calculate_leveraged_pnl_pct(entry, price, side, leverage=None, margin=None, position_size=None):
    """
    Return position ROI/PnL percentage using actual exchange position data.

    TRADE_LEVERAGE is intentionally NOT used here.
    It is only the entry leverage setting.

    For an existing position:
      ROI % = unrealized PnL / actual position margin * 100

    When actual exchange margin is unavailable, fall back to
    position_size / actual leverage.
    """
    raw_pct = calculate_raw_pnl_pct(entry, price, side)

    if margin is not None:
        try:
            margin = float(margin)
            if margin > 0:
                if position_size is None:
                    position_size = float(entry) * 0.0
                if position_size is not None and float(position_size) > 0:
                    unrealized_pnl = float(position_size) * raw_pct / 100.0
                    return (unrealized_pnl / margin) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    try:
        actual_leverage = float(leverage) if leverage is not None else 0.0
        if actual_leverage > 0:
            return raw_pct * actual_leverage
    except (TypeError, ValueError):
        pass

    return 0.0



def sl_risk_allowed(entry, sl, leverage=None, symbol=None, side=None):
    """Allow entry only when leveraged SL risk is <= MAX_SL_RISK_PCT."""
    if entry is None or sl is None or entry <= 0:
        return False

    lev = float(leverage or TRADE_LEVERAGE)
    if lev <= 0:
        return False

    raw_risk_pct = abs(entry - sl) / entry * 100.0
    capital_risk_pct = raw_risk_pct * lev

    if capital_risk_pct > MAX_SL_RISK_PCT:
        label = f"{symbol} {side}" if symbol and side else (symbol or side or "trade")
        print(
            f"[ENTRY AVOIDED] {label}: SL capital risk {capital_risk_pct:.3f}% "
            f"> max {MAX_SL_RISK_PCT:.3f}% | raw price risk {raw_risk_pct:.3f}% | "
            f"leverage {lev:g}x | entry {entry:.8f} | SL {sl:.8f}"
        )
        return False

    return True

def count_open_positions():
    """Must be called under state_lock."""

    return sum(1 for s in symbol_states.values() if s["in_position"])


def total_deployed_notional():
    """Must be called under state_lock. Sum of notional (USDT) value
    across all currently open positions."""

    return sum(
        s["position_size"] or 0.0
        for s in symbol_states.values()
        if s["in_position"]
    )


def total_deployed_margin():
    """
    Must be called under state_lock.

    Sum the ACTUAL margin already deployed by every open position.

    LIVE:
      - Prefer exchange-reported position_margin.
      - If unavailable, fall back to position_size / actual_leverage.

    PAPER:
      - Use the simulated position_margin.

    This deliberately does NOT divide all positions by TRADE_LEVERAGE,
    because recovered/live positions may have exchange-reported leverage
    and margin that differ from the entry configuration.
    """

    total = 0.0

    for state in symbol_states.values():
        if not state.get("in_position"):
            continue

        margin = state.get("position_margin")

        try:
            margin = float(margin) if margin is not None else None
        except (TypeError, ValueError):
            margin = None

        if margin is not None and margin > 0:
            total += margin
            continue

        position_size = state.get("position_size")

        try:
            position_size = (
                float(position_size)
                if position_size is not None
                else None
            )
        except (TypeError, ValueError):
            position_size = None

        actual_leverage = state.get("actual_leverage")

        try:
            actual_leverage = (
                float(actual_leverage)
                if actual_leverage is not None
                else None
            )
        except (TypeError, ValueError):
            actual_leverage = None

        if (
            position_size is not None
            and position_size > 0
            and actual_leverage is not None
            and actual_leverage > 0
        ):
            total += position_size / actual_leverage

    return total


def entry_margin_allocation():
    """Return the maximum margin budget for one new entry.

    NORMAL:
      Uses ENTRY_ALLOCATION_PCT of the 50% deployment budget.

    LOW_CAPITAL_MODE:
      The 50% deployment cap is removed. One entry may use the
      full effective capital, subject to the remaining deployed
      margin and the selected leverage feasibility checks.
    """
    capital = effective_total_capital()
    if capital <= 0:
        return 0.0

    if LOW_CAPITAL_MODE:
        return capital

    max_deployment = capital * (MAX_ALLOCATION_PCT / 100.0)
    return max_deployment * (ENTRY_ALLOCATION_PCT / 100.0)



def can_afford_new_position(symbol, price, leverage=None):
    lev = float(leverage or TRADE_LEVERAGE)
    if lev <= 0 or price is None or price <= 0:
        return False

    info = instrument_info.get(symbol)
    if not info or info.get("min_qty") is None:
        return False

    min_qty = to_float(info.get("min_qty"))
    if min_qty is None or min_qty <= 0:
        return False

    prospective_notional = min_qty * price
    prospective_margin = prospective_notional / lev

    deployed_margin = total_deployed_margin()
    max_margin = max_deployable_margin()

    return (
        deployed_margin + prospective_margin
        <= max_margin + 1e-12
    )

def _recursive_dicts(value):
    """Yield every nested dict inside an API payload."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _recursive_dicts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _recursive_dicts(v)


def _first_number(payload, keys):
    wanted = {str(k).lower() for k in keys}
    for obj in _recursive_dicts(payload):
        for k, v in obj.items():
            if str(k).lower() in wanted:
                n = to_float(v)
                if n is not None:
                    return n
    return None


def _first_text(payload, keys):
    wanted = {str(k).lower() for k in keys}
    for obj in _recursive_dicts(payload):
        for k, v in obj.items():
            if str(k).lower() in wanted and v not in (None, ""):
                return str(v)
    return None


def _extract_order_id(payload):
    return _first_text(
        payload,
        ("order_id", "orderId", "id", "client_order_id", "clientOrderId"),
    )


def _extract_position_from_payload(payload, symbol):
    """Normalize a matching exchange position without assuming one payload shape."""
    symbol = str(symbol).upper()

    for obj in _recursive_dicts(payload):
        obj_symbol = str(
            obj.get("symbol")
            or obj.get("instrument")
            or obj.get("market")
            or ""
        ).upper()

        if obj_symbol and obj_symbol != symbol:
            continue

        qty = _first_number(
            obj,
            ("position_size", "positionSize", "size", "quantity", "qty")
        )
        entry = _first_number(
            obj,
            ("avg_entry_price", "average_entry_price", "entry_price", "entryPrice")
        )

        if qty is None and entry is None:
            continue

        side = _first_text(
            obj,
            ("side", "position_side", "positionSide", "direction")
        )

        leverage = _first_number(
            obj,
            ("leverage", "actual_leverage", "position_leverage")
        )
        margin = _first_number(
            obj,
            ("position_margin", "margin", "initial_margin")
        )
        mark = _first_number(
            obj,
            ("mark_price", "markPrice", "current_price")
        )
        liquidation = _first_number(
            obj,
            ("liquidation_price", "liquidationPrice", "liq_price")
        )

        # Ignore obvious zero/closed position records.
        if qty is not None and abs(qty) <= 0:
            continue

        return {
            "symbol": symbol,
            "side": side,
            "qty": abs(qty) if qty is not None else None,
            "entry": entry,
            "leverage": leverage,
            "margin": margin,
            "mark": mark,
            "liquidation": liquidation,
            "raw": obj,
        }

    return None


def _extract_order_rows(payload):
    rows = []
    for obj in _recursive_dicts(payload):
        if any(
            k in obj
            for k in (
                "order_id",
                "orderId",
                "order_type",
                "orderType",
                "trigger_price",
                "triggerPrice",
            )
        ):
            rows.append(obj)
    return rows


def _order_is_stop_protection(
    order,
    symbol,
    side,
    trigger_price,
    position_entry=None,
):
    """
    Validate an exchange-side reduce-only STOP as protective.

    Protection is intentionally direction-aware:
      LONG  -> lower trigger protects the position.
      SHORT -> higher trigger protects the position.

    An existing STOP is accepted when it is already at least as protective
    as the requested trigger. This prevents recovery from creating duplicate
    STOP orders when a stricter exchange-side STOP already exists.
    """
    if not isinstance(order, dict):
        return False

    osymbol = str(
        order.get("symbol")
        or order.get("instrument")
        or ""
    ).upper()

    if osymbol and osymbol != str(symbol).upper():
        return False

    order_type = str(
        order.get("order_type")
        or order.get("orderType")
        or ""
    ).upper()

    if "STOP" not in order_type:
        return False

    order_side = str(order.get("side") or "").upper()
    if order_side and order_side != side:
        return False

    reduce_only = order.get("reduce_only")
    if reduce_only is None:
        reduce_only = order.get("reduceOnly")

    if reduce_only is False:
        return False

    actual_trigger = _first_number(
        order,
        ("trigger_price", "triggerPrice", "stop_price", "stopPrice")
    )

    if actual_trigger is None:
        return False

    requested_trigger = float(trigger_price)
    actual_trigger = float(actual_trigger)

    tolerance = max(abs(requested_trigger) * 0.00001, 1e-10)

    # If entry is available, first require the STOP to be on the
    # protective side of the actual entry.
    if position_entry is not None:
        try:
            entry = float(position_entry)

            if side == "SELL":
                # Closing a LONG with SELL STOP: trigger must be below entry.
                if actual_trigger >= entry + tolerance:
                    return False

            elif side == "BUY":
                # Closing a SHORT with BUY STOP: trigger must be above entry.
                if actual_trigger <= entry - tolerance:
                    return False

        except (TypeError, ValueError):
            return False

    # Existing protection may be stricter than the requested trigger.
    if side == "SELL":
        # LONG protection: trigger must remain below entry.
        # A higher/equal trigger is closer to entry and therefore
        # provides at least as much protection as the requested SL.
        return actual_trigger >= requested_trigger - tolerance

    if side == "BUY":
        # SHORT protection: trigger must remain above entry.
        # A lower/equal trigger is reached earlier on an adverse rise
        # and therefore provides at least as much protection.
        return actual_trigger <= requested_trigger + tolerance

    return False


def _extract_position_payload(payload, symbol):
    return _extract_position_from_payload(payload, symbol)


def _live_position_snapshot(symbol):
    api_ok, payload = _get_live_positions_with_status(symbol)

    # API/network/signing failure:
    # position state is UNKNOWN, so never assume it is closed.
    if not api_ok:
        return None

    pos = _extract_position_payload(payload, symbol)

    # API succeeded and returned no live position for this symbol.
    # This is a confirmed external/manual close.
    if pos is None:
        return {
            "_position_closed": True,
            "symbol": str(symbol).upper(),
        }

    # Normalize exchange position side to the bot's internal convention:
    # BUY = LONG, SELL = SHORT.
    exchange_side = str(pos.get("side") or "").upper()
    if exchange_side in ("LONG", "BUY"):
        exchange_side = "BUY"
    elif exchange_side in ("SHORT", "SELL"):
        exchange_side = "SELL"
    else:
        raw_qty = pos.get("qty")
        if raw_qty is not None:
            try:
                exchange_side = "BUY" if float(raw_qty) > 0 else "SELL"
            except (TypeError, ValueError):
                exchange_side = ""

    if exchange_side in ("BUY", "SELL"):
        pos["side"] = exchange_side

    return pos


def place_exchange_stop_loss(symbol, position_side, trigger_price, quantity):
    """
    Place the primary exchange-side protective STOP_MARKET.
    BUY position -> SELL stop.
    SELL position -> BUY stop.
    """
    if not LIVE_MODE or not ORDERS_ENABLED:
        return None

    try:
        close_side = "SELL" if position_side == "BUY" else "BUY"

        headers, path = sign_request(
            "POST", "/trade/api/v2/futures/order"
        )

        body = {
            "exchange": "EXCHANGE_2",
            "symbol": str(symbol).upper(),
            "side": close_side,
            "order_type": "STOP_MARKET",
            "quantity": 0,
            "trigger_price": float(trigger_price),
            "reduce_only": True,
        }

        response = requests.post(
            BASE_URL + path,
            headers=headers,
            json=body,
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()
        order_id = _extract_order_id(payload)

        print(
            f"[PROTECTION] {symbol} STOP_MARKET "
            f"side={close_side} trigger={trigger_price:.8f} "
            f"qty=0 reduce_only=True -> {payload}"
        )

        return {
            "order_id": order_id,
            "trigger_price": float(trigger_price),
            "side": close_side,
            "raw": payload,
        }

    except requests.Timeout as exc:
        print(f"[PROTECTION AMBIGUOUS] {symbol}: STOP_MARKET timeout: {exc}")
        return {"ambiguous": True}

    except Exception as exc:
        print(f"[PROTECTION ERROR] {symbol}: {exc}")
        if "response" in locals():
            print(f"[PROTECTION HTTP] status={response.status_code}")
            print(f"[PROTECTION RESPONSE] {response.text}")
        return None


def verify_exchange_stop_loss(symbol, position_side, trigger_price):
    """Verify a reduce-only STOP order exists at the expected trigger."""
    payload = get_live_open_orders(symbol=symbol, limit=50)
    if payload is None:
        return None

    close_side = "SELL" if position_side == "BUY" else "BUY"

    position = _live_position_snapshot(symbol)
    position_entry = position.get("entry") if position else None

    for order in _extract_order_rows(payload):
        if _order_is_stop_protection(
            order,
            symbol,
            close_side,
            trigger_price,
            position_entry=position_entry,
        ):
            return {
                "verified": True,
                "order_id": _extract_order_id(order),
                "trigger_price": order.get("trigger_price"),
                "raw": order,
            }

    return {
        "verified": False,
        "order_id": None,
    }


def _emergency_sl(entry, side, qty=None, margin=None):
    """
    Emergency SL fallback.

    EMERGENCY_SL_PCT is the maximum price-distance target.
    Actual capital risk is capped by MAX_SL_RISK_PCT whenever
    actual position quantity and margin are available.
    """
    entry = to_float(entry)
    qty = to_float(qty)
    margin = to_float(margin)

    if entry is None or entry <= 0:
        return None

    fixed_distance = entry * (EMERGENCY_SL_PCT / 100.0)

    if qty is not None and qty > 0 and margin is not None and margin > 0:
        max_loss = margin * (MAX_SL_RISK_PCT / 100.0)
        max_distance = max_loss / abs(qty)
        distance = min(fixed_distance, max_distance)
    else:
        distance = fixed_distance

    if side == "BUY":
        return entry - distance

    if side == "SELL":
        return entry + distance

    return None


def _sl_crossed(entry, side, sl, price):
    if entry is None or sl is None or price is None:
        return False
    if side == "BUY":
        return price <= sl
    return price >= sl


def _actual_sl_risk_pct(entry, sl, qty, margin):
    if not entry or not sl or not qty or not margin:
        return None

    loss = abs(entry - sl) * abs(qty)
    return (loss / margin) * 100.0


def _clear_local_position_state(symbol, recovery_status="EXTERNALLY_CLOSED"):
    """
    Centralized local position reset.

    IMPORTANT:
    This only clears LOCAL runtime state.
    It must be called only after the exchange has successfully
    confirmed that the position no longer exists.

    API failure must NEVER call this helper.
    """
    old_sl_order_id = None

    with state_lock:
        state = symbol_states.get(symbol)

        if not state:
            return None

        old_sl_order_id = state.get("sl_order_id")

        for key in (
            "in_position",
            "position_closing",
            "position_side",
            "position_entry",
            "position_sl",
            "position_size",
            "position_qty",
            "position_open_time",
            "position_margin",
            "capital_used_pct",
            "scanner_score",
            "scanner_quality",
            "scanner_bias",
            "entry_fee",
            "exchange_order_id",
            "actual_entry",
            "actual_qty",
            "actual_leverage",
            "strategy_sl",
            "emergency_sl",
            "active_sl",
            "sl_order_id",
            "sl_status",
            "protection_type",
            "protection_verified_at",
            "protection_last_check",
            "mark_price",
            "liquidation_price",
            "sl_distance_pct",
            "sl_capital_risk_pct",
            "trailing_active",
            "trailing_armed",
            "trailing_stage",
            "trailing_peak",
            "trailing_sl",
            "trailing_last_order_id",
        ):
            state[key] = (
                False
                if key in ("in_position", "position_closing", "trailing_active")
                else None
            )

        state["sl_status"] = "NONE"
        state["protection_type"] = "NONE"
        state["recovery_status"] = recovery_status

    return old_sl_order_id


def _finalize_external_position_close(symbol, recovery_status="EXTERNALLY_CLOSED"):
    """
    Clear a locally remembered position only after exchange-side
    reconciliation has positively confirmed zero/open-position absence.
    """
    old_sl_order_id = _clear_local_position_state(
        symbol,
        recovery_status=recovery_status,
    )

    print(
        f"[POSITION SYNC] {symbol}: exchange confirms position CLOSED. "
        f"Local position state cleared."
    )

    # Best-effort cleanup of an old protective STOP.
    if old_sl_order_id:
        try:
            cancel_order(symbol, old_sl_order_id)
        except Exception as exc:
            print(
                f"[POSITION SYNC] {symbol}: old SL cleanup warning: {exc}"
            )

    return True


def _set_live_state_from_exchange(symbol, pos):
    """
    Update local live-position state from an exchange position.

    Accepts either:
      1) normalized position data:
         side / qty / entry / margin / leverage / mark / liquidation
      2) raw CoinSwitch futures positions row:
         position_side / position_size / avg_entry_price /
         position_margin / leverage / mark_price /
         liquidation_price / unrealised_pnl

    Exchange position data is the source of truth.
    """

    with state_lock:
        state = ensure_symbol_state(symbol) if symbol in symbol_states else None

        if state is None:
            symbol_states[symbol] = make_symbol_state()
            state = symbol_states[symbol]

        # --------------------------------------------------------
        # Normalize raw CoinSwitch fields when present.
        # --------------------------------------------------------
        exchange_side = str(
            pos.get("side")
            or pos.get("position_side")
            or pos.get("positionSide")
            or ""
        ).upper()

        if exchange_side in ("LONG", "BUY"):
            exchange_side = "BUY"
        elif exchange_side in ("SHORT", "SELL"):
            exchange_side = "SELL"

        raw_qty = (
            pos.get("qty")
            if pos.get("qty") is not None
            else pos.get("position_size")
        )

        raw_entry = (
            pos.get("entry")
            if pos.get("entry") is not None
            else pos.get("avg_entry_price")
        )

        raw_margin = (
            pos.get("margin")
            if pos.get("margin") is not None
            else pos.get("position_margin")
        )

        raw_leverage = (
            pos.get("leverage")
            if pos.get("leverage") is not None
            else pos.get("actual_leverage")
        )

        raw_mark = (
            pos.get("mark")
            if pos.get("mark") is not None
            else (
                pos.get("mark_price")
                if pos.get("mark_price") is not None
                else pos.get("last_price")
            )
        )

        raw_liquidation = (
            pos.get("liquidation")
            if pos.get("liquidation") is not None
            else pos.get("liquidation_price")
        )

        raw_unrealised = (
            pos.get("unrealised_pnl")
            if pos.get("unrealised_pnl") is not None
            else pos.get("unrealized_pnl")
        )

        # --------------------------------------------------------
        # Normalize side from signed quantity if needed.
        # --------------------------------------------------------
        if exchange_side not in ("BUY", "SELL") and raw_qty is not None:
            try:
                exchange_side = (
                    "BUY" if float(raw_qty) > 0 else "SELL"
                )
            except (TypeError, ValueError):
                exchange_side = ""

        # --------------------------------------------------------
        # Store exchange position as local source of truth.
        # --------------------------------------------------------
        state["in_position"] = True
        state["position_closing"] = False
        state["position_side"] = exchange_side

        if raw_entry is not None:
            try:
                entry = float(raw_entry)
                state["position_entry"] = entry
                state["actual_entry"] = entry
            except (TypeError, ValueError):
                pass

        if raw_qty is not None:
            try:
                qty = abs(float(raw_qty))
                state["position_qty"] = qty
                state["actual_qty"] = qty
            except (TypeError, ValueError):
                pass

        if raw_margin is not None:
            try:
                margin = float(raw_margin)
                if margin > 0:
                    state["position_margin"] = margin
            except (TypeError, ValueError):
                pass

        if raw_leverage is not None:
            try:
                leverage = float(raw_leverage)
                if leverage > 0:
                    state["actual_leverage"] = leverage
            except (TypeError, ValueError):
                pass

        if raw_mark is not None:
            try:
                mark = float(raw_mark)
                if mark > 0:
                    state["mark_price"] = mark
            except (TypeError, ValueError):
                pass

        if raw_liquidation is not None:
            try:
                liquidation = float(raw_liquidation)
                if liquidation > 0:
                    state["liquidation_price"] = liquidation
            except (TypeError, ValueError):
                pass

        # CoinSwitch provides exchange-calculated unrealised PnL.
        # Preserve it separately so dashboard/live PnL can use the
        # exchange value without replacing the existing strategy math.
        if raw_unrealised is not None:
            try:
                state["exchange_unrealised_pnl"] = float(raw_unrealised)
            except (TypeError, ValueError):
                pass

        # Position notional/value used by existing capacity/dashboard
        # logic. Prefer actual entry * actual quantity because these
        # are the normalized position fields already used elsewhere.
        if state.get("position_entry") and state.get("position_qty"):
            state["position_size"] = (
                state["position_entry"] * state["position_qty"]
            )


def _protect_live_position(symbol, allow_emergency=True):
    """
    Reconcile one live exchange position and guarantee that an exchange-side
    protective stop is either verified or the position is actively recovered.
    """
    global RECOVERY_LOCK

    pos = _live_position_snapshot(symbol)

    # ------------------------------------------------------------
    # EXTERNAL / MANUAL CLOSE RECONCILIATION
    # Exchange API succeeded and confirmed that this symbol has
    # no open position. Clear the local runtime position state.
    # Do NOT treat this as an API failure.
    # ------------------------------------------------------------
    if isinstance(pos, dict) and pos.get("_position_closed"):
        _finalize_external_position_close(
            symbol,
            recovery_status="EXTERNALLY_CLOSED",
        )

        RECOVERY_LOCK = False
        return True

    if pos is None:
        with state_lock:
            state = symbol_states.get(symbol)
            if state and state.get("in_position"):
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
        RECOVERY_LOCK = True
        return False

    if pos.get("qty") is None or abs(float(pos.get("qty") or 0.0)) <= 0:
        return False

    _set_live_state_from_exchange(symbol, pos)

    with state_lock:
        state = symbol_states[symbol]
        side = state.get("position_side")
        entry = state.get("actual_entry") or state.get("position_entry")
        strategy_sl = state.get("strategy_sl") or state.get("position_sl")
        qty = state.get("actual_qty") or state.get("position_qty")
        margin = state.get("position_margin")
        mark = state.get("mark_price") or state.get("live_price")
        leverage = state.get("actual_leverage")

    if not side or not entry or not qty:
        with state_lock:
            state["recovery_status"] = "RECOVERING"
            state["sl_status"] = "UNKNOWN"
        RECOVERY_LOCK = True
        return False

    # Primary strategy SL remains primary whenever it is valid for the side.
    primary_valid = (
        strategy_sl is not None
        and (
            (side == "BUY" and strategy_sl < entry)
            or (side == "SELL" and strategy_sl > entry)
        )
    )

    target_sl = strategy_sl if primary_valid else None
    protection_type = "PRIMARY"

    # IMPORTANT:
    # During recovery, first accept an already-existing exchange-side
    # protective STOP if it is valid and protective for the recovered
    # position. Only fall back to the emergency SL when no valid
    # exchange protection exists.
    if target_sl is not None:
        verification = verify_exchange_stop_loss(
            symbol,
            side,
            target_sl,
        )
    else:
        verification = verify_exchange_stop_loss(
            symbol,
            side,
            _emergency_sl(entry, side, qty, margin),
        )

    if verification is None:
        with state_lock:
            state["recovery_status"] = "RECOVERING"
            state["sl_status"] = "UNKNOWN"
        RECOVERY_LOCK = True
        return False

    if verification.get("verified"):
        verified_trigger = verification.get("trigger_price")
        try:
            verified_trigger = float(verified_trigger)
        except (TypeError, ValueError):
            verified_trigger = None

        if verified_trigger is not None:
            target_sl = verified_trigger

        protection_type = "PRIMARY" if primary_valid else "EXCHANGE_EXISTING"

    else:
        if not allow_emergency:
            with state_lock:
                state["recovery_status"] = "UNPROTECTED"
                state["sl_status"] = "INVALID"
            RECOVERY_LOCK = True
            return False

        target_sl = _emergency_sl(entry, side, qty, margin)
        protection_type = "EMERGENCY"

        risk_pct = _actual_sl_risk_pct(entry, target_sl, qty, margin)

        if risk_pct is not None and risk_pct > (MAX_SL_RISK_PCT + 1e-9):
            print(
                f"[PROTECTION BLOCKED] {symbol}: {protection_type} SL risk "
                f"{risk_pct:.3f}% exceeds {MAX_SL_RISK_PCT:.3f}%."
            )

            with state_lock:
                state["recovery_status"] = "EMERGENCY_RISK_EXCEEDED"
                state["sl_status"] = "INVALID"

            RECOVERY_LOCK = True
            return False

        # If downtime already crossed the emergency level, close immediately.
        if _sl_crossed(entry, side, target_sl, mark):
            print(
                f"[EMERGENCY EXIT] {symbol}: current price {mark} already "
                f"crossed emergency SL {target_sl:.8f}. Closing reduce-only."
            )

            with state_lock:
                state["position_sl"] = target_sl
                state["strategy_sl"] = strategy_sl
                state["emergency_sl"] = target_sl
                state["active_sl"] = target_sl
                state["protection_type"] = "EMERGENCY_BREACHED"
                state["sl_status"] = "BREACHED"
                state["recovery_status"] = "CLOSING_RECOVERED"

            close_position(symbol, mark, "EMERGENCY SL BREACHED DURING RECOVERY")
            RECOVERY_LOCK = True
            return False

        print(
            f"[PROTECTION] {symbol}: no verified exchange STOP found. "
            f"Placing emergency protection at {target_sl:.8f}..."
        )

        result = place_exchange_stop_loss(
            symbol,
            side,
            target_sl,
            qty,
        )

        if result is None or result.get("ambiguous"):
            with state_lock:
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
            RECOVERY_LOCK = True
            return False

        verification = verify_exchange_stop_loss(
            symbol,
            side,
            target_sl,
        )

        if verification is None or not verification.get("verified"):
            with state_lock:
                state["recovery_status"] = "UNVERIFIED"
                state["sl_status"] = "UNVERIFIED"
            RECOVERY_LOCK = True
            return False

        verified_trigger = verification.get("trigger_price")
        try:
            verified_trigger = float(verified_trigger)
        except (TypeError, ValueError):
            verified_trigger = target_sl

        target_sl = verified_trigger

    # Calculate risk from the actual protection currently being used.
    risk_pct = _actual_sl_risk_pct(entry, target_sl, qty, margin)

    with state_lock:
        state = symbol_states[symbol]
        state["strategy_sl"] = strategy_sl
        state["emergency_sl"] = _emergency_sl(entry, side, qty, margin)
        state["position_sl"] = target_sl
        state["active_sl"] = target_sl
        state["sl_order_id"] = verification.get("order_id")
        state["sl_status"] = "VERIFIED"
        state["protection_type"] = protection_type
        state["protection_verified_at"] = time.time()
        state["protection_last_check"] = time.time()
        state["recovery_status"] = "PROTECTED"
        state["actual_entry"] = entry
        state["actual_qty"] = qty
        state["actual_leverage"] = leverage
        state["sl_distance_pct"] = (
            abs(entry - target_sl) / entry * 100.0
            if entry else None
        )
        state["sl_capital_risk_pct"] = risk_pct

    RECOVERY_LOCK = False
    print(
        f"[PROTECTED] {symbol} {side}: "
        f"{protection_type} SL={target_sl:.8f} "
        f"order_id={verification.get('order_id')}"
    )
    return True


def recover_live_positions(allow_orders_off=False):
    """
    Startup reconciliation. Exchange is the source of truth.

    Normal startup recovery requires LIVE_MODE + ORDERS_ENABLED because
    recovered positions must be protected before live execution is unlocked.

    When allow_orders_off=True, R hotkey can perform a READ-ONLY live
    exchange discovery test with LIVE_MODE ON and ORDERS_ENABLED OFF.
    In that mode no protection/order action is attempted.
    """
    global RECOVERY_LOCK

    if not LIVE_MODE:
        RECOVERY_LOCK = False
        return True

    if not ORDERS_ENABLED and not allow_orders_off:
        RECOVERY_LOCK = False
        return True

    readonly_recovery = bool(allow_orders_off and not ORDERS_ENABLED)

    if readonly_recovery:
        print("[RECOVERY] READ-ONLY LIVE exchange discovery mode.")
    else:
        RECOVERY_LOCK = True
        print("[RECOVERY] Starting LIVE exchange reconciliation...")

    RECOVERY_LOCK = True
    print("[RECOVERY] Starting LIVE exchange reconciliation...")

    path = "/trade/api/v2/futures/positions"

    try:
        headers, signed_path = sign_request(
            "GET",
            path,
            params={"exchange": "EXCHANGE_2"},
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[RECOVERY POSITIONS] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            print("[RECOVERY] Invalid positions payload.")
            RECOVERY_LOCK = True
            return False

        rows = payload.get("data")

        if not isinstance(rows, list):
            print("[RECOVERY] Positions payload missing data list.")
            RECOVERY_LOCK = True
            return False

        exchange_symbols = set()
        recovered_symbols = []

        for row in rows:
            if not isinstance(row, dict):
                continue

            symbol = str(row.get("symbol") or "").upper().strip()
            status = str(row.get("status") or "").upper()

            try:
                qty = abs(float(row.get("position_size") or 0.0))
            except Exception:
                qty = 0.0

            if not symbol or qty <= 0 or status not in ("OPEN", "ACTIVE", ""):
                continue

            exchange_symbols.add(symbol)
            recovered_symbols.append(symbol)

            print(
                f"[RECOVERY POSITION] {symbol}: "
                f"side={row.get('position_side')} "
                f"qty={row.get('position_size')} "
                f"entry={row.get('avg_entry_price')} "
                f"leverage={row.get('leverage')}"
            )

            _set_live_state_from_exchange(symbol, row)

            if readonly_recovery:
                with state_lock:
                    state = symbol_states[symbol]
                    state["recovery_status"] = "DISCOVERED_READONLY"
                    state["sl_status"] = "UNKNOWN"

                print(
                    f"[RECOVERY READONLY] {symbol}: "
                    "exchange position restored locally; "
                    "protection check skipped because ORDERS_ENABLED=OFF."
                )
                continue

            with state_lock:
                state = symbol_states[symbol]

                # Preserve any already-known strategy SL.
                # On a restart this may be unavailable, so protection
                # verification must rely on the exchange-side STOP.
                if not state.get("strategy_sl"):
                    state["strategy_sl"] = state.get("position_sl")

                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"

            try:
                protected = _protect_live_position(symbol)
            except Exception as exc:
                print(f"[RECOVERY PROTECTION ERROR] {symbol}: {exc}")
                protected = False

            if protected:
                print(f"[RECOVERY PROTECTED] {symbol}")
            else:
                print(f"[RECOVERY UNPROTECTED] {symbol}")

        # IMPORTANT:
        # The positions endpoint succeeded and returned the complete
        # exchange position snapshot. Therefore a locally-open position
        # absent from exchange_symbols is confirmed closed externally.
        #
        # Never leave such a position as RECOVERING, because dashboard
        # and position-capacity logic use local in_position=True and would
        # otherwise create a permanent ghost position.
        with state_lock:
            local_live_symbols = {
                str(symbol).upper()
                for symbol, state in symbol_states.items()
                if state.get("in_position")
            }

        missing_locally = local_live_symbols - exchange_symbols

        for symbol in sorted(missing_locally):
            print(
                f"[RECOVERY SYNC] {symbol}: local position not found in "
                f"successful exchange snapshot. Clearing local position."
            )

            _finalize_external_position_close(
                symbol,
                recovery_status="EXTERNALLY_CLOSED",
            )

        # Final safety gate.
        # At this point every locally remembered live position has either:
        #   1) been found on the successful exchange snapshot, or
        #   2) been confirmed absent and locally cleared.
        with state_lock:
            active = [
                symbol
                for symbol, state in symbol_states.items()
                if state.get("in_position")
            ]

            all_protected = all(
                symbol in exchange_symbols
                and symbol_states[symbol].get("sl_status") == "VERIFIED"
                and symbol_states[symbol].get("recovery_status") == "PROTECTED"
                for symbol in active
            )

        RECOVERY_LOCK = not all_protected

        print(
            f"[RECOVERY] Exchange positions discovered: "
            f"{', '.join(recovered_symbols) if recovered_symbols else 'NONE'}"
        )
        print(
            f"[RECOVERY] FINAL STATUS: "
            f"{'PROTECTED / UNLOCKED' if not RECOVERY_LOCK else 'LOCKED / RECOVERING'}"
        )

        return not RECOVERY_LOCK

    except Exception as exc:
        print(f"[RECOVERY ERROR] {exc}")
        RECOVERY_LOCK = True
        return False


def start_live_position_sync_thread():
    """Start fast live position synchronization exactly once."""
    global LIVE_POSITION_SYNC_THREAD

    try:
        if LIVE_POSITION_SYNC_THREAD is not None:
            if LIVE_POSITION_SYNC_THREAD.is_alive():
                return

        LIVE_POSITION_SYNC_THREAD = threading.Thread(
            target=live_position_sync_loop,
            daemon=True,
            name="live-position-sync",
        )
        LIVE_POSITION_SYNC_THREAD.start()
        print("[LIVE SYNC] Fast live position sync started.")

    except Exception as exc:
        print(f"[LIVE SYNC] Failed to start sync thread: {exc}")


def live_position_sync_loop():
    """
    Fast read-only exchange position synchronization.

    Purpose:
      - Keep local in_position state aligned with exchange.
      - Detect external/manual/SL closures quickly.
      - Do NOT perform protection placement here.
      - Do NOT unlock RECOVERY_LOCK here.
      - Protection remains the responsibility of live_protection_watchdog().

    This loop runs faster than the protection watchdog so the dashboard
    does not retain stale local positions for the full recovery interval.
    """
    global RECOVERY_LOCK

    while True:
        try:
            if LIVE_MODE:

                with state_lock:
                    active_symbols = [
                        str(symbol).upper()
                        for symbol, state in symbol_states.items()
                        if state.get("in_position")
                    ]

                for symbol in active_symbols:
                    try:
                        pos = _live_position_snapshot(symbol)

                        # Exchange API succeeded and explicitly confirmed
                        # that this symbol has no open position.
                        if isinstance(pos, dict) and pos.get("_position_closed"):
                            _finalize_external_position_close(
                                symbol,
                                recovery_status="EXTERNALLY_CLOSED",
                            )
                            print(
                                f"[FAST POSITION SYNC] {symbol}: "
                                f"exchange position closed; local state cleared."
                            )
                            continue

                        # API failure: NEVER clear local state.
                        if pos is None:
                            continue

                        # Exchange position still exists.
                        qty = abs(float(pos.get("qty") or 0.0))
                        if qty <= 0:
                            continue

                        _set_live_state_from_exchange(symbol, pos)

                        if not ORDERS_ENABLED:
                            with state_lock:
                                state = symbol_states.get(symbol)
                                if state and state.get("in_position"):
                                    state["recovery_status"] = "DISCOVERED_READONLY"
                                    state["sl_status"] = "UNKNOWN"

                    except Exception as exc:
                        print(
                            f"[FAST POSITION SYNC ERROR] "
                            f"{symbol}: {exc}"
                        )

            elif not LIVE_MODE:
                RECOVERY_LOCK = False

        except Exception as exc:
            print(f"[FAST POSITION SYNC LOOP ERROR] {exc}")

        time.sleep(LIVE_POSITION_SYNC_INTERVAL)

def live_protection_watchdog():
    """Continuously reconcile every locally open live position."""
    global RECOVERY_LOCK

    while True:
        try:
            if LIVE_MODE and ORDERS_ENABLED:
                active = []

                with state_lock:
                    for symbol, state in symbol_states.items():
                        if state.get("in_position"):
                            active.append(symbol)

                if active:
                    RECOVERY_LOCK = True
                    all_protected = True

                    for symbol in active:
                        try:
                            protected = _protect_live_position(symbol)
                            if not protected:
                                all_protected = False
                        except Exception as exc:
                            print(f"[WATCHDOG ERROR] {symbol}: {exc}")
                            all_protected = False

                    # Unlock only when every active live position has verified
                    # protection. A single failed symbol keeps the bot locked.
                    RECOVERY_LOCK = not all_protected
                else:
                    RECOVERY_LOCK = False

        except Exception as exc:
            print(f"[WATCHDOG LOOP ERROR] {exc}")
            RECOVERY_LOCK = True

        time.sleep(POSITION_RECOVERY_INTERVAL)


def low_capital_leverage_candidates(symbol):
    with state_lock:
        info = dict(instrument_info.get(symbol) or {})

    max_lev = to_float(info.get("max_leverage")) or 0.0

    return [
        float(lev)
        for lev in LOW_CAPITAL_LEVERAGES
        if float(lev) <= max_lev + 1e-9
    ]


def select_low_capital_entry(symbol, entry, sl, side):
    """
    LOW CAPITAL MODE:
      - quantity is ALWAYS exchange min_qty
      - try 10x, then 20x, then 40x
      - never exceed exchange max leverage
      - never exceed our 40x hard limit
      - selected leverage must pass SL-risk
      - selected leverage must make min_qty affordable
    """
    with state_lock:
        info = dict(instrument_info.get(symbol) or {})

    min_qty = to_float(info.get("min_qty"))
    if min_qty is None or min_qty <= 0:
        return None, None, None

    candidates = low_capital_leverage_candidates(symbol)
    if not candidates:
        return None, None, None

    with state_lock:
        deployed_margin = total_deployed_margin()

    max_margin = max_deployable_margin()
    remaining_margin = max(
        0.0,
        max_margin - deployed_margin
    )

    available = effective_available_capital()
    if available is not None and available >= 0:
        remaining_margin = min(
            remaining_margin,
            float(available)
        )

    notional = min_qty * entry

    for lev in candidates:
        if not sl_risk_allowed(
            entry,
            sl,
            leverage=lev,
            symbol=symbol,
            side=side,
        ):
            continue

        required_margin = notional / lev

        if required_margin <= remaining_margin + 1e-12:
            return lev, min_qty, required_margin

        print(
            f"[LOW CAPITAL] {symbol}: {lev:g}x cannot afford "
            f"min_qty={min_qty:.12g} | required margin="
            f"{required_margin:.8f} | available capacity="
            f"{remaining_margin:.8f}"
        )

    return None, None, None

def open_position(symbol, side, entry, sl, candle_start):
    global RECOVERY_LOCK

    """
    Open only after crossover + funding + scanner + capital safety gates.

    IMPORTANT (concurrency): this function must be called WITHOUT
    holding state_lock. It takes the lock itself only for short state
    reads/writes; the network calls (set_symbol_leverage,
    place_market_order) run OUTSIDE any lock, so a slow/hanging HTTP
    request never blocks price/candle updates or stop-loss checks for
    other symbols. The position slot is reserved (in_position = True)
    under lock BEFORE the network call starts, so two callers can
    never race into opening the same symbol twice; the reservation is
    rolled back if the order actually fails.
    """

    if LIVE_MODE and ORDERS_ENABLED and RECOVERY_LOCK:
        print(f"[ENTRY BLOCKED] {symbol}: LIVE recovery/protection lock is active.")
        return False

    if LIVE_MODE and (account_balance is None or account_balance <= 0):
        print(f"[ENTRY BLOCKED] {symbol}: execution-account USDT balance is unavailable/zero.")
        return False

    blocked, funding_time, _, _ = funding_status()
    if blocked:
        print(f"[ENTRY BLOCKED] {symbol} {side}: funding blackout around {funding_time.strftime('%H:%M UTC')}")
        return False

    allowed, scanner_score, scanner_reason = hft_entry_gate(symbol, side)
    if not allowed:
        print(f"[ENTRY BLOCKED] {symbol} {side}: HFT scanner -> {scanner_reason}")
        return False

    # If multiple symbols cross in the same ticker cycle, only the
    # highest scanner-priority candidate gets the single position slot.
    with state_lock:
        priority_symbol = entry_priority_symbol
        priority_score = entry_priority_score
    if priority_symbol is not None and priority_symbol != symbol:
        if scanner_score < (priority_score or scanner_score):
            print(f"[ENTRY PRIORITY] {symbol} deferred: {priority_symbol} has higher scanner score {priority_score:.2f}")
            return False

    capital = effective_total_capital()

    with state_lock:
        info = dict(instrument_info.get(symbol) or {})

    min_qty = to_float(info.get("min_qty"))

    if min_qty is None or min_qty <= 0:
        print(f"[ENTRY BLOCKED] {symbol}: invalid exchange min_qty.")
        return False

    selected_leverage = None
    qty = None
    margin = None
    notional = None

    if LOW_CAPITAL_MODE:
        # C-MODE: quantity NEVER grows beyond exchange minimum.
        selected_leverage, qty, margin = select_low_capital_entry(
            symbol,
            entry,
            sl,
            side,
        )

        if selected_leverage is None:
            print(
                f"[ENTRY BLOCKED] {symbol}: min_qty={min_qty:.12g} "
                f"not affordable at 10x -> 20x -> 40x, or SL risk "
                f"failed at the candidate leverage."
            )
            return False

        notional = qty * entry

    else:
        # NORMAL MODE: preserve the existing 10x allocation behavior.
        selected_leverage = TRADE_LEVERAGE

        if not sl_risk_allowed(
            entry,
            sl,
            leverage=selected_leverage,
            symbol=symbol,
            side=side,
        ):
            return False

        max_entry_margin = entry_margin_allocation()
        max_total_margin = max_deployable_margin()

        with state_lock:
            deployed_margin = total_deployed_margin()

        remaining_margin = max(
            0.0,
            max_total_margin - deployed_margin
        )
        target_margin = min(
            max_entry_margin,
            remaining_margin
        )

        if capital <= 0 or target_margin <= 0:
            print(
                f"[ENTRY BLOCKED] {symbol}: no capital/deployment capacity."
            )
            return False

        target_notional = target_margin * selected_leverage
        qty = target_notional / entry

        if qty + 1e-12 < min_qty:
            print(
                f"[ENTRY BLOCKED] {symbol}: calculated qty={qty:.12g} "
                f"< exchange min_qty={min_qty:.12g}"
            )
            return False

        notional = qty * entry
        margin = notional / selected_leverage

    capital_used_pct = (
        (margin / capital * 100.0)
        if capital > 0
        else 0.0
    )

    with state_lock:
        can_afford = can_afford_new_position(
            symbol,
            entry,
            selected_leverage,
        )

    if not can_afford:
        print(
            f"[ENTRY BLOCKED] {symbol}: margin {margin:.8f} USDT "
            f"exceeds available deployment capacity "
            f"{max_deployable_margin():.8f} USDT "
            f"at {selected_leverage:g}x"
        )
        return False

    # ORDERS is an independent execution switch.
    # Account mode can remain selected while execution is OFF.
    if not ORDERS_ENABLED:
        print(f"[ENTRY BLOCKED] {symbol}: ORDERS are disabled.")
        return False

    # Reserve the position slot atomically BEFORE any network call, so
    # no other candidate can slip into this symbol (or this bot's
    # single-position slot) while the order is in flight.
    with state_lock:
        state = symbol_states.get(symbol)
        if state is None or state["in_position"]:
            return False
        state["in_position"] = True

    # Use the leverage selected above for this entry.
    configured_leverage_by_symbol.setdefault(symbol, None)
    if configured_leverage_by_symbol.get(symbol) != selected_leverage:
        leverage_set_symbols.discard(symbol)

    needs_leverage = LIVE_MODE and ORDERS_ENABLED and symbol not in leverage_set_symbols

    if needs_leverage:
        if set_symbol_leverage(symbol, selected_leverage):  # network call - NOT under state_lock
            with state_lock:
                leverage_set_symbols.add(symbol)
        else:
            print(f"[open_position] Could not confirm {selected_leverage:g}x leverage for {symbol}.")
            with state_lock:
                state["in_position"] = False  # release the reservation
            return False

    order_response = None
    entry_fee = 0.0

    if LIVE_MODE and ORDERS_ENABLED:
        order_response = place_market_order(
            symbol,
            side,
            qty,
        )  # network call - NOT under state_lock

        if order_response is None:
            print(f"[open_position] Order placement FAILED for {symbol} {side}.")
            with state_lock:
                state["in_position"] = False
            return False

        # Timeout after submission is ambiguous. Never resend automatically.
        if order_response.get("_ambiguous"):
            print(
                f"[open_position] AMBIGUOUS execution for {symbol} {side}. "
                f"Starting reconciliation; no duplicate order will be sent."
            )
            with state_lock:
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
            RECOVERY_LOCK = True
            return False

        entry_fee = extract_actual_fee(order_response) or 0.0
        if extract_actual_fee(order_response) is None:
            print(
                f"[FEE WARNING] {symbol}: exchange entry-order response "
                f"did not expose a fee field; entry fee saved as 0."
            )

        # Exchange is source of truth for actual fill.
        time.sleep(LIVE_ORDER_POLL_SECONDS)

        exchange_pos = None
        for _ in range(LIVE_ORDER_POLL_ATTEMPTS):
            exchange_pos = _live_position_snapshot(symbol)
            if exchange_pos and exchange_pos.get("qty") and exchange_pos.get("entry"):
                break
            time.sleep(LIVE_ORDER_POLL_SECONDS)

        if not exchange_pos or not exchange_pos.get("qty") or not exchange_pos.get("entry"):
            print(
                f"[open_position] Could not confirm exchange position for "
                f"{symbol}. No duplicate order will be sent."
            )
            with state_lock:
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
            RECOVERY_LOCK = True
            return False

        actual_entry = exchange_pos["entry"]
        actual_qty = exchange_pos["qty"]
        actual_leverage = exchange_pos.get("leverage")
        try:
            actual_leverage = float(actual_leverage) if actual_leverage is not None else None
            if actual_leverage is not None and actual_leverage <= 0:
                actual_leverage = None
        except (TypeError, ValueError):
            actual_leverage = None

        actual_margin = exchange_pos.get("margin")
        try:
            actual_margin = float(actual_margin) if actual_margin is not None else None
            if actual_margin is not None and actual_margin <= 0:
                actual_margin = None
        except (TypeError, ValueError):
            actual_margin = None

        # Post-entry position accounting must use exchange-reported
        # margin/leverage. Never inject configured entry leverage here.
        if actual_margin is None and actual_leverage is not None:
            actual_margin = (
                actual_entry * actual_qty / actual_leverage
            )

        with state_lock:
            state["position_side"] = side
            state["position_entry"] = actual_entry
            state["actual_entry"] = actual_entry
            state["position_sl"] = sl
            state["strategy_sl"] = sl
            state["position_qty"] = actual_qty
            state["actual_qty"] = actual_qty
            state["position_size"] = actual_entry * actual_qty
            state["position_margin"] = actual_margin
            state["actual_leverage"] = actual_leverage
            state["capital_used_pct"] = (
                actual_margin / capital * 100.0 if capital > 0 else 0.0
            )
            state["scanner_score"] = scanner_score
            state["scanner_quality"] = scanner_reason
            state["scanner_bias"] = "LONG" if side == "BUY" else "SHORT"
            state["position_open_time"] = time.time()
            state["entry_fee"] = entry_fee
            state["exchange_order_id"] = _extract_order_id(order_response)
            state["recovery_status"] = "PROTECTING"

        # Primary exchange SL is mandatory before considering the position protected.
        if not _protect_live_position(symbol, allow_emergency=True):
            print(
                f"[open_position] {symbol}: exchange SL could not be verified. "
                f"Recovery lock remains active."
            )
            return False

    else:
        with state_lock:
            state["position_side"] = side
            state["position_entry"] = entry
            state["actual_entry"] = entry
            state["position_sl"] = sl
            state["strategy_sl"] = sl
            state["position_qty"] = qty
            state["actual_qty"] = qty
            state["position_size"] = notional
            state["position_margin"] = margin
            state["actual_leverage"] = selected_leverage
            state["capital_used_pct"] = capital_used_pct
            state["scanner_score"] = scanner_score
            state["scanner_quality"] = scanner_reason
            state["scanner_bias"] = "LONG" if side == "BUY" else "SHORT"
            state["position_open_time"] = time.time()
            state["entry_fee"] = entry_fee

    print()
    print("#" * 68)
    print(f">>> POSITION OPENED: {symbol} {side} <<<")
    print("#" * 68)
    with state_lock:
        _display_entry = state.get("actual_entry") or entry
        _display_sl = state.get("active_sl") or sl
        _display_qty = state.get("actual_qty") or qty
        _display_margin = state.get("position_margin") or margin
        _display_notional = state.get("position_size") or notional
        _display_leverage = state.get("actual_leverage") or TRADE_LEVERAGE

    print(f"Entry            : {_display_entry:.8f}")
    print(f"Stop Loss        : {_display_sl:.8f}")
    print(f"Quantity         : {_display_qty}")
    print(f"Margin Used      : {_display_margin:.6f} USDT")
    print(f"Capital Used     : {capital_used_pct:.3f}%")
    print(f"Notional         : {_display_notional:.6f} USDT")
    print(f"Leverage         : {_display_leverage}x")
    print(f"HFT Priority     : {scanner_score:.2f} ({scanner_reason})")
    if LIVE_MODE and ORDERS_ENABLED:
        print("Real Order       : YES")
    elif PAPER_MODE and ORDERS_ENABLED:
        print("Real Order       : NO (paper trade)")
    else:
        print("Real Order       : NO (orders disabled)")
    print("#" * 68)
    return True


def close_position(symbol, exit_price, reason):
    """
    IMPORTANT (concurrency): like open_position(), this must be
    called WITHOUT holding state_lock. The closing order network
    call runs outside any lock. `position_closing` is set under
    lock BEFORE that call starts (in_position stays True so no new
    entry can race in on this symbol mid-close) and is rolled back
    if the closing order actually fails, so the exit will simply be
    retried on the next price tick.
    """
    global paper_capital, trade_log
    global RECOVERY_LOCK

    with state_lock:
        state = symbol_states.get(symbol)
        if state is None or not state["in_position"] or state.get("position_closing"):
            return
        side = state["position_side"]
        entry = state["position_entry"]
        qty = state.get("actual_qty") or state["position_qty"]
        size = state["position_size"]
        sl_at_open = state["active_sl"] or state["position_sl"]
        actual_entry = state.get("actual_entry") or state["position_entry"]
        actual_leverage = state.get("actual_leverage")
        try:
            actual_leverage = float(actual_leverage) if actual_leverage is not None else 0.0
        except (TypeError, ValueError):
            actual_leverage = 0.0

        capital_used_pct = state.get("capital_used_pct")
        scanner_score = state.get("scanner_score")
        scanner_quality = state.get("scanner_quality")
        open_time = state["position_open_time"]
        entry_fee = float(state.get("entry_fee") or 0.0)

        margin = state.get("position_margin")
        try:
            margin = float(margin) if margin is not None else 0.0
        except (TypeError, ValueError):
            margin = 0.0

        if margin <= 0 and size and actual_leverage > 0:
            margin = size / actual_leverage
        state["position_closing"] = True  # reserve the close

    order_response = None
    exit_fee = 0.0

    if LIVE_MODE and ORDERS_ENABLED:
        opposite_side = "SELL" if side == "BUY" else "BUY"
        order_response = place_market_order(symbol, opposite_side, qty, reduce_only=True)  # network call - NOT under state_lock

        # A timeout/ambiguous response means the exchange may have accepted
        # the close. Never resend the close and never cancel the protective
        # STOP until the exchange position is explicitly reconciled.
        if order_response is None or (
            isinstance(order_response, dict) and order_response.get("_ambiguous")
        ):
            print(
                f"[close_position] AMBIGUOUS/FAILED close for {symbol}. "
                f"No retry order and protective STOP will remain active."
            )
            with state_lock:
                state["position_closing"] = False
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
            RECOVERY_LOCK = True
            return

        actual_fee = extract_actual_fee(order_response)
        if actual_fee is not None:
            exit_fee = actual_fee
        else:
            print(
                f"[FEE WARNING] {symbol}: exchange close-order response "
                f"did not expose a fee field; exit fee saved as 0. "
                f"No estimated fee used."
            )

        # Exchange-reported average execution price is the authoritative
        # live exit price. The strategy trigger price is only the requested
        # market-exit reference and must not be used for final accounting.
        exchange_exit_price = extract_actual_execution_price(order_response)

        if exchange_exit_price is not None:
            exit_price = exchange_exit_price
            print(
                f"[LIVE FILL] {symbol}: actual exit fill "
                f"{exit_price:.8f}"
            )
        else:
            print(
                f"[FILL WARNING] {symbol}: close response did not expose "
                f"avg_execution_price. Using requested exit price "
                f"{exit_price:.8f}; no estimated fill will be invented."
            )

        # Exchange is the source of truth. Confirm that the position is
        # actually gone before removing its protective STOP.
        close_verified = False
        remaining_qty = None

        for _ in range(LIVE_ORDER_POLL_ATTEMPTS):
            raw_after = get_live_positions(symbol)

            if raw_after is None:
                # API failure/unknown state. Do not assume the position closed.
                time.sleep(LIVE_ORDER_POLL_SECONDS)
                continue

            pos_after = _extract_position_from_payload(raw_after, symbol)

            if pos_after is None:
                close_verified = True
                break

            try:
                remaining_qty = float(pos_after.get("qty") or 0.0)
            except Exception:
                remaining_qty = None

            if remaining_qty is not None and remaining_qty <= 0:
                close_verified = True
                break

            # Partial close or residual position. Keep the protective STOP.
            if remaining_qty is not None and remaining_qty > 0:
                print(
                    f"[close_position] PARTIAL/RESIDUAL position remains for "
                    f"{symbol}: qty={remaining_qty}. Protective STOP retained."
                )
                with state_lock:
                    state["actual_qty"] = remaining_qty
                    state["position_qty"] = remaining_qty
                    state["position_closing"] = False
                    state["recovery_status"] = "RECOVERING"
                    state["sl_status"] = "VERIFIED"
                RECOVERY_LOCK = True
                return

            time.sleep(LIVE_ORDER_POLL_SECONDS)

        if not close_verified:
            print(
                f"[close_position] CLOSE NOT VERIFIED for {symbol}. "
                f"Protective STOP remains active; recovery lock enabled."
            )
            with state_lock:
                state["position_closing"] = False
                state["recovery_status"] = "RECOVERING"
                state["sl_status"] = "UNKNOWN"
            RECOVERY_LOCK = True
            return

        print(
            f"[close_position] EXCHANGE POSITION CLOSED VERIFIED for {symbol}."
        )

        # Only after zero position is verified, remove the old protective STOP.
        old_sl_order_id = state.get("sl_order_id")
        if old_sl_order_id:
            cancel_result = cancel_order(old_sl_order_id)
            if cancel_result is None:
                print(
                    f"[PROTECTION WARNING] {symbol}: could not cancel "
                    f"old STOP_MARKET order {old_sl_order_id} after verified close. "
                    f"Open-order reconciliation will handle it."
                )
            else:
                print(
                    f"[PROTECTION] {symbol}: old STOP_MARKET "
                    f"{old_sl_order_id} cancellation requested after verified close."
                )

    if LIVE_MODE and ORDERS_ENABLED:
        fee = entry_fee + exit_fee
    elif PAPER_MODE and ORDERS_ENABLED:
        # Paper mode mirrors the configured taker fee on both entry and exit.
        fee = (size or 0.0) * PAPER_FEE_RATE + (size or 0.0) * PAPER_FEE_RATE
    else:
        fee = 0.0

    entry = actual_entry
    raw_pnl_pct = calculate_raw_pnl_pct(entry, exit_price, side)
    pnl_usdt = size * raw_pnl_pct / 100.0 if size else 0.0
    margin_roi_pct = (pnl_usdt / margin * 100.0) if margin else 0.0

    # For an existing live position, ROI is based on actual position
    # margin. Do not re-apply configured entry leverage.
    pnl_pct = margin_roi_pct
    net_pnl_usdt = pnl_usdt - fee
    timestamp = time.time()

    record = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "sl": sl_at_open,
        "size": size,
        "margin": margin,
        "capital_used_pct": capital_used_pct,
        "leverage": actual_leverage,
        "actual_entry": actual_entry,
        "actual_qty": qty,
        "reason": reason,
        "pnl_pct": pnl_pct,
        "pnl_usdt": pnl_usdt,
        "fee": fee,
        "net_pnl_usdt": net_pnl_usdt,
        "margin_roi_pct": margin_roi_pct,
        "scanner_score": scanner_score,
        "scanner_quality": scanner_quality,
        "opened_at": open_time,
        "closed_at": timestamp,
        "mode": "live" if LIVE_MODE else ("paper" if PAPER_MODE else "none"),
    }

    trade_log.append(record)

    if LIVE_MODE:
        live_trade_log.append(record)
        _atomic_write_json(LIVE_LOG_FILE, live_trade_log)  # file I/O - NOT under state_lock
    elif PAPER_MODE:
        paper_capital = paper_capital + net_pnl_usdt
        paper_trade_log.append({**record, "resulting_capital": paper_capital})
        _atomic_write_json(PAPER_LOG_FILE, paper_trade_log)  # file I/O - NOT under state_lock
        save_paper_capital()

    print()
    print("#" * 68)
    print(f">>> POSITION CLOSED: {symbol} {side} <<<")
    print("#" * 68)
    print(f"Entry            : {entry:.8f}")
    print(f"Exit             : {exit_price:.8f}")
    print(f"Reason           : {reason}")
    print(f"Price Move       : {raw_pnl_pct:+.3f}%")
    print(f"Gross PnL        : {pnl_usdt:+.6f} USDT")
    print(f"Fees             : {fee:.6f} USDT")
    print(f"Net PnL          : {net_pnl_usdt:+.6f} USDT")
    print(f"Margin ROI       : {margin_roi_pct:+.3f}%")
    print(f"Position ROI     : {pnl_pct:+.3f}%"
          + (f" @ {actual_leverage:g}x" if actual_leverage > 0 else ""))
    if PAPER_MODE:
        print(f"Paper Capital    : {paper_capital:.6f} USDT")
    print("#" * 68)

    _clear_local_position_state(
        symbol,
        recovery_status="CLOSED",
    )


def check_position_exit(symbol, price, current_candle_now, previous_candle_now):
    """
    Must be called under state_lock, with symbol_states[symbol]
    ["in_position"] True.

    Exit conditions, checked in this order:
      1) Fixed/local stop loss.
      2) Rolling opposite BODY-OPEN cross:
         BUY  -> previous candle must be GREEN and the current RED
                 candle body crosses below the previous candle OPEN.
         SELL -> previous candle must be RED and the current GREEN
                 candle body crosses above the previous candle OPEN.

    Entry candles are handled separately; this function is only for
    managing an already-open position.
    """

    state = symbol_states[symbol]
    side = state["position_side"]
    sl = state["position_sl"]

    # --------------------------------------------------------
    # 1) STOP LOSS
    # --------------------------------------------------------
    if price is not None and sl is not None:
        if side == "BUY" and price <= sl:
            return True, price, "STOP LOSS HIT"
        if side == "SELL" and price >= sl:
            return True, price, "STOP LOSS HIT"

    # --------------------------------------------------------
    # 2) ROLLING OPPOSITE BODY-OPEN CROSS
    # --------------------------------------------------------
    if current_candle_now is None or previous_candle_now is None:
        return False, None, None

    try:
        current_open = float(current_candle_now["open"])
        reference_open = float(previous_candle_now["open"])
        live_price = float(price)
    except (KeyError, TypeError, ValueError):
        return False, None, None

    if current_open <= 0 or reference_open <= 0:
        return False, None, None

    # Current candle colour is determined by its fixed OPEN versus the
    # live/current close. A doji current candle has no opposite colour.
    if side == "BUY":
        previous_green = (
            float(previous_candle_now["close"]) >
            float(previous_candle_now["open"])
        )
        current_red = live_price < current_open

        # The BODY (open -> current close) must straddle the reference
        # open downward. A wick-only touch cannot satisfy this.
        body_crossed_down = current_open >= reference_open and live_price < reference_open

        if previous_green and current_red and body_crossed_down:
            return True, live_price, "OPPOSITE BODY-OPEN EXIT (RED crossed prior GREEN open)"

    elif side == "SELL":
        previous_red = (
            float(previous_candle_now["close"]) <
            float(previous_candle_now["open"])
        )
        current_green = live_price > current_open

        # The BODY (open -> current close) must straddle the reference
        # open upward. A wick-only touch cannot satisfy this.
        body_crossed_up = current_open <= reference_open and live_price > reference_open

        if previous_red and current_green and body_crossed_up:
            return True, live_price, "OPPOSITE BODY-OPEN EXIT (GREEN crossed prior RED open)"

    return False, None, None


# ============================================================
# TRAILING STOP HELPERS
# ============================================================

def _roi_price_for_target(entry, side, target_roi_pct, leverage=None,
                          margin=None, position_size=None):
    """Convert a capital/ROI target into the corresponding market price."""
    try:
        entry = float(entry)
        target_roi_pct = float(target_roi_pct)
    except (TypeError, ValueError):
        return None

    if entry <= 0:
        return None

    # Prefer actual margin + notional because that is the same basis used
    # by calculate_leveraged_pnl_pct(). Fall back to actual leverage.
    effective_leverage = None
    try:
        if margin is not None and position_size is not None:
            margin_f = float(margin)
            size_f = float(position_size)
            if margin_f > 0 and size_f > 0:
                effective_leverage = size_f / margin_f
    except (TypeError, ValueError, ZeroDivisionError):
        effective_leverage = None

    if effective_leverage is None:
        try:
            lev = float(leverage) if leverage is not None else 0.0
            if lev > 0:
                effective_leverage = lev
        except (TypeError, ValueError):
            pass

    if effective_leverage is None or effective_leverage <= 0:
        return None

    raw_move_pct = target_roi_pct / effective_leverage

    if side == "BUY":
        return entry * (1.0 + raw_move_pct / 100.0)
    if side == "SELL":
        return entry * (1.0 - raw_move_pct / 100.0)

    return None


def _calculate_trailing_candidate(side, entry, current_price, current_sl,
                                  trailing_peak=None, leverage=None,
                                  margin=None, position_size=None,
                                  trailing_armed=False):
    """
    Two-stage capital/ROI trailing logic.

    Stage 1 -- ARM at +5% ROI:
      Once favourable ROI reaches +5%, the position is armed.
      Before +10% ROI, the protective fallback is +2.5% ROI.
      If ROI subsequently falls to +2.5%, the local/exchange STOP exits.

    Stage 2 -- ACTIVATE at +10% ROI:
      Once +10% ROI is reached, the initial trailing floor becomes +10% ROI.
      Above +10%, the old 70/30 peak ratchet may move that floor higher.
      It can never move below +10% ROI.

    ROI is based on actual position margin/leverage, not configured entry
    leverage when actual exchange values are available.
    """

    if not TRAILING_SL_ENABLED:
        return None

    try:
        entry = float(entry)
        price = float(current_price)
    except (TypeError, ValueError):
        return None

    if entry <= 0 or price <= 0:
        return None

    try:
        arm_pct = float(TRAILING_ARM_PCT)
        fallback_pct = float(TRAILING_FALLBACK_PCT)
        activation_pct = float(TRAILING_ACTIVATION_PCT)
        retracement = float(TRAILING_RETRACEMENT_PCT)
    except (TypeError, ValueError):
        return None

    if not (0 <= fallback_pct < arm_pct < activation_pct):
        return None
    if not 0.0 <= retracement <= 100.0:
        return None

    roi = calculate_leveraged_pnl_pct(
        entry,
        price,
        side,
        leverage=leverage,
        margin=margin,
        position_size=position_size,
    )

    # No actual leverage/margin basis means we cannot safely interpret the
    # capital thresholds. Do not invent one.
    if roi == 0.0:
        raw_now = calculate_raw_pnl_pct(entry, price, side)
        try:
            if abs(raw_now) > 1e-12:
                return None
        except Exception:
            return None

    previous_peak = None
    if trailing_peak is not None:
        try:
            previous_peak = float(trailing_peak)
        except (TypeError, ValueError):
            previous_peak = None

    if side == "BUY":
        new_peak = max(price, previous_peak) if previous_peak is not None else price
    elif side == "SELL":
        new_peak = min(price, previous_peak) if previous_peak is not None else price
    else:
        return None

    # Do nothing before +5% ROI.
    if roi < arm_pct and not trailing_armed:
        return None

    # Once +5% has been reached, arm the fallback at +2.5% ROI.
    if roi < activation_pct:
        trigger = _roi_price_for_target(
            entry, side, fallback_pct,
            leverage=leverage,
            margin=margin,
            position_size=position_size,
        )
        if trigger is None:
            return None

        if current_sl is not None:
            try:
                current_sl = float(current_sl)
                if side == "BUY" and trigger <= current_sl:
                    return None
                if side == "SELL" and trigger >= current_sl:
                    return None
            except (TypeError, ValueError):
                pass

        return {
            "side": side,
            "peak": new_peak,
            "trigger": trigger,
            "stage": "ARMED",
            "armed": True,
            "roi": roi,
        }

    # +10% ROI reached: activate at a hard +10% ROI floor.
    activation_trigger = _roi_price_for_target(
        entry, side, activation_pct,
        leverage=leverage,
        margin=margin,
        position_size=position_size,
    )
    if activation_trigger is None:
        return None

    # Above +10%, retain the old 70/30 ratchet, but never below +10% ROI.
    lock_ratio = 1.0 - (retracement / 100.0)
    if side == "BUY":
        favorable_move = max(0.0, new_peak - entry)
        ratchet = entry + favorable_move * lock_ratio
        trigger = max(activation_trigger, ratchet)
    else:
        favorable_move = max(0.0, entry - new_peak)
        ratchet = entry - favorable_move * lock_ratio
        trigger = min(activation_trigger, ratchet)

    if current_sl is not None:
        try:
            current_sl = float(current_sl)
            if side == "BUY" and trigger <= current_sl:
                return None
            if side == "SELL" and trigger >= current_sl:
                return None
        except (TypeError, ValueError):
            pass

    return {
        "side": side,
        "peak": new_peak,
        "trigger": trigger,
        "stage": "ACTIVE",
        "armed": True,
        "roi": roi,
    }


def _replace_live_trailing_stop(symbol, position_side, candidate,
                                quantity, old_sl_order_id, current_sl):
    """
    LIVE trailing STOP replacement.

    Sequence:
      1. Place tighter STOP.
      2. Verify exchange-side STOP.
      3. Only after verification, cancel old STOP.
      4. Return verified protection information.

    Local trailing state must NOT be committed by this function.
    """

    if not LIVE_MODE or not ORDERS_ENABLED:
        return None

    if candidate is None or quantity is None:
        return None

    try:
        candidate = float(candidate)
        current_sl = float(current_sl) if current_sl is not None else None
    except (TypeError, ValueError):
        return None

    # Defensive ratchet check.
    if current_sl is not None:
        if position_side == "BUY" and candidate <= current_sl:
            return None
        if position_side == "SELL" and candidate >= current_sl:
            return None

    print(
        f"[TRAILING] {symbol} {position_side}: "
        f"placing tighter STOP at {candidate:.8f}"
    )

    new_order = place_exchange_stop_loss(
        symbol,
        position_side,
        candidate,
        quantity,
    )

    if new_order is None or new_order.get("ambiguous"):
        print(
            f"[TRAILING WARNING] {symbol}: new trailing STOP "
            f"could not be confirmed after placement."
        )
        return None

    verification = verify_exchange_stop_loss(
        symbol,
        position_side,
        candidate,
    )

    if verification is None or not verification.get("verified"):
        print(
            f"[TRAILING WARNING] {symbol}: new trailing STOP "
            f"failed exchange verification. Local trailing state NOT committed."
        )
        return None

    verified_trigger = verification.get("trigger_price")
    try:
        verified_trigger = float(verified_trigger)
    except (TypeError, ValueError):
        verified_trigger = None

    if verified_trigger is None:
        print(
            f"[TRAILING WARNING] {symbol}: verified STOP has no valid "
            f"trigger price. Local trailing state NOT committed."
        )
        return None

    # The verified protection itself must be tighter than the current
    # local protection. This prevents accidentally accepting the old
    # STOP as the replacement.
    if current_sl is not None:
        if position_side == "BUY" and verified_trigger <= current_sl:
            print(
                f"[TRAILING WARNING] {symbol}: verified trigger "
                f"{verified_trigger:.8f} is not tighter than current "
                f"SL {current_sl:.8f}. Local state NOT committed."
            )
            return None

        if position_side == "SELL" and verified_trigger >= current_sl:
            print(
                f"[TRAILING WARNING] {symbol}: verified trigger "
                f"{verified_trigger:.8f} is not tighter than current "
                f"SL {current_sl:.8f}. Local state NOT committed."
            )
            return None

    placed_order_id = new_order.get("order_id")

    # SAFETY:
    # When the exchange gives us an order ID for the newly placed STOP,
    # verification must identify that exact order. A different pre-existing
    # STOP is not sufficient proof that our replacement was accepted.
    verified_order_id = verification.get("order_id")

    if placed_order_id is not None:
        if verified_order_id is None:
            print(
                f"[TRAILING WARNING] {symbol}: exchange verification "
                f"did not return the newly placed STOP order ID. "
                f"Old STOP will NOT be cancelled."
            )
            return None

        if str(verified_order_id) != str(placed_order_id):
            print(
                f"[TRAILING WARNING] {symbol}: verified STOP order "
                f"{verified_order_id} does not match newly placed STOP "
                f"{placed_order_id}. Old STOP will NOT be cancelled."
            )
            return None

    new_order_id = verified_order_id or placed_order_id

    # IMPORTANT:
    # New protection has already been verified and, when an order ID
    # was supplied by placement, its exact identity has also been
    # verified. Only now request cancellation of the old STOP.
    if old_sl_order_id and old_sl_order_id != new_order_id:
        cancel_result = cancel_order(old_sl_order_id)

        if cancel_result is None:
            print(
                f"[TRAILING WARNING] {symbol}: old STOP "
                f"{old_sl_order_id} could not be cancelled. "
                f"Verified new STOP remains active."
            )
        else:
            print(
                f"[TRAILING] {symbol}: old STOP "
                f"{old_sl_order_id} cancellation requested."
            )

    print(
        f"[TRAILING VERIFIED] {symbol} {position_side}: "
        f"STOP={verified_trigger:.8f} order_id={new_order_id}"
    )

    return {
        "verified": True,
        "trigger_price": verified_trigger,
        "order_id": new_order_id,
    }


def _commit_trailing_state(symbol, trailing_info, protection=None):
    """
    Commit trailing state only after the new LIVE STOP has been
    verified, or immediately for PAPER/local trailing.
    """
    if trailing_info is None:
        return

    with state_lock:
        state = symbol_states.get(symbol)

        if not state or not state.get("in_position"):
            return

        trigger = float(trailing_info["trigger"])
        peak = float(trailing_info["peak"])

        state["trailing_armed"] = bool(trailing_info.get("armed", True))
        state["trailing_stage"] = str(trailing_info.get("stage") or "ACTIVE")
        state["trailing_active"] = state["trailing_stage"] == "ACTIVE"
        state["trailing_peak"] = peak
        state["trailing_sl"] = trigger
        state["position_sl"] = trigger
        state["strategy_sl"] = trigger
        state["active_sl"] = trigger
        state["protection_type"] = (
            "TRAILING" if state["trailing_active"] else "TRAILING_ARMED"
        )

        if protection is not None:
            verified_trigger = protection.get("trigger_price")
            if verified_trigger is not None:
                trigger = float(verified_trigger)
                state["trailing_sl"] = trigger
                state["position_sl"] = trigger
                state["strategy_sl"] = trigger
                state["active_sl"] = trigger

            state["sl_order_id"] = protection.get("order_id")
            state["trailing_last_order_id"] = protection.get("order_id")
            state["sl_status"] = "VERIFIED"
            state["protection_verified_at"] = time.time()
            state["protection_last_check"] = time.time()

        entry = state.get("actual_entry") or state.get("position_entry")
        qty = state.get("actual_qty") or state.get("position_qty")
        margin = state.get("position_margin")

        if entry:
            state["sl_distance_pct"] = (
                abs(float(entry) - float(state["position_sl"]))
                / float(entry) * 100.0
            )

            # Keep the dashboard/risk state synchronized with the
            # newly committed trailing protection.
            state["sl_capital_risk_pct"] = _actual_sl_risk_pct(
                float(entry),
                float(state["position_sl"]),
                qty,
                margin,
            )

        print(
            f"[TRAILING ACTIVE] {symbol} {state.get('position_side')}: "
            f"peak={peak:.8f} SL={float(state['position_sl']):.8f}"
        )


# ============================================================
# CORE PRICE-DRIVEN LOGIC (per symbol)
# ============================================================

def handle_new_price(symbol, price, bid=None, ask=None, mark=None, trade_time=None):
    """
    Updates live price state for `symbol`, manages its open
    position's exit, and looks for fresh entry signals. Called
    once per REST ticker poll for every tracked symbol.
    """

    try:

        if price is None:
            return

        with state_lock:

            if symbol not in symbol_states:
                return

            state = symbol_states[symbol]

            old_price = state["previous_live_price"]

            state["live_price"] = price

            if bid is not None:
                state["bid_price"] = bid
            if ask is not None:
                state["ask_price"] = ask
            if mark is not None:
                state["mark_price"] = mark
            if trade_time is not None:
                state["last_trade_time"] = trade_time

            current = (
                dict(state["current_candle"])
                if state["current_candle"] is not None
                else None
            )

            previous = (
                dict(state["previous_closed_candle"])
                if state["previous_closed_candle"] is not None
                else None
            )

            current_signal_candle = state["signal_candle_start"]
            already_triggered = state["signal_triggered"]

            # --------------------------------------------------
            # STEP 1: If a position is open on this symbol, ONLY
            # manage the exit. No new entries evaluated for it.
            # The actual close_position() call (which can place a
            # real closing order over the network) happens AFTER
            # this lock is released - see below.
            # --------------------------------------------------

            in_position_now = state["in_position"]
            pending_exit = None
            trailing_action = None

            if in_position_now:

                # --------------------------------------------------
                # TRAILING STOP
                # --------------------------------------------------
                # Calculate only under state_lock. No network call
                # is allowed here.
                #
                # If an opposite exit is triggered on this same tick,
                # trailing replacement is skipped because the position
                # is already scheduled to close.
                # --------------------------------------------------

                side_now = state.get("position_side")
                entry_now = state.get("actual_entry") or state.get("position_entry")
                current_sl_now = state.get("position_sl")
                trailing_peak_now = state.get("trailing_peak")
                trailing_armed_now = bool(state.get("trailing_armed"))
                qty_now = state.get("actual_qty") or state.get("position_qty")
                margin_now = state.get("position_margin")
                leverage_now = state.get("actual_leverage")
                position_size_now = state.get("position_size")
                old_sl_order_id_now = state.get("sl_order_id")

                trailing_candidate = _calculate_trailing_candidate(
                    side_now,
                    entry_now,
                    price,
                    current_sl_now,
                    trailing_peak_now,
                    leverage=leverage_now,
                    margin=margin_now,
                    position_size=position_size_now,
                    trailing_armed=trailing_armed_now,
                )

                if trailing_candidate is not None:

                    if LIVE_MODE and ORDERS_ENABLED:

                        trailing_action = {
                            "candidate": trailing_candidate,
                            "quantity": qty_now,
                            "old_sl_order_id": old_sl_order_id_now,
                            "current_sl": current_sl_now,
                        }

                    else:
                        # PAPER/local trailing needs no exchange call.
                        state["trailing_armed"] = bool(trailing_candidate.get("armed", True))
                        state["trailing_stage"] = str(trailing_candidate.get("stage") or "ACTIVE")
                        state["trailing_active"] = state["trailing_stage"] == "ACTIVE"
                        state["trailing_peak"] = trailing_candidate["peak"]
                        state["trailing_sl"] = trailing_candidate["trigger"]
                        state["position_sl"] = trailing_candidate["trigger"]
                        state["strategy_sl"] = trailing_candidate["trigger"]
                        state["active_sl"] = trailing_candidate["trigger"]
                        state["protection_type"] = (
                            "TRAILING" if state["trailing_active"] else "TRAILING_ARMED"
                        )

                should_exit, exit_price, reason = check_position_exit(
                    symbol, price, current, previous
                )

                if should_exit:
                    pending_exit = (exit_price, reason)
                    trailing_action = None

            state["previous_live_price"] = price

            # --------------------------------------------------
            # STEP 2: Only look for NEW entries if this symbol is
            # still part of the active watchlist AND not already
            # in a position.
            # --------------------------------------------------

            symbol_is_watched = (not in_position_now) and (symbol in watchlist)

        # Lock released above. Close the position (if flagged) OUTSIDE
        # the lock - close_position() places the real closing order
        # over the network itself and manages its own locking, so
        # this never blocks other symbols' price handling.
        if pending_exit is not None:
            close_position(symbol, pending_exit[0], pending_exit[1])

        # --------------------------------------------------------
        # LIVE trailing STOP replacement.
        #
        # Network calls are deliberately outside state_lock:
        #   1. place new tighter STOP
        #   2. verify new STOP
        #   3. cancel old STOP
        #   4. commit local trailing state
        #
        # If verification fails, local trailing state is untouched.
        # --------------------------------------------------------

        if (
            in_position_now
            and pending_exit is None
            and trailing_action is not None
            and LIVE_MODE
            and ORDERS_ENABLED
        ):

            live_trailing = _replace_live_trailing_stop(
                symbol,
                trailing_action["candidate"]["side"],
                trailing_action["candidate"]["trigger"],
                trailing_action["quantity"],
                trailing_action["old_sl_order_id"],
                trailing_action["current_sl"],
            )

            if live_trailing is not None:

                committed_trailing = dict(trailing_action["candidate"])

                # The exchange-verified trigger is the source of truth
                # for the committed local protection level.
                committed_trailing["trigger"] = live_trailing["trigger_price"]

                _commit_trailing_state(
                    symbol,
                    committed_trailing,
                    protection=live_trailing,
                )

        if in_position_now:
            return

        # ----------------------------------------------------
        # Required data for cross detection
        # ----------------------------------------------------

        if (
            not symbol_is_watched
            or current is None
            or previous is None
            or old_price is None
        ):

            with state_lock:
                symbol_states[symbol]["previous_live_price"] = price

            return

        current_start = current["start_time"]

        # ----------------------------------------------------
        # Only one signal per current 5-minute candle
        # ----------------------------------------------------

        if (
            already_triggered
            and current_signal_candle == current_start
        ):

            with state_lock:
                symbol_states[symbol]["previous_live_price"] = price

            return

        # ----------------------------------------------------
        # ENTRY: current candle BODY crosses previous candle OPEN.
        #
        # The previous candle may be GREEN, RED or DOJI. Its colour is
        # irrelevant for entry. Direction comes only from the current
        # candle body crossing the previous candle OPEN.
        # ----------------------------------------------------

        previous_open = float(previous["open"])
        previous_high = float(previous["high"])
        previous_low = float(previous["low"])
        current_open = float(current["open"])

        crossed_up = current_open <= previous_open < price
        crossed_down = current_open >= previous_open > price

        side = "BUY" if crossed_up else ("SELL" if crossed_down else None)

        if side is not None:
            trigger = previous_open
            stop_loss = previous_low if side == "BUY" else previous_high
            should_attempt_open = False

            with state_lock:
                state = symbol_states[symbol]

                can_open = can_afford_new_position(symbol, price)
                risk_allowed = sl_risk_allowed(
                    trigger,
                    stop_loss,
                    symbol=symbol,
                    side=side,
                )

                if (
                    (
                        not state["signal_triggered"]
                        or state["signal_candle_start"] != current_start
                    )
                    and not state["in_position"]
                    and can_open
                    and risk_allowed
                ):
                    state["signal"] = side
                    state["entry_price"] = trigger
                    state["stop_loss"] = stop_loss
                    state["signal_candle_start"] = current_start
                    should_attempt_open = True

            if should_attempt_open:
                prev_colour = (
                    "DOJI"
                    if previous["close"] == previous["open"]
                    else ("GREEN" if previous["close"] > previous["open"] else "RED")
                )

                print()
                print("=" * 68)
                print(f">>> {side} SIGNAL TRIGGERED: {symbol} <<<")
                print("=" * 68)
                print(f"Previous Candle    : {prev_colour}")
                print(f"Previous OPEN      : {trigger:.8f}")
                print(f"Current OPEN       : {current_open:.8f}")
                print(f"Live Price         : {price:.8f}")
                print(f"Entry              : {trigger:.8f}")
                print(f"Stop Loss          : {stop_loss:.8f}")
                raw_risk = abs(trigger - stop_loss) / trigger * 100.0
                print(f"SL Price Risk      : {raw_risk:.3f}%")
                print(f"SL Capital Risk    : {raw_risk * TRADE_LEVERAGE:.3f}% @ configured {TRADE_LEVERAGE}x")
                print(f"Leverage           : configured {TRADE_LEVERAGE}x")
                print("Quantity           : EXCHANGE MINIMUM")
                print("Orders             : ENABLED" if ORDERS_ENABLED else "Orders             : DISABLED")
                print("=" * 68)

                opened = open_position(
                    symbol,
                    side,
                    trigger,
                    stop_loss,
                    current_start,
                )

                if opened:
                    with state_lock:
                        symbol_states[symbol]["signal_triggered"] = True

        with state_lock:
            symbol_states[symbol]["previous_live_price"] = price
        with state_lock:
            symbol_states[symbol]["previous_live_price"] = price

    except Exception as e:

        print(f"[Price Handler Error] ({symbol}) {e}")


def detect_cross_candidate(symbol, data):
    """Read-only body-cross snapshot used to rank simultaneous entries."""
    try:
        price = data.get("price")
        if price is None:
            return None

        with state_lock:
            state = symbol_states.get(symbol)
            if not state or symbol not in watchlist or state["in_position"]:
                return None
            if state["previous_closed_candle"] is None or state["current_candle"] is None:
                return None
            if state["signal_triggered"]:
                return None

            prev = dict(state["previous_closed_candle"])
            current = dict(state["current_candle"])
            current_start = current["start_time"]

        reference_open = float(prev["open"])
        current_open = float(current["open"])

        # Previous candle colour is deliberately ignored. DOJI is valid.
        if current_open <= reference_open < float(price):
            side = "BUY"
        elif current_open >= reference_open > float(price):
            side = "SELL"
        else:
            return None

        allowed, score, reason = hft_entry_gate(symbol, side)
        if not allowed:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "score": score,
            "reason": reason,
            "candle": current_start,
        }
    except Exception:
        return None


def ticker_poll_loop():
    """Poll live prices and give the strongest simultaneous crossover first priority."""
    global entry_priority_symbol, entry_priority_score

    while True:
        all_data = fetch_all_pairs_ticker()

        if all_data:
            with state_lock:
                tracked = list(subscribed_symbols)

            # Snapshot all possible crossovers BEFORE mutating previous_live_price.
            candidates = []
            blocked, _, _, _ = funding_status()
            if not blocked:
                for symbol in tracked:
                    data = all_data.get(symbol)
                    if data is None:
                        continue
                    candidate = detect_cross_candidate(symbol, data)
                    if candidate:
                        candidates.append(candidate)

            candidates.sort(key=lambda x: x["score"], reverse=True)
            winner = candidates[0] if candidates else None
            entry_priority_symbol = winner["symbol"] if winner else None
            entry_priority_score = winner["score"] if winner else None

            for symbol in tracked:
                data = all_data.get(symbol)
                if data is None:
                    continue
                handle_new_price(
                    symbol,
                    data["price"],
                    bid=data["bid"],
                    ask=data["ask"],
                    mark=data["mark"],
                    trade_time=data["timestamp"],
                )

            entry_priority_symbol = None
            entry_priority_score = None

        time.sleep(TICKER_POLL_SECONDS)


# ============================================================
# KLINE WEBSOCKET

@sio.on("FETCH_CANDLESTICK_CS_PRO", namespace=NAMESPACE)
def on_kline(data):

    try:

        if not isinstance(data, dict):
            return

        symbol = data.get("s")
        start_time = to_int(data.get("t"))
        open_price = to_float(data.get("o"))
        high_price = to_float(data.get("h"))
        low_price = to_float(data.get("l"))
        close_price = to_float(data.get("c"))
        volume = to_float(data.get("v"))

        candle_closed = bool(data.get("x", False))

        if (
            symbol is None
            or start_time is None
            or open_price is None
            or high_price is None
            or low_price is None
            or close_price is None
        ):
            return

        new_candle = {
            "start_time": start_time,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }

        with state_lock:

            if symbol not in symbol_states:
                # Not (or no longer) something we track - ignore.
                return

            state = symbol_states[symbol]

            old_candle = (
                dict(state["current_candle"])
                if state["current_candle"] is not None
                else None
            )

            # ------------------------------------------------
            # NEW 5-MINUTE CANDLE
            # ------------------------------------------------

            if (
                old_candle is not None
                and old_candle["start_time"] != start_time
            ):

                state["previous_closed_candle"] = dict(old_candle)

                # New candle gets a fresh signal window. This does
                # NOT touch an already-open position - it stays
                # open across candle boundaries (no fixed TP).
                state["signal"] = "WAIT"
                state["entry_price"] = None
                state["stop_loss"] = None
                state["signal_candle_start"] = None
                state["signal_triggered"] = False

                if state["live_price"] is not None:
                    state["previous_live_price"] = state["live_price"]

            # ------------------------------------------------
            # EXPLICIT CLOSED CANDLE
            # ------------------------------------------------

            if candle_closed:
                state["previous_closed_candle"] = dict(new_candle)

            state["current_candle"] = new_candle

    except Exception as e:

        print(f"[Kline Handler Error] {e}")


# ============================================================
# TICKER WEBSOCKET
# ============================================================

@sio.on("FETCH_TICKER_INFO_CS_PRO", namespace=NAMESPACE)
def on_ticker(data):
    """
    Live-price push from the official Futures Ticker WebSocket.
    Feeds the same handle_new_price() pipeline that ticker_poll_loop()
    (REST) already feeds, so a WS push and a REST poll for the same
    symbol are handled identically - handle_new_price() only acts on
    price CHANGES relative to the last stored price, so getting an
    update from two sources is safe, just more responsive.

    NOTE: CoinSwitch's public docs for this event show the subscribe
    call but not a field-by-field schema for the push payload, so
    this parses defensively - it tries several plausible key names
    (matching the same defensive style already used elsewhere in this
    file, e.g. extract_actual_fee()) rather than assuming one exact
    shape. If price still doesn't move on this feed, print(data) once
    to see the real keys and adjust the *_KEYS tuples below.
    """

    try:

        if not isinstance(data, dict):
            return

        symbol = data.get("s") or data.get("m") or data.get("symbol")
        if not symbol:
            return

        symbol = str(symbol).upper().replace("/", "")

        PRICE_KEYS = ("last_price", "price", "c", "p", "ltp")
        BID_KEYS = ("best_bid_price", "bid", "b")
        ASK_KEYS = ("best_ask_price", "ask", "a")
        MARK_KEYS = ("mark_price", "mark", "i")
        TIME_KEYS = ("timestamp", "t", "E", "trade_time")

        def first_float(keys):
            for k in keys:
                v = to_float(data.get(k))
                if v is not None:
                    return v
            return None

        def first_int(keys):
            for k in keys:
                v = to_int(data.get(k))
                if v is not None:
                    return v
            return None

        price = first_float(PRICE_KEYS)
        if price is None:
            return

        handle_new_price(
            symbol,
            price,
            bid=first_float(BID_KEYS),
            ask=first_float(ASK_KEYS),
            mark=first_float(MARK_KEYS),
            trade_time=first_int(TIME_KEYS),
        )

    except Exception as e:

        print(f"[Ticker Handler Error] {e}")


# ============================================================
# WEBSOCKET CONNECTION
# ============================================================

@sio.event(namespace=NAMESPACE)
def connect():

    global ws_connected

    ws_connected = True

    print()
    print("[WebSocket] CONNECTED")
    print(
        "[WebSocket] Waiting for watchlist refresh to subscribe "
        "to candle streams..."
    )


@sio.event(namespace=NAMESPACE)
def disconnect():

    global ws_connected

    ws_connected = False

    print()
    print("[WebSocket] DISCONNECTED")


@sio.event(namespace=NAMESPACE)
def connect_error(data):

    global ws_connected

    ws_connected = False

    print()
    print(f"[WebSocket] CONNECTION ERROR: {data}")


# ============================================================
# DISPLAY
# ============================================================
#
# Pure presentation layer for the terminal dashboard. Nothing here
# reads/writes trading state or changes any strategy/logic — it only
# formats the same values that were already computed in
# display_market() into a colored, boxed, mobile-friendly layout.

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    WHITE = "\033[97m"
    CYAN = "\033[96m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    GRAY = "\033[90m"
    MAGENTA = "\033[95m"

    # Dashboard theme
    DARK_GREEN = "\033[32m"       # Positive P&L
    DARK_BLUE = "\033[34m"        # Funding / dashboard accent
    AMBER = "\033[38;5;214m"      # DOJI / neutral signal


def _vlen(text):
    """Visible length of a string, ignoring ANSI color codes."""
    return len(_ANSI_RE.sub("", text))


def _pad(text, width):
    pad = width - _vlen(text)
    return text + (" " * pad if pad > 0 else "")


def _color(text, color):
    return f"{color}{text}{Ansi.RESET}"


def _pnl_color(value):
    if value > 0:
        return Ansi.DARK_GREEN
    if value < 0:
        return Ansi.RED
    return Ansi.GRAY


def _dashboard_width():
    try:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        cols = 80
    return max(20, cols)
# ---- outer box (white, double border) ----

def _outer_top(w):
    return _color("╔" + "═" * (w - 2) + "╗", Ansi.WHITE)


def _outer_bottom(w):
    return _color("╚" + "═" * (w - 2) + "╝", Ansi.WHITE)


def _outer_div(w):
    return _color("╟" + "─" * (w - 2) + "╢", Ansi.WHITE)


def _outer_line(w, text=""):
    content_w = w - 4
    return (
        _color("║", Ansi.WHITE) + " " + _pad(text, content_w) + " " + _color("║", Ansi.WHITE)
    )


def _outer_title(w, text, color=Ansi.BOLD + Ansi.YELLOW):
    content_w = w - 4
    stripped = _vlen(text)
    total_pad = max(0, content_w - stripped)
    left = total_pad // 2
    right = total_pad - left
    centered = (" " * left) + _color(text, color) + (" " * right)
    return _outer_line(w, centered)


def _outer_blank(w):
    return _outer_line(w, "")


# ---- inner box (cyan, single border) nested inside the outer box ----

def _inner_top(w, label=""):
    content_w = w - 4
    if label:
        lbl = f" {_color(label, Ansi.BOLD + Ansi.CYAN)} "
        dash_total = max(0, content_w - 2 - _vlen(lbl))
        left = 2
        right = max(0, dash_total - left)
        line = (
            _color("┌" + "─" * left, Ansi.WHITE)
            + lbl
            + _color("─" * right + "┐", Ansi.WHITE)
        )
    else:
        line = _color("┌" + "─" * (content_w - 2) + "┐", Ansi.WHITE)
    return _outer_line(w, line)


def _inner_bottom(w):
    content_w = w - 4
    return _outer_line(
        w,
        _color("└" + "─" * (content_w - 2) + "┘", Ansi.WHITE)
    )


def _inner_line(w, text=""):
    content_w = w - 4
    inner_w = content_w - 4
    body = (
        _color("│", Ansi.WHITE) + " " + _pad(text, inner_w) + " " + _color("│", Ansi.WHITE)
    )
    return _outer_line(w, body)


def _inner_wrapped(w, label, symbols, color, empty_text="none", bold=True):
    """Yield inner-box lines for a label + a wrapped, comma-joined symbol list."""
    content_w = w - 4
    inner_w = content_w - 4
    count = len(symbols)
    header_style = (Ansi.BOLD + color) if bold else color
    header = _color(f"{label} ({count}):", header_style)
    lines = [_inner_line(w, header)]
    if not symbols:
        lines.append(_inner_line(w, _color(f"  {empty_text}", Ansi.GRAY)))
        return lines
    joined = ", ".join(symbols)
    for wrapped in textwrap.wrap(joined, width=max(10, inner_w - 2)) or [""]:
        lines.append(_inner_line(w, "  " + wrapped))
    return lines


def _split_line(w, left, right):
    """Outer-box line with `left` flush left and `right` flush right on the
    same row, fully visible (no truncation) with the gap between filled in."""
    content_w = w - 4
    lv, rv = _vlen(left), _vlen(right)
    gap = max(1, content_w - lv - rv)
    return _outer_line(w, left + (" " * gap) + right)


def _pair_row(w, seg1, seg2, connector=" \u2502 "):
    """Two small connected 'boxes' (seg1, seg2) on one inner-box row when
    they fit; falls back to one segment per row if the combo is too wide."""
    content_w = w - 4
    inner_w = content_w - 4
    combined = seg1 + connector + seg2
    if _vlen(combined) <= inner_w:
        return [_inner_line(w, combined)]
    return [_inner_line(w, seg1), _inner_line(w, seg2)]


def display_market():
    with state_lock:
        connected = ws_connected
        watch_count = len(watchlist)
        tracked_count = len(subscribed_symbols)
        refresh_ts = last_watchlist_refresh
        open_positions = []

        for symbol, state in symbol_states.items():
            if state["in_position"]:
                # Display price can remain the live ticker price.
                # PnL itself MUST use the exchange mark price.
                display_price = state.get("live_price")
                pnl_price = state.get("mark_price") or display_price

                entry = state["position_entry"]
                side = state["position_side"]

                # Entry leverage is only used when opening a new position.
                # Existing-position PnL uses actual exchange leverage/margin.
                actual_leverage = state.get("actual_leverage")
                actual_margin = state.get("position_margin")

                if actual_leverage is None:
                    actual_leverage = 0.0
                else:
                    actual_leverage = float(actual_leverage)

                if actual_margin is None:
                    actual_margin = (
                        state["position_size"] / actual_leverage
                        if state.get("position_size") and actual_leverage > 0
                        else 0.0
                    )

                if LIVE_MODE:
                    # LIVE dashboard P&L must come directly from
                    # CoinSwitch exchange-reported unrealised P&L.
                    unreal = to_float(
                        state.get("exchange_unrealised_pnl")
                    )
                    if unreal is None:
                        unreal = 0.0
                else:
                    unreal = (
                        calculate_leveraged_pnl_pct(
                            entry,
                            pnl_price,
                            side,
                            leverage=actual_leverage,
                            margin=actual_margin,
                            position_size=state.get("position_size"),
                        )
                        if pnl_price is not None and entry
                        else 0.0
                    )

                open_positions.append({
                    "symbol": symbol, "side": side, "entry": entry, "price": display_price,
                    "sl": state.get("active_sl") or state.get("position_sl"),
                    "sl_type": state.get("protection_type") or (
                        "TRAILING" if state.get("trailing_active") else "INITIAL"
                    ),
                    "peak": state.get("trailing_peak"),
                    "size": state["position_size"],
                    "qty": state["position_qty"],
                    "unreal": unreal,
                    "exchange_unrealised_pnl": state.get(
                        "exchange_unrealised_pnl"
                    ),
                    "margin": actual_margin,
                    "leverage": actual_leverage,
                    "scanner_score": state.get("scanner_score"),
                    "scanner_quality": state.get("scanner_quality"),
                })

        # Mode-specific trade source for dashboard P&L/history.
        # PAPER ON -> paper data only
        # LIVE ON  -> live data only
        # Both OFF -> no P&L/history data
        if PAPER_MODE:
            pnl_history = paper_trade_log
        elif LIVE_MODE:
            pnl_history = live_trade_log
        else:
            pnl_history = []

        # Only active-mode trades created in this terminal session are displayed.
        closed_trades = [
            t for t in pnl_history
            if float(t.get("closed_at") or 0) >= SESSION_STARTED_AT
        ]

        # Current-session metrics only. The session starts when this bot
        # process starts; older persisted trades are not included.
        session_gross = sum(
            float(t.get("pnl_usdt") or 0.0)
            for t in closed_trades
        )
        session_fees = sum(
            float(t.get("fee") or 0.0)
            for t in closed_trades
        )
        session_pnl = session_gross - session_fees

        historical_count = len(pnl_history)

        missing_price, missing_candles = [], []
        red_symbols, green_symbols, doji_symbols = [], [], []
        for symbol in watchlist:
            state = symbol_states.get(symbol)
            if state is None:
                missing_price.append(symbol); missing_candles.append(symbol); continue
            if state["live_price"] is None:
                missing_price.append(symbol)
            prev = state["previous_closed_candle"]
            if prev is None:
                missing_candles.append(symbol); continue
            if state["in_position"]:
                continue
            if prev["close"] < prev["open"]: red_symbols.append(symbol)
            elif prev["close"] > prev["open"]: green_symbols.append(symbol)
            else: doji_symbols.append(symbol)

    deployed = sum(float(p.get("size") or 0.0) for p in open_positions)
    deployed_margin = sum(float(p.get("margin") or 0.0) for p in open_positions)
    total_capital = effective_total_capital()
    available_capital = effective_available_capital()
    max_margin = max_deployable_margin()
    used_pct = (deployed_margin / total_capital * 100.0) if total_capital > 0 else 0.0
    blocked, funding_time, next_funding, _ = funding_status()
    # Account / execution status for dashboard
    orders_label = "ON" if ORDERS_ENABLED else "OFF"
    paper_label = "ON" if PAPER_MODE else "OFF"
    live_label = "ON" if LIVE_MODE else "OFF"

    paper_balance = paper_capital if PAPER_MODE else 0.0
    live_balance = account_balance if (LIVE_MODE and account_balance is not None) else 0.0

    # ========================================================
    # P&L SOURCE OF TRUTH
    #
    # LIVE:
    #   CoinSwitch exchange data ONLY.
    #   Never calculate LIVE P&L from local entry/exit/price.
    #
    # PAPER:
    #   Existing local paper-trade accounting remains unchanged.
    # ========================================================

    if LIVE_MODE:
        # ----------------------------------------------------
        # LIVE UNREALIZED P&L
        # ----------------------------------------------------
        # Each live position is populated from the exchange
        # position snapshot. The exchange-reported unrealised
        # P&L is the ONLY LIVE USDT P&L value accepted here.
        unrealized_pnl_usdt = 0.0
        exchange_unrealized_count = 0

        for p in open_positions:
            state = symbol_states.get(p["symbol"], {})
            exchange_pnl = state.get("exchange_unrealised_pnl")

            try:
                if exchange_pnl is not None:
                    unrealized_pnl_usdt += float(exchange_pnl)
                    exchange_unrealized_count += 1
            except (TypeError, ValueError):
                pass

        # Never fall back to local price/entry math in LIVE mode.
        if open_positions and exchange_unrealized_count == len(open_positions):
            unrealized_pnl_pct = (
                unrealized_pnl_usdt / deployed_margin * 100.0
                if deployed_margin > 0
                else 0.0
            )
        else:
            # Exchange P&L is incomplete/unavailable.
            # Do NOT invent a local estimate.
            unrealized_pnl_usdt = 0.0
            unrealized_pnl_pct = 0.0

        # ----------------------------------------------------
        # LIVE REALIZED P&L
        # ----------------------------------------------------
        # Read CoinSwitch transaction history. Do not use the
        # locally calculated closed-trade P&L as LIVE truth.
        realized_pnl_usdt = 0.0
        realized_pnl_pct = 0.0
        exchange_realized_available = False
        exchange_realized_rows = []

        try:
            tx_payload = get_live_transactions(limit=100)

            def _collect_exchange_realized(obj):
                rows = []

                if isinstance(obj, dict):
                    for k, v in obj.items():
                        key = str(k).lower().replace("-", "_").replace(" ", "_")

                        if key in {
                            "realized_pnl",
                            "realised_pnl",
                            "realized_profit",
                            "realised_profit",
                            "realized_pnl_usdt",
                            "realised_pnl_usdt",
                        }:
                            n = to_float(v)
                            if n is not None:
                                rows.append(n)

                        rows.extend(_collect_exchange_realized(v))

                elif isinstance(obj, list):
                    for item in obj:
                        rows.extend(_collect_exchange_realized(item))

                return rows

            exchange_realized_rows = _collect_exchange_realized(tx_payload)

            if exchange_realized_rows:
                realized_pnl_usdt = sum(exchange_realized_rows)
                exchange_realized_available = True

                # Percentage is exchange P&L divided by exchange-
                # reported/local-synced live margin for the session.
                realized_pnl_pct = (
                    realized_pnl_usdt / realized_margin * 100.0
                    if realized_margin > 0
                    else 0.0
                )

        except Exception as e:
            print(f"[LIVE P&L] Exchange realized-P&L read failed: {e}")

        # If CoinSwitch did not expose realized P&L in the
        # transaction response, keep it at zero rather than
        # substituting the bot's local entry/exit calculation.
        if not exchange_realized_available:
            realized_pnl_usdt = 0.0
            realized_pnl_pct = 0.0

        # LIVE closed-trade win count must not be inferred from
        # local P&L either.
        wins = 0

    elif PAPER_MODE:
        # ----------------------------------------------------
        # PAPER MODE: local simulated P&L is valid.
        # ----------------------------------------------------
        realized_pnl_usdt = sum(
            float(t.get("pnl_usdt") or 0.0)
            for t in closed_trades
        )

        realized_margin = sum(
            float(t.get("margin") or 0.0)
            for t in closed_trades
        )

        realized_pnl_pct = (
            realized_pnl_usdt / realized_margin * 100.0
            if realized_margin > 0
            else 0.0
        )

        wins = sum(
            1 for t in closed_trades
            if float(t.get("pnl_pct") or 0.0) > 0
        )

        unrealized_pnl_pct = sum(
            float(p.get("unreal") or 0.0)
            for p in open_positions
        )

        unrealized_pnl_usdt = 0.0

        for p in open_positions:
            pnl_price = p.get("price")

            if pnl_price is not None:
                raw_unreal = calculate_raw_pnl_pct(
                    p["entry"],
                    pnl_price,
                    p["side"]
                )
                unrealized_pnl_usdt += (
                    (p["size"] or 0.0) * raw_unreal / 100.0
                )

    else:
        realized_pnl_usdt = 0.0
        realized_pnl_pct = 0.0
        unrealized_pnl_usdt = 0.0
        unrealized_pnl_pct = 0.0
        wins = 0

    total_pnl_pct = realized_pnl_pct + unrealized_pnl_pct
    total_pnl_usdt = realized_pnl_usdt + unrealized_pnl_usdt

    w = _dashboard_width()

    # Shared dashboard dimensions. w MUST be initialized first.
    content_w = w - 4
    inner_w = content_w - 4
    L = ["\033[2J\033[H"]

    # ---- header ----
    L.append(_outer_top(w))
    L.append(_outer_title(w, "HFT PRIORITY + 5M BODY OPEN CROSS", Ansi.BOLD + Ansi.WHITE))
    L.append(_outer_title(w, f"DEFAULT {TRADE_LEVERAGE}x | ACTUAL LEVERAGE ACCOUNTING", Ansi.BOLD + Ansi.WHITE))
    L.append(_outer_div(w))

    ws_color = Ansi.GREEN if connected else Ansi.RED
    ws_text = "CONNECTED" if connected else "DISCONNECTED"
    if LIVE_MODE:
        orders_tag = _color("LIVE", Ansi.BOLD + Ansi.GREEN)
    elif PAPER_MODE:
        orders_tag = _color("PAPER", Ansi.BOLD + Ansi.YELLOW)
    else:
        orders_tag = _color("OFF", Ansi.BOLD + Ansi.GRAY)
    L.append(_split_line(w, f"WebSocket : {_color(ws_text, Ansi.BOLD + ws_color)}", orders_tag))
    L.append(_outer_line(w, f"Time (UTC): {now_string()}"))
    L.append(_outer_line(w, f"Watchlist : {watch_count} active | {tracked_count} tracked"))
    if refresh_ts:
        next_refresh_in = max(0, int(WATCHLIST_REFRESH_SECONDS - (time.time() - refresh_ts)))
        L.append(_outer_line(w, f"Next refresh in ~{next_refresh_in // 60} min"))
    else:
        L.append(_outer_line(w, "Watchlist : not yet refreshed"))

    # ---- scan results (nested nested box = "double layer") ----
    L.append(_outer_div(w))
    data_ok = watch_count - len(set(missing_price) | set(missing_candles))
    L.append(_outer_line(w, _color(f"Scan status: {data_ok}/{watch_count} symbols ready", Ansi.BOLD + Ansi.WHITE)))
    L.append(_inner_top(w, "SCAN RESULTS"))
    L.append(_inner_line(w, "Entry: CURRENT BODY crosses PREVIOUS OPEN"))
    if missing_price:
        L.extend(_inner_wrapped(w, "NO LIVE PRICE", missing_price, Ansi.YELLOW))
    if missing_candles:
        L.extend(_inner_wrapped(w, "NO CANDLE DATA", missing_candles, Ansi.YELLOW))
    L.extend(_inner_wrapped(w, "PREV RED  - valid reference", red_symbols, Ansi.RED, bold=False))
    L.extend(_inner_wrapped(w, "PREV GREEN- valid reference", green_symbols, Ansi.GREEN, bold=False))
    if doji_symbols:
        L.extend(_inner_wrapped(w, "PREV DOJI - valid reference", doji_symbols, Ansi.AMBER, bold=False))
    L.append(_inner_bottom(w))

    # ---- closed trades (numbered, same compact connected-box format) ----
    L.append(_outer_div(w))
    L.append(_inner_top(w, f"CLOSED: {len(closed_trades)} sess / {historical_count} hist"))
    if closed_trades:
        for i, t in enumerate(closed_trades, start=1):
            side_color = Ansi.GREEN if t['side'] == "BUY" else Ansi.RED
            pnl_c = _pnl_color(float(t.get("pnl_pct") or 0.0))
            trade_pnl_str = f"{t['pnl_pct']:.2f}% ({t['pnl_usdt']:+.2f})"
            net = float(t.get('net_pnl_usdt') or 0.0)
            fee = float(t.get('fee') or 0.0)
            L.append(_inner_line(w, _color(f"#{i} [{t['side']}] {t['symbol']}", Ansi.BOLD + side_color)))
            L.append(_inner_line(
                w,
                f"Entry {t['entry']:.4f} | Exit {t['exit']:.4f} | "
                f"PnL {_color(trade_pnl_str, pnl_c)} | Fees {fee:.2f} | "
                f"Net {_color(f'{net:+.2f}', _pnl_color(net))}"
            ))
            for reason_line in textwrap.wrap(f"reason: {t['reason']}", width=max(10, (w - 4) - 4 - 2)) or []:
                L.append(_inner_line(w, "  " + reason_line))
    else:
        L.append(_inner_line(w, _color("none", Ansi.GRAY)))
    L.append(_inner_bottom(w))

    # ---- open positions (numbered, compact connected-box rows) ----
    L.append(_outer_div(w))
    L.append(_inner_top(w, f"OPEN POSITIONS ({len(open_positions)})"))
    if open_positions:
        for i, p in enumerate(open_positions, start=1):
            price_str = f"{p['price']:.4f}" if p.get('price') is not None else "N/A"
            entry_str = f"{p['entry']:.4f}" if p.get('entry') is not None else "N/A"
            sl_str = f"{p['sl']:.4f}" if p.get('sl') is not None else "N/A"
            sl_type = str(p.get('sl_type') or "INITIAL").upper()
            peak_str = f"{p['peak']:.4f}" if p.get('peak') is not None else "N/A"
            qty_str = f"{p['qty']}" if p.get('qty') is not None else "N/A"
            side_color = Ansi.GREEN if p['side'] == "BUY" else Ansi.RED
            margin_p = float(p.get("margin") or 0.0)
            cap_used = (margin_p / total_capital * 100.0) if total_capital else 0.0
            unreal_str = f"{float(p.get('unreal') or 0.0):.2f}%"
            L.append(_inner_line(w, _color(f"#{i} [{p['side']}] {p['symbol']}", Ansi.BOLD + side_color)))
            # Wrap position details to the actual inner-box width so no
            # position data can spill past the right border on narrow
            # Termux/mobile terminals. Keep the fields in the same order.
            position_details = (
                f"Qty {qty_str} | Entry {entry_str} | Price {price_str} | "
                f"SL {sl_str} [{sl_type}] | Peak {peak_str} | "
                f"Margin {margin_p:.2f} ({cap_used:.2f}%) | "
                f"PnL {_color(unreal_str, _pnl_color(p['unreal']))}"
            )
            for detail_line in textwrap.wrap(
                position_details,
                width=max(10, inner_w),
                break_long_words=False,
                break_on_hyphens=False,
            ) or [""]:
                L.append(_inner_line(w, detail_line))

    else:
        L.append(_inner_line(w, _color("none", Ansi.GRAY)))
    L.append(_inner_bottom(w))

    # ---- shared dashboard width calculations ----
    # Must be defined before OPEN/CLOSED/STATUS/CAPITAL/P&L blocks.
    # ---- STATUS ----
    L.append(_outer_div(w))
    L.append(_inner_top(w, "STATUS"))

    orders_status = _color(
        f"[ {orders_label} ]",
        Ansi.GREEN if ORDERS_ENABLED else Ansi.GRAY
    )
    paper_status = _color(
        f"[ {paper_label} ]",
        Ansi.YELLOW if PAPER_MODE else Ansi.GRAY
    )
    live_status = _color(
        f"[ {live_label} ]",
        Ansi.GREEN if LIVE_MODE else Ansi.GRAY
    )

    # Three separate status columns.
    L.append(_inner_line(
        w,
        f"ORDERS {orders_status}   "
        f"PAPER {paper_status}   "
        f"LIVE {live_status}"
    ))

    L.append(_inner_bottom(w))

    # ---- CAPITAL ----
    L.append(_inner_top(w, "CAPITAL"))

    L.append(_inner_line(
        w,
        f"Capital   : {total_capital:.2f} USDT"
    ))
    L.append(_inner_line(
        w,
        f"Available : {available_capital:.2f} USDT"
    ))
    L.append(_inner_line(
        w,
        f"Margin    : {deployed_margin:.2f} / {max_margin:.2f} USDT"
    ))
    if LOW_CAPITAL_MODE:
        cap_line = f"            C-MODE ON | MIN QTY | 10->20->40x | {used_pct:.2f}% used"
    else:
        cap_line = f"            {MAX_ALLOCATION_PCT:.0f}% cap | {used_pct:.2f}% used"

    L.append(_inner_line(w, cap_line))
    L.append(_inner_line(
        w,
        f"Notional  : {deployed:.2f} USDT @ actual leverage"
    ))
    L.append(_inner_line(
        w,
        f"Positions : {len(open_positions)} / UNLIMITED"
    ))
    L.append(_inner_line(
        w,
        f"Max SL    : {MAX_SL_RISK_PCT:.1f}% of margin"
    ))

    fund_color = Ansi.RED if blocked else Ansi.DARK_BLUE
    fund_text = "BLOCKED" if blocked else "ENTRY ACTIVE"

    L.append(_inner_line(
        w,
        f"Funding   : {_color(fund_text, Ansi.BOLD + fund_color)}"
    ))
    L.append(_inner_line(
        w,
        f"Next      : {funding_time.strftime('%H:%M UTC')} "
        f"(±{FUNDING_BLACKOUT_MINUTES}m)"
    ))

    L.append(_inner_bottom(w))

    # ---- P&L ----
    L.append(_inner_top(w, "P&L"))

    realized_value = _color(
        f"{realized_pnl_pct:+.2f}% ({realized_pnl_usdt:+.2f})",
        _pnl_color(realized_pnl_pct)
    )

    unrealized_value = _color(
        f"{unrealized_pnl_pct:+.2f}% ({unrealized_pnl_usdt:+.2f})",
        _pnl_color(unrealized_pnl_pct)
    )

    total_value = _color(
        f"{total_pnl_pct:+.2f}% ({total_pnl_usdt:+.2f})",
        Ansi.BOLD + _pnl_color(total_pnl_pct)
    )

    # Session PnL is NET of session fees, and its percentage is measured
    # against the configured session capital rather than position margin.
    session_pnl_capital_pct = (
        session_pnl / total_capital * 100.0
        if total_capital > 0
        else 0.0
    )

    session_pnl_value = _color(
        f"{session_pnl:+.2f} USDT ({session_pnl_capital_pct:+.2f}% capital)",
        _pnl_color(session_pnl_capital_pct)
    )
    session_fees_value = _color(
        f"{session_fees:.2f} USDT",
        Ansi.RESET
    )

    session_pnl_text = _color(
        f"Session PnL: {session_pnl_value}",
        Ansi.RESET
    )
    session_fees_text = _color(
        f"Fees Sess.: {session_fees_value}",
        Ansi.RESET
    )

    # Two-column rows INSIDE the inner P&L box.
    # Left side stays aligned; right side is flush to the inner-box edge.

    left_realized = f"Realized   : {realized_value}"
    left_unrealized = f"Unrealized : {unrealized_value}"

    right_session_pnl = session_pnl_text
    right_session_fees = session_fees_text

    def _pnl_inner_row(left, right):
        gap = max(1, inner_w - _vlen(left) - _vlen(right))
        return _inner_line(w, left + (" " * gap) + right)

    L.append(_pnl_inner_row(left_realized, right_session_pnl))
    L.append(_pnl_inner_row(left_unrealized, right_session_fees))

    L.append(_inner_line(
        w,
        _color(f"TOTAL      : {total_value}", Ansi.BOLD)
    ))

    if closed_trades:
        L.append(_inner_line(
            w,
            f"Win rate   : {wins}/{len(closed_trades)}"
        ))

    L.append(_inner_bottom(w))

    # ---- keyboard controls / dashboard tip ----
    L.append(_inner_line(
        w,
        "TIP: P=Paper | L=Live | O=Orders | R=Recovery | E=Exit All | Q=Quit"
    ))

    L.append(_outer_bottom(w))

    # ---- fixed top-anchored dashboard redraw ----
    print("\033[2J\033[H", end="")
    print("\n".join(L))


def _keyboard_control_loop():
    """
    Non-blocking terminal keyboard controls for the dashboard.

    P = toggle paper mode
    L = toggle live mode (requires Y confirmation when enabling)
    O = toggle order execution
    C = toggle LOW CAPITAL MODE (min qty, 10x -> 20x -> 40x)
    R = run LIVE exchange recovery
    E = emergency/manual exit ALL open positions
    Q = stop the bot
    """
    import sys
    import select
    import termios
    import tty

    global PAPER_MODE, LIVE_MODE, ORDERS_ENABLED, LOW_CAPITAL_MODE

    fd = sys.stdin.fileno()

    try:
        old_settings = termios.tcgetattr(fd)
    except Exception as e:
        print(f"[Keyboard] Terminal control unavailable: {e}")
        return

    try:
        tty.setcbreak(fd)

        while True:
            readable, _, _ = select.select([sys.stdin], [], [], 0.2)

            if not readable:
                continue

            key = sys.stdin.read(1).lower()

            # -------------------------------------------------
            # Q = STOP
            # -------------------------------------------------
            if key == "q":
                print("\n[Keyboard] Q pressed - stopping bot...")

                try:
                    sio.disconnect()
                except Exception:
                    pass

                return

            # -------------------------------------------------
            # P = PAPER TOGGLE
            # -------------------------------------------------
            if key == "p":

                if PAPER_MODE:
                    PAPER_MODE = False
                    print("\n[Keyboard] PAPER_MODE = OFF")

                else:
                    if LIVE_MODE:
                        LIVE_MODE = False
                        print("\n[Keyboard] LIVE_MODE = OFF")

                    PAPER_MODE = True
                    print("\n[Keyboard] PAPER_MODE = ON")

                continue

            # -------------------------------------------------
            # L = LIVE TOGGLE
            # -------------------------------------------------
            if key == "l":

                if LIVE_MODE:
                    LIVE_MODE = False
                    print("\n[Keyboard] LIVE_MODE = OFF")
                    continue

                print("\n" + "!" * 68)
                print("!!! LIVE MODE REQUESTED !!!")
                print("LIVE mode can use REAL CoinSwitch futures execution.")
                print("This does NOT automatically enable ORDERS.")
                print("Press Y to confirm LIVE_MODE ON.")
                print("Any other key cancels.")
                print("!" * 68)

                confirm = sys.stdin.read(1).lower()

                if confirm == "y":

                    PAPER_MODE = False
                    LIVE_MODE = True

                    print("[Keyboard] LIVE_MODE = ON")

                    try:
                        refresh_account_balance()
                    except Exception as e:
                        print(f"[Keyboard] Balance refresh failed: {e}")

                    # -------------------------------------------------
                    # Automatic LIVE exchange recovery BEFORE orders.
                    # ORDERS_ENABLED is still OFF, so this is strictly
                    # read-only discovery/synchronization.
                    # -------------------------------------------------
                    try:
                        recovered = recover_live_positions(
                            allow_orders_off=True
                        )

                        if recovered:
                            print(
                                "[Keyboard] AUTOMATIC LIVE recovery completed: "
                                "exchange positions discovered/synced; "
                                "no orders or protection actions were attempted."
                            )
                        else:
                            print(
                                "[Keyboard] AUTOMATIC LIVE recovery completed "
                                "with RECOVERY_LOCK still active."
                            )

                    except Exception as e:
                        print(f"[Keyboard] Automatic LIVE recovery failed: {e}")

                    # Start continuous read-only exchange position sync.
                    start_live_position_sync_thread()

                else:
                    print("[Keyboard] LIVE_MODE activation cancelled.")

                continue

            # -------------------------------------------------
            # O = ORDERS TOGGLE
            # -------------------------------------------------
            if key == "o":

                if ORDERS_ENABLED:
                    ORDERS_ENABLED = False
                    print("\n[Keyboard] ORDERS_ENABLED = OFF")

                else:
                    ORDERS_ENABLED = True

                    if LIVE_MODE:
                        print("\n[Keyboard] ORDERS_ENABLED = ON")
                        print("[Keyboard] LIVE_MODE is ON - REAL orders are now permitted.")

                    elif PAPER_MODE:
                        print("\n[Keyboard] ORDERS_ENABLED = ON")
                        print("[Keyboard] PAPER trading orders enabled.")

                    else:
                        print("\n[Keyboard] ORDERS_ENABLED = ON")
                        print("[Keyboard] No PAPER/LIVE mode selected; no trade execution mode is active.")

                continue

            # -------------------------------------------------
            # E = EMERGENCY EXIT ALL OPEN POSITIONS
            # -------------------------------------------------
            if key == "e":

                print("\n" + "!" * 68)
                print("!!! EMERGENCY EXIT ALL OPEN POSITIONS !!!")

                with state_lock:
                    open_positions = []

                    for symbol, state in symbol_states.items():
                        if state.get("in_position") and not state.get("position_closing"):
                            exit_price = (
                                state.get("live_price")
                                or state.get("mark_price")
                            )

                            open_positions.append(
                                (symbol, exit_price)
                            )

                if not open_positions:
                    print("[Keyboard] No open positions found.")
                    print("!" * 68)
                    continue

                print(
                    f"[Keyboard] {len(open_positions)} open position(s) "
                    "will be sent to close_position()."
                )

                for symbol, exit_price in open_positions:
                    print(
                        f"  - {symbol}: exit reference = "
                        f"{exit_price if exit_price is not None else 'N/A'}"
                    )

                print()
                print("Confirm EMERGENCY EXIT ALL?")
                print("Press Y = YES, execute all closes")
                print("Press N = NO, cancel")
                print("!" * 68)

                confirm = sys.stdin.read(1).lower()

                if confirm != "y":
                    print("[Keyboard] Emergency exit cancelled.")
                    continue

                print(
                    "\n[Keyboard] EMERGENCY EXIT CONFIRMED - "
                    "closing all open positions..."
                )

                for symbol, exit_price in open_positions:

                    if exit_price is None:
                        print(
                            f"[Keyboard] {symbol}: no live/mark price available; "
                            "close skipped."
                        )
                        continue

                    try:
                        close_position(
                            symbol,
                            exit_price,
                            "MANUAL EMERGENCY EXIT ALL"
                        )

                    except Exception as e:
                        print(
                            f"[Keyboard] Emergency close failed for "
                            f"{symbol}: {e}"
                        )

                print(
                    "[Keyboard] EMERGENCY EXIT ALL sequence completed."
                )

                continue

            # -------------------------------------------------
            # R = LIVE EXCHANGE RECOVERY
            # -------------------------------------------------
            if key == "c":
                LOW_CAPITAL_MODE = not LOW_CAPITAL_MODE
            
                if LOW_CAPITAL_MODE:
                    print(
                        "\n[Keyboard] LOW CAPITAL MODE = ON | "
                        "min qty | leverage fallback 10x -> 20x -> 40x"
                    )
                else:
                    print(
                        "\n[Keyboard] LOW CAPITAL MODE = OFF | "
                        "normal 10x / 50% cap"
                    )
                continue

            if key == "r":

                if not LIVE_MODE:
                    print(
                        "\n[Keyboard] LIVE recovery blocked: "
                        "LIVE_MODE must be ON."
                    )
                    continue

                try:
                    recovered = recover_live_positions(
                        allow_orders_off=True
                    )

                    if recovered:
                        if ORDERS_ENABLED:
                            print(
                                "[Keyboard] LIVE recovery completed: "
                                "all exchange positions protected."
                            )
                        else:
                            print(
                                "[Keyboard] READ-ONLY LIVE recovery completed: "
                                "exchange positions discovered/synced; "
                                "no orders or protection actions were attempted."
                            )
                    else:
                        print(
                            "[Keyboard] LIVE recovery completed with "
                            "RECOVERY_LOCK still active."
                        )

                except Exception as e:
                    print(f"[Keyboard] LIVE recovery failed: {e}")

                continue

    except Exception as e:
        print(f"[Keyboard Control Error] {e}")

    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

def display_loop():

    keyboard_thread = threading.Thread(
        target=_keyboard_control_loop,
        daemon=True,
        name="dashboard-keyboard",
    )
    keyboard_thread.start()

    while True:

        try:
            display_market()
        except Exception as e:
            print(f"[Display Loop Error] {e}")

        time.sleep(PRINT_INTERVAL)


# ============================================================
# MAIN
# ============================================================

def main():

    global account_balance, account_available_balance

    load_persistent_state()
    print(f"Connecting to {WS_URL} ...")

    if not API_KEY or not SECRET_KEY:
        print(
            "[WARNING] API_KEY / SECRET_KEY not set - REST calls "
            "(watchlist + ticker) will fail. Set COINSWITCH_API_KEY "
            "and COINSWITCH_SECRET_KEY env vars, or edit the config "
            "at the top of this file."
        )

    if LIVE_MODE and ORDERS_ENABLED:

        print()
        print("!" * 68)
        print("!!! LIVE TRADING IS ENABLED !!!")
        print("!" * 68)
        print(f"Real MARKET orders WILL be placed at {TRADE_LEVERAGE}x leverage")
        print("with REAL money on your CoinSwitch PRO futures account.")
        print("Type YES (all caps) to continue, anything else to abort.")
        print("!" * 68)

        confirmation = input("> ").strip()

        if confirmation != "YES":
            print("Aborted. LIVE_MODE remains disabled.")
            return

        print("Confirmed. Starting live trading...")

    elif PAPER_MODE and ORDERS_ENABLED:

        print()
        print("=" * 68)
        print("PAPER TRADING ENABLED")
        print(f"Paper Capital : {paper_capital:.6f} USDT")
        print("No real CoinSwitch orders will be placed.")
        print("=" * 68)

    else:

        print()
        print("=" * 68)
        print("ORDERS DISABLED")
        print(f"PAPER_MODE = {PAPER_MODE}")
        print(f"LIVE_MODE  = {LIVE_MODE}")
        print("No orders or simulated trades will be executed.")
        print("=" * 68)

    try:

        sio.connect(
            WS_URL,
            namespaces=[NAMESPACE],
            socketio_path=SOCKETIO_PATH,
            transports=["websocket"],
        )

    except Exception as e:

        print(f"[Connection Error] {e}")
        return

    if LIVE_MODE:
        refresh_account_balance()
    else:
        with state_lock:
            account_balance = None
            account_available_balance = None

    load_hft_watchlist(force=True)

    # LIVE exchange reconciliation MUST happen before scanner threads
    # are allowed to create new positions.
    if LIVE_MODE and ORDERS_ENABLED:
        recover_live_positions()

        protection_thread = threading.Thread(
            target=live_protection_watchdog,
            daemon=True,
            name="live-protection-watchdog",
        )
        protection_thread.start()

        start_live_position_sync_thread()

    balance_thread = threading.Thread(target=balance_poll_loop, daemon=True)
    balance_thread.start()

    scanner_thread = threading.Thread(target=hft_scanner_loop, daemon=True)
    scanner_thread.start()

    display_thread = threading.Thread(
        target=display_loop,
        daemon=True,
        name="display_loop",
    )
    display_thread.start()

    ticker_thread = threading.Thread(target=ticker_poll_loop, daemon=True)
    ticker_thread.start()

    watchlist_thread = threading.Thread(target=watchlist_refresh_loop, daemon=True)
    watchlist_thread.start()

    try:

        sio.wait()

    except KeyboardInterrupt:

        print()
        print("Stopping...")

    finally:

        try:
            sio.disconnect()
        except Exception:
            pass


# ============================================================
# LIVE EXCHANGE READ-ONLY RECONCILIATION
# CoinSwitch Futures
# NO ORDER PLACEMENT
# NO ORDER CANCELLATION
# ============================================================

def get_live_order_status(order_id):
    """Read-only: fetch one futures order by CoinSwitch order_id."""
    if not order_id:
        return None

    try:
        path = "/trade/api/v2/futures/order"
        headers, signed_path = sign_request(
            "GET",
            path,
            params={"order_id": str(order_id)},
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE ORDER STATUS] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE ORDER STATUS ERROR] {exc}")
        return None


def _get_live_positions_with_status(symbol):
    """Read-only futures position request with explicit API success state."""
    if not symbol:
        return False, None

    try:
        symbol = str(symbol).upper()

        path = "/trade/api/v2/futures/positions"
        headers, signed_path = sign_request(
            "GET",
            path,
            params={
                "exchange": "EXCHANGE_2",
                "symbol": symbol,
            },
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE POSITIONS] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return True, payload

    except Exception as exc:
        print(f"[LIVE POSITIONS ERROR] {symbol}: {exc}")
        return False, None


def get_live_positions(symbol):
    """Read-only: fetch currently open futures position for a symbol."""
    if not symbol:
        return None

    try:
        symbol = str(symbol).upper()

        path = "/trade/api/v2/futures/positions"
        headers, signed_path = sign_request(
            "GET",
            path,
            params={
                "exchange": "EXCHANGE_2",
                "symbol": symbol,
            },
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE POSITIONS] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE POSITIONS ERROR] {symbol}: {exc}")
        return None


def get_live_open_orders(symbol=None, limit=20):
    """
    Read-only: fetch currently working (non-terminal) futures orders.

    Official endpoint: POST /trade/api/v2/futures/orders/open
    (NOT "GET /trade/api/v2/futures/orders" - that path doesn't exist
    on the Futures v2 API; Open Orders is a POST with a JSON body,
    mirroring Closed Orders below.)
    """
    try:
        path = "/trade/api/v2/futures/orders/open"

        body = {
            "exchange": "EXCHANGE_2",
            "limit": int(limit),
        }

        if symbol:
            body["symbol"] = str(symbol).upper()

        headers, signed_path = sign_request(
            "POST",
            path,
        )

        response = requests.post(
            BASE_URL + signed_path,
            headers=headers,
            json=body,
            timeout=15,
        )

        print(f"[LIVE OPEN ORDERS] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE OPEN ORDERS ERROR] {exc}")
        return None


def get_live_closed_orders(symbol=None, limit=20):
    """Read-only: fetch recently closed futures orders."""
    try:
        path = "/trade/api/v2/futures/orders/closed"

        body = {
            "exchange": "EXCHANGE_2",
            "limit": int(limit),
        }

        if symbol:
            body["symbol"] = str(symbol).upper()

        headers, signed_path = sign_request(
            "POST",
            path,
        )

        response = requests.post(
            BASE_URL + signed_path,
            headers=headers,
            json=body,
            timeout=15,
        )

        print(f"[LIVE CLOSED ORDERS] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE CLOSED ORDERS ERROR] {exc}")
        return None


def get_live_symbol_leverage(symbol):
    """Read-only: fetch exchange-confirmed leverage for a symbol."""
    if not symbol:
        return None

    try:
        symbol = str(symbol).upper()

        path = "/trade/api/v2/futures/leverage"

        headers, signed_path = sign_request(
            "GET",
            path,
            params={
                "exchange": "EXCHANGE_2",
                "symbol": symbol,
            },
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE LEVERAGE] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE LEVERAGE ERROR] {symbol}: {exc}")
        return None


# ============================================================
# ADDITIONAL OFFICIAL ENDPOINTS (previously missing from this file)
#
# These are NOT wired into the automatic strategy loop - the bot
# never calls them on its own. They're here as ready-to-use manual
# utilities matching the official Futures v2 reference. Cancel Order
# / Cancel All / Add Margin place or modify real state when
# ORDERS_ENABLED + LIVE_MODE are on - use them deliberately.
# ============================================================

def cancel_order(order_id):
    """
    DELETE /trade/api/v2/futures/order

    Cancel a single open futures order by order_id. Returns the
    parsed response dict on success, or None on any failure.
    """
    if not order_id:
        return None

    try:
        headers, signed_path = sign_request(
            "DELETE",
            "/trade/api/v2/futures/order",
            params={"order_id": str(order_id)},
        )

        response = requests.delete(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[CANCEL ORDER] {order_id} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[CANCEL ORDER ERROR] {order_id}: {exc}")
        return None


def cancel_all_open_orders(symbol=None):
    """
    POST /trade/api/v2/futures/cancel_all

    Cancel every open order (optionally scoped to one symbol).
    Returns the parsed response dict (with "orders_ids") on
    success, or None on any failure.
    """
    try:
        body = {"exchange": "EXCHANGE_2"}
        if symbol:
            body["symbol"] = str(symbol).upper()

        headers, signed_path = sign_request(
            "POST",
            "/trade/api/v2/futures/cancel_all",
        )

        response = requests.post(
            BASE_URL + signed_path,
            headers=headers,
            json=body,
            timeout=15,
        )

        print(f"[CANCEL ALL] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[CANCEL ALL ERROR] {exc}")
        return None


def add_margin(symbol, margin):
    """
    POST /trade/api/v2/futures/add_margin

    Top up margin (in USDT) on an open position, pushing the
    liquidation price further from the mark price. `margin` must
    be <= available wallet balance. Returns the parsed response
    dict on success, or None on any failure.
    """
    if not symbol:
        return None

    try:
        body = {
            "exchange": "EXCHANGE_2",
            "symbol": str(symbol).upper(),
            "margin": margin,
        }

        headers, signed_path = sign_request(
            "POST",
            "/trade/api/v2/futures/add_margin",
        )

        response = requests.post(
            BASE_URL + signed_path,
            headers=headers,
            json=body,
            timeout=15,
        )

        print(f"[ADD MARGIN] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[ADD MARGIN ERROR] {symbol}: {exc}")
        return None


def get_live_transactions(symbol=None, limit=20):
    """
    GET /trade/api/v2/futures/transactions

    Read-only: fees, funding payments, realized PnL, and add-margin
    history. Returns the parsed response dict, or None on failure.
    """
    try:
        params = {"exchange": "EXCHANGE_2", "limit": int(limit)}
        if symbol:
            params["symbol"] = str(symbol).upper()

        headers, signed_path = sign_request(
            "GET",
            "/trade/api/v2/futures/transactions",
            params=params,
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE TRANSACTIONS] HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE TRANSACTIONS ERROR] {exc}")
        return None


def get_live_order_book(symbol):
    """
    GET /trade/api/v2/futures/order_book

    Read-only: current bids/asks snapshot for a symbol. Returns
    the parsed response dict, or None on failure.
    """
    if not symbol:
        return None

    try:
        symbol = str(symbol).upper()

        headers, signed_path = sign_request(
            "GET",
            "/trade/api/v2/futures/order_book",
            params={"exchange": "EXCHANGE_2", "symbol": symbol},
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE ORDER BOOK] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE ORDER BOOK ERROR] {symbol}: {exc}")
        return None


def get_live_ticker(symbol):
    """
    GET /trade/api/v2/futures/ticker

    Read-only: 24h stats + funding info for ONE symbol (unlike
    fetch_all_pairs_ticker(), which covers every symbol in one
    call). Returns the parsed response dict, or None on failure.
    """
    if not symbol:
        return None

    try:
        symbol = str(symbol).upper()

        headers, signed_path = sign_request(
            "GET",
            "/trade/api/v2/futures/ticker",
            params={"exchange": "EXCHANGE_2", "symbol": symbol},
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE TICKER] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE TICKER ERROR] {symbol}: {exc}")
        return None


def get_live_trades(symbol):
    """
    GET /trade/api/v2/futures/trades

    Read-only: recent public trade prints for a symbol. Returns
    the parsed response dict, or None on failure.
    """
    if not symbol:
        return None

    try:
        symbol = str(symbol).upper()

        headers, signed_path = sign_request(
            "GET",
            "/trade/api/v2/futures/trades",
            params={"exchange": "EXCHANGE_2", "symbol": symbol},
        )

        response = requests.get(
            BASE_URL + signed_path,
            headers=headers,
            timeout=15,
        )

        print(f"[LIVE TRADES] {symbol} HTTP: {response.status_code}")
        response.raise_for_status()

        payload = response.json()
        print(json.dumps(payload, indent=2))
        return payload

    except Exception as exc:
        print(f"[LIVE TRADES ERROR] {symbol}: {exc}")
        return None


def live_exchange_audit(symbol="BTCUSDT"):
    """
    Complete read-only LIVE exchange audit.

    IMPORTANT:
    This function NEVER places or cancels an order.
    """

    print()
    print("=" * 78)
    print("COINSWITCH LIVE EXCHANGE AUDIT")
    print("=" * 78)
    print(f"SYMBOL: {symbol}")
    print()

    print("-" * 78)
    print("1. FUTURES WALLET BALANCE")
    print("-" * 78)
    try:
        balance = fetch_futures_balance()
        print(json.dumps(balance, indent=2))
    except Exception as exc:
        print(f"[BALANCE ERROR] {exc}")

    print()
    print("-" * 78)
    print("2. OPEN POSITION")
    print("-" * 78)
    get_live_positions(symbol)

    print()
    print("-" * 78)
    print("3. OPEN ORDERS")
    print("-" * 78)
    get_live_open_orders(symbol)

    print()
    print("-" * 78)
    print("4. CLOSED ORDERS")
    print("-" * 78)
    get_live_closed_orders(symbol, limit=20)

    print()
    print("-" * 78)
    print("5. EXCHANGE LEVERAGE")
    print("-" * 78)
    get_live_symbol_leverage(symbol)

    print()
    print("=" * 78)
    print("LIVE EXCHANGE AUDIT COMPLETE")
    print("READ-ONLY: NO ORDER WAS PLACED OR CANCELLED")
    print("=" * 78)
    print()


if __name__ == "__main__" and os.environ.get("COINSWITCH_AUDIT_ONLY") == "1":
    live_exchange_audit(os.environ.get("COINSWITCH_AUDIT_SYMBOL", "BTCUSDT"))

# ============================================================
# PROGRAM ENTRY
# All function definitions must exist before main() executes.
# ============================================================

if __name__ == "__main__":
    main()
