import os
from dotenv import load_dotenv
load_dotenv()

# ── MODE ─────────────────────────────────────────────
# ── MODE ─────────────────────────────────────────────
TRADING_LEVEL = 1        # 1 = PAPER, 2 = LIVE

# ── CAPITAL ──────────────────────────────────────────
INITIAL_BANKROLL  = float(os.getenv("BANKROLL", "100.0"))
MAX_ORDER_SIZE    = 0.0   # PAPER = 0 argent réel

# ── APIs PUBLIQUES ────────────────────────────────────
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
CLOB_WS_URL  = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS   = "wss://stream.binance.com:9443/stream"
BINANCE_REST = "https://api.binance.com/api/v3"
CHAIN_ID     = 137

# ── STRATÉGIE ─────────────────────────────────────────
PREFETCH_LEAD_S  = 15
FORCED_ENTRY_S   = 270
NO_NEW_ORDERS_S  = 10
REPRICE_EVERY_S  = 2.0
SPREAD_TARGET    = 0.018

CONFIDENCE_TIERS = {
    "primary":   0.30,   # Normal entry
    "secondary": 0.18,   # T+240s entry
    "forced":    0.00,   # T+270s hard deadline
}

INDICATOR_WEIGHTS = {
    # Binance (inchangés)
    "window_delta":   7.0,
    "rsi":            2.0,
    "ema_cross":      3.0,
    "momentum":       2.5,
    "volume_surge":   1.5,

    # OB Polymarket (NOUVEAU — remplace l'ancien 3.0)
    "ob_imbalance":   2.0,   # imb_l5 de base
    "flow":           2.5,   # trade flow delta
    "mid_vel":        1.5,   # vélocité du mid OB

    # ML ensemble (augmenté car plus fiable)
    "ml_score":       5.0,
}

# ── CREDENTIALS (jamais hardcodés) ───────────────────
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")

# ── LOGGER DEBUG ─────────────────────────────────────
import logging
import json
import os
from datetime import datetime

os.makedirs("logs", exist_ok=True)

SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

# ── LOG 1 : DEBUG COMPLET (tout) ─────────────────────
# Fichier : logs/debug_SESSION.log
debug_logger = logging.getLogger("sniper.debug")
debug_logger.setLevel(logging.DEBUG)
_fh_debug = logging.FileHandler(
    f"logs/debug_{SESSION_ID}.log",
    encoding="utf-8"
)
_fh_debug.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d | %(levelname)-8s "
    "| %(message)s",
    datefmt="%H:%M:%S"
))
debug_logger.addHandler(_fh_debug)

# ── LOG 2 : TRADES UNIQUEMENT (JSON lines) ───────────
# Fichier : logs/trades_SESSION.jsonl
trade_logger = logging.getLogger("sniper.trades")
trade_logger.setLevel(logging.INFO)
_fh_trades = logging.FileHandler(
    f"logs/trades_{SESSION_ID}.jsonl",
    encoding="utf-8"
)
_fh_trades.setFormatter(logging.Formatter("%(message)s"))
trade_logger.addHandler(_fh_trades)

# ── LOG 3 : RAPPORT HUMAIN (Markdown) ────────────────
# Fichier : logs/report_SESSION.md
report_logger = logging.getLogger("sniper.report")
report_logger.setLevel(logging.INFO)
REPORT_FILENAME = "logs/palestinereport.md"
_fh_report = logging.FileHandler(
    REPORT_FILENAME,
    mode="a",
    encoding="utf-8"
)
_fh_report.setFormatter(logging.Formatter("%(message)s"))
report_logger.addHandler(_fh_report)

def log_trade_event(event_type: str, data: dict):
    """Log un événement trade en JSON sur une ligne."""
    payload = {
        "ts":    datetime.now().isoformat(),
        "event": event_type,
        **data,
    }
    trade_logger.info(json.dumps(payload))

def log_report(line: str):
    """Log une ligne dans le rapport Markdown."""
    report_logger.info(line)

# En tête de session au démarrage
log_report(f"\n\n---\n# ▶ SESSION {SESSION_ID} — "
           f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
