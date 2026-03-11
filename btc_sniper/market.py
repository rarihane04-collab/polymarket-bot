import time
import json
import requests
import threading
import logging
import websocket
from datetime import datetime, timezone
from collections import deque
from typing import Optional, Union, Dict, List
from btc_sniper import config, display

logger = logging.getLogger("Market")

def get_current_window_ts() -> int:
    """Returns current 300s floor timestamp."""
    return (int(time.time()) // 300) * 300

def get_next_window_ts() -> int:
    return get_current_window_ts() + 300

def seconds_until_next_window() -> float:
    """Exact seconds remaining in the current 5-min window."""
    return get_next_window_ts() - time.time()

def fetch_market_by_slug(slug: str) -> Optional[dict]:
    """Target < 80ms resolution with keepalive."""
    url = f"{config.GAMMA_API}/markets?slug={slug}"
    session = requests.Session() # In production, global session preferred
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=0.1)
            if resp.status_code == 200:
                data = resp.json()
                return data[0] if data else None
        except Exception:
            time.sleep(0.2)
    return None

def resolve_market(window_ts: Optional[int] = None) -> Optional[dict]:
    ts = window_ts or get_current_window_ts()
    slug = f"btc-updown-5m-{ts}"
    for attempt in range(5):
        market_data = fetch_market_by_slug(slug)
        if market_data:
            return market_data
        time.sleep(0.5)
    return None

class LiveOrderBook:
    def __init__(self, token_id: str, label: str):
        self.token_id    = token_id
        self.label       = label
        self.bids        = {}   # price → size
        self.asks        = {}
        self.best_bid    = 0.0
        self.best_ask    = 0.0
        self.mid         = 0.0
        self.book_imbalance = 0.5
        self.update_count= 0
        self._lock       = threading.Lock()
        self._ws         = None
        self._thread     = None
        self._running    = False
        self.connected   = False
        self.snapshots   = deque(maxlen=200)
        self.trades      = deque(maxlen=200)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_ws,
            name=f"OB_{self.label}",
            daemon=True,
        )
        self._thread.start()
        config.debug_logger.info(
            f"OB_{self.label} | WebSocket started "
            f"token={self.token_id[:16]}..."
        )

    def _run_ws(self):
        url = config.CLOB_WS_URL
        while self._running:
            try:
                config.debug_logger.info(f"OB_{self.label} attempting connection to {url}")
                ws = websocket.WebSocketApp(
                    url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws = ws
                # Increased ping_interval to avoids race conditions with server
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                config.debug_logger.error(f"OB_{self.label} WS run_forever crash: {e}")
            
            self.connected = False
            if self._running:
                time.sleep(3)   # Reconnect delay

    def _on_open(self, ws):
        self.connected = True
        payload = {
            "type": "subscribe",
            "assets_ids": [self.token_id],
            "channel": "book"
        }
        ws.send(json.dumps(payload))
        config.debug_logger.info(f"OB_{self.label} | WebSocket OPEN & Subscribed to 'book' via assets_ids")
        display.log(f"📡 OB {self.label} connecté, canal 'book' souscrit.")

    def _on_message(self, ws, message):
        try:
            with open("/tmp/ob_debug.json", "w") as f:
                f.write(message)
            data = json.loads(message)
            if isinstance(data, list):
                for item in data: self._process_ob_event(item)
            else:
                self._process_ob_event(data)
        except Exception as e:
            config.debug_logger.warning(...)

    def _process_ob_event(self, data: dict):
        if not isinstance(data, dict): return
        
        event_type = data.get("type") or data.get("event_type") or ""
        
        # DEBUG log
        if self.update_count < 3:
            config.debug_logger.debug(f"OB_{self.label} (ID:{id(self)}) msg type='{event_type}' keys={list(data.keys())}")

        if event_type == "error":
            msg = data.get("message", "Unknown error")
            config.debug_logger.error(f"OB_{self.label} WS ERR: {msg}")
            display.log(f"❌ OB {self.label} ERR: {msg}")
            return

        def _parse_level(item):
            try:
                if isinstance(item, dict):
                    return float(item.get("price", 0)), float(item.get("size", 0))
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    return float(item[0]), float(item[1])
            except: pass
            return None, None

        do_log_1st = False
        with self._lock:
            # Snapshot complet
            is_snapshot = event_type in ("book", "snapshot") or ("bids" in data and "asks" in data)
            
            if is_snapshot:
                raw_bids = data.get("bids", [])
                if raw_bids:
                     config.debug_logger.debug(f"OB_{self.label} RAW BID[0]: {raw_bids[0]}")
                self.bids = {}
                self.asks = {}
                for b in data.get("bids", []):
                    p, s = _parse_level(b)
                    if p is not None and p > 0: self.bids[p] = s
                for a in data.get("asks", []):
                    p, s = _parse_level(a)
                    if p is not None and p > 0: self.asks[p] = s
                self._recompute()
                config.debug_logger.info(f"OB_{self.label} SNAPSHOT | bids={len(self.bids)} ask={self.best_ask:.3f} bid={self.best_bid:.3f}")
            
            if self.update_count == 0:
                do_log_1st = True

            # Update delta
            elif event_type in ("price_change", "delta", "update"):
                # Handle standard Polymarket price_change format
                for change in data.get("changes", []):
                    try:
                        side  = change.get("side", "").lower()
                        p     = float(change.get("price", 0))
                        s     = float(change.get("size",  0))
                        if p <= 0: continue
                        target = self.bids if side == "buy" else self.asks
                        if s == 0: target.pop(p, None)
                        else: target[p] = s
                    except: pass
                self._recompute()

            self.update_count += 1
            self.take_snapshot()

        if do_log_1st:
            display.log(f"✅ OB {self.label} — 1er message reçu !")

    def _recompute(self):
        """Recalcule best_bid, best_ask, mid, imbalance."""
        self.best_bid = max(self.bids.keys()) if self.bids else 0.0
        self.best_ask = min(self.asks.keys()) if self.asks else 0.0
        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = (self.best_bid + self.best_ask) / 2
        elif self.best_bid > 0:
            self.mid = self.best_bid
        elif self.best_ask > 0:
            self.mid = self.best_ask
        else:
            self.mid = 0.0

        # Imbalance top 5
        top_bids = sorted(self.bids.items(), reverse=True)[:5]
        top_asks = sorted(self.asks.items())[:5]
        bv = sum(s for _, s in top_bids)
        av = sum(s for _, s in top_asks)
        self.book_imbalance = bv / max(bv + av, 0.001)

    def take_snapshot(self):
        """Captures current OB state for velocity tracking."""
        with self._lock:
            snap = {
                "ts": time.time(),
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "mid": self.mid,
            }
            self.snapshots.append(snap)

    def register_trade(self, side, size, price):
        """Logs a trade for flow analysis."""
        trade = {
            "ts": time.time(),
            "side": side,
            "size": size,
            "price": price
        }
        self.trades.append(trade)

    def _on_error(self, ws, error):
        config.debug_logger.error(f"OB_{self.label} WS ERROR: {error}")
        display.log(f"❌ OB {self.label} WS ERROR: {error}")

    def _on_close(self, ws, status_code, close_msg):
        self.connected = False
        config.debug_logger.warning(
            f"OB_{self.label} WS CLOSED: code={status_code}, msg={close_msg}"
        )
        display.log(f"🔌 OB {self.label} déconnecté ({status_code})")

    def stop(self):
        self._running = False
        if self._ws:
            try: self._ws.close()
            except: pass

    # Compatibilité optionnelle : get_top_asks / get_top_bids used in bot.py
    def get_top_asks(self, n=5) -> List[tuple]:
        if not self._lock.acquire(timeout=0.05): return []
        try:
            return sorted(self.asks.items(), key=lambda x: x[0])[:n]
        finally:
            self._lock.release()

    def get_top_bids(self, n=5) -> List[tuple]:
        if not self._lock.acquire(timeout=0.05): return []
        try:
            return sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:n]
        finally:
            self._lock.release()
