import numpy as np
import pandas as pd
import threading
import logging
import time
import json
import websocket
from collections import deque
from dataclasses import dataclass
from typing import Optional, Union, Dict, List
import requests

try:
    from btc_sniper import config
    from btc_sniper.ob_features import OBFeaturesEngine
    from btc_sniper.ml_engine import MLEngine
except ImportError:
    import config
    from ob_features import OBFeaturesEngine
    from ml_engine import MLEngine

logger = logging.getLogger("Strategy")

@dataclass
class SignalResult:
    direction: str
    confidence: float
    total_score: float
    breakdown: dict
    reasoning: str
    weights: dict


class BinanceFeed:
    def __init__(self):
        self.ticks = deque(maxlen=600)
        self.candles_1m = deque(maxlen=50)
        self.candles_training = deque(maxlen=1000)
        self.window_open_price = 0.0
        self.lock = threading.Lock()
        self.running = False
        
        # Engines
        self.ob_engine_yes = OBFeaturesEngine()
        self.ob_engine_no  = OBFeaturesEngine()
        self.ml_engine     = MLEngine()

    def seed_data(self):
        """Fetch historical data via REST to avoid zero-start."""
        try:
            # 1. Fetch 1000 candles for ML and 50 for live
            r = requests.get("https://api.binance.com/api/v3/klines", 
                             params={"symbol": "BTCUSDT", "interval": "1m", "limit": 1000}, timeout=10)
            if r.status_code == 200:
                with self.lock:
                    self.candles_1m.clear()
                    self.candles_training.clear()
                    all_candles = []
                    for k in r.json():
                        c = {
                            'open': float(k[1]), 'high': float(k[2]),
                            'low': float(k[3]), 'close': float(k[4]),
                            'volume': float(k[5])
                        }
                        all_candles.append(c)
                        self.candles_training.append(c)
                    
                    for c in all_candles[-50:]:
                        self.candles_1m.append(c)
                        
                logger.info("📡 Seeded 1000 candles from Binance REST for ML (50 applied to live)")
                # No longer training old ML model
            
            # 2. Fetch last price
            r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
            if r.status_code == 200:
                price = float(r.json()["price"])
                self.on_tick(price, 0)
                logger.info(f"📡 Seeded current price: ${price}")
        except Exception as e:
            logger.error(f"Failed to seed Binance data: {e}")

    def on_tick(self, price, qty):
        with self.lock:
            self.ticks.append({'price': float(price), 'qty': float(qty), 'ts': time.time_ns()})

    def on_candle(self, candle):
        with self.lock:
            self.candles_1m.append(candle)
            self.candles_training.append(candle)

    def set_window_open(self, price):
        with self.lock: self.window_open_price = price

    def start(self):
        self.seed_data()
        self.running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()

    def _run_ws(self):
        url = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@kline_1m"
        while self.running:
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                ws.run_forever()
            except Exception:
                time.sleep(1)

    def _on_message(self, ws, message):
        try:
            raw = json.loads(message)
            data = raw.get('data', {})
        except (json.JSONDecodeError, AttributeError):
            return
        if 'e' in data:
            if data['e'] == 'trade':
                self.on_tick(data['p'], data['q'])
            elif data['e'] == 'kline':
                k = data['k']
                if k['x']: # Candle closed
                    self.on_candle({
                        'open': float(k['o']), 'high': float(k['h']),
                        'low': float(k['l']), 'close': float(k['c']),
                        'volume': float(k['v'])
                    })

    def _on_error(self, ws, error): logger.error(f"Binance WS Error: {error}")
    def _on_close(self, ws, *args): logger.info("Binance WS Closed")

    def analyze(self, ticks, candles, ob_yes, window_open_price, weights: Optional[dict] = None) -> SignalResult:
        w = weights or config.INDICATOR_WEIGHTS
        scores = {k: 0.0 for k in w.keys()}
        
        if not ticks or window_open_price == 0:
            return SignalResult("DOWN", 0.0, 0.0, scores, "Waiting for data...", w)
            
        current_price = ticks[-1]['price']
        
        # 1. Window Delta
        delta = ((current_price - window_open_price) / window_open_price * 100)
        scores["window_delta"] = w["window_delta"] * min(abs(delta) / 0.15, 1.0) * (1 if delta > 0 else -1)

        # 2. OB Features & Imbalance Score
        if ob_yes and ob_yes.update_count > 0:
            # AVANT d'utiliser book_imbalance :
            if ob_yes.best_bid <= 0 or ob_yes.best_ask <= 0 or ob_yes.update_count < 3:
                # OB pas encore prêt → neutraliser
                ob_imb_raw   = 0.5   # neutre
                ob_imb_score = 0.0
                config.debug_logger.debug(
                    f"OB_NOT_READY | neutralisé "
                    f"upd={ob_yes.update_count} "
                    f"bid={ob_yes.best_bid}"
                )
                ob_feats = self.ob_engine_yes.get_feature_vector(ob_yes)
            else:
                ob_feats = self.ob_engine_yes.get_feature_vector(ob_yes)
                ob_imb_raw = ob_feats.get("imb_l5", 0.5)
                ob_imb_score = (
                    ob_feats["imb_score"]       * w["ob_imbalance"]
                  + ob_feats["flow_score"]      * w.get("flow", 2.5)
                  + ob_feats["mid_vel_score"]   * w.get("mid_vel", 1.5)
                )
            
            scores["ob_imbalance"] = ob_imb_score
            
            spread_mult = ob_feats.get("spread_conf_mult", 1.0)
            liq_mult    = ob_feats.get("size_mult", 1.0)
            
            # Temporary storage for multipliers for debug logging later if needed
            self.last_ob_feats = ob_feats
        else:
            ob_feats = {}
            spread_mult = 1.0
            liq_mult = 1.0
            scores["ob_imbalance"] = 0.0

        # 3. Binance Features for ML
        if len(candles) >= 14:
            closes = [c["close"] for c in list(candles)]
            rsi_val = self._compute_rsi(closes, period=14)
            ema_delta = 0.0 # simplified for features dict
            if len(closes) >= 21:
                emas = pd.Series(closes).ewm(span=9).mean()
                ema9 = emas.iloc[-1]
                ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]
                ema_delta = (ema9 - ema21) / ema21 * 100
                
            momentum = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
            vol_surge = 0.0
            if len(candles) >= 5:
                cur_v = candles[-1]["volume"]
                av_v = np.mean([c["volume"] for c in list(candles)[-10:]])
                vol_surge = cur_v / av_v if av_v > 0 else 1.0

            tick_vel = 0.0
            if len(ticks) >= 30:
                tick_vel = (ticks[-1]['price'] - ticks[-30]['price']) / ((ticks[-1]['ts'] - ticks[-30]['ts'])/1e9)

            c_last = candles[-1]
            c_range = max(c_last['high'] - c_last['low'], 0.001)
            candle_body = abs(c_last['close'] - c_last['open']) / c_range
            upper_wick = (c_last['high'] - max(c_last['open'], c_last['close'])) / c_range
            lower_wick = (min(c_last['open'], c_last['close']) - c_last['low']) / c_range
            vol_1m = np.std(closes[-15:]) / np.mean(closes[-15:]) if len(closes) >= 15 else 0.0

            binance_feats = {
                "rsi":               rsi_val,
                "ema_delta_pct":     ema_delta,
                "momentum_pct":      momentum,
                "volume_surge":      vol_surge,
                "window_delta_pct":  delta,
                "tick_velocity":     tick_vel,
                "candle_body_pct":   candle_body,
                "upper_wick_pct":    upper_wick,
                "lower_wick_pct":    lower_wick,
                "volatility_1m":     vol_1m,
            }
            self.last_binance_feats = binance_feats
        else:
            binance_feats = {}
            rsi_val = 50.0

        # 4. ML Prediction
        if binance_feats and ob_feats:
            ml_pred = self.ml_engine.predict(binance_feats, ob_feats)
            if ml_pred["trained"]:
                ml_score = (ml_pred["p_up"] - 0.5) * 2
                scores["ml_score"] = ml_score * w["ml_score"]
                config.debug_logger.debug(f"ML_PREDICT  | p_up={ml_pred['p_up']:.3f} p_down={ml_pred['p_down']:.3f} dir={ml_pred['direction']} edge={ml_pred['edge']:.3f} breakdown={ml_pred['breakdown']}")
        
        # 5. Traditional Indicators (Optional fallback/complement)
        # RSI
        if rsi_val > 75: scores["rsi"] = -w["rsi"]
        elif rsi_val < 25: scores["rsi"] = w["rsi"]
        
        # EMA
        if len(candles) >= 21:
            if ema9 > ema21: scores["ema_cross"] = w["ema_cross"]
            else: scores["ema_cross"] = -w["ema_cross"]

        # 6. Final Confidence
        total = sum(scores.values())
        max_score = sum(w.values())
        raw_confidence = min(abs(total) / (max_score * 0.4), 1.0)
        final_conf = min(raw_confidence * spread_mult * liq_mult, 1.0)
        direction = "UP" if total > 0 else "DOWN"
        
        if ob_feats:
            config.debug_logger.debug(f"OB_FEATURES | imb_l1={ob_feats['imb_l1']:.2f} imb_l5={ob_feats['imb_l5']:.2f} consistency={ob_feats['imb_consistency']:.2f} flow_delta={ob_feats['flow_delta']:+.2f} mid_vel={ob_feats['mid_velocity']:+.4f} spread={ob_feats['spread']:.3f} liq=${ob_feats['total_liquidity']:.0f}")

        return SignalResult(
            direction=direction,
            confidence=final_conf,
            total_score=total,
            breakdown=scores,
            reasoning=f"Total {total:+.2f} | Conf {final_conf:.3f} (S:{spread_mult:.1f} L:{liq_mult:.1f})",
            weights=w
        )

    def _compute_rsi(self, closes: list, period: int = 14) -> float:
        if len(closes) < period + 1: return 50.0
        deltas = np.diff(closes)
        seed = deltas[:period+1]
        up = seed[seed >= 0].sum() / period
        down = -seed[seed < 0].sum() / period
        if down == 0: return 100.0
        rs = up / down
        rsi = np.zeros_like(closes)
        rsi[:period] = 100. - 100. / (1. + rs)
        for i in range(period, len(closes)):
            delta = deltas[i-1]
            if delta > 0:
                upval = delta; downval = 0.
            else:
                upval = 0.; downval = -delta
            up = (up * (period - 1) + upval) / period
            down = (down * (period - 1) + downval) / period
            rs = up / down
            rsi[i] = 100. - 100. / (1. + rs)
        return float(rsi[-1])
