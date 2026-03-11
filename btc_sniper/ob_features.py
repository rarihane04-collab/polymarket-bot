from dataclasses import dataclass, field
from collections import deque
import numpy as np
import time

@dataclass
class OBSnapshot:
    """Snapshot horodaté de l'orderbook."""
    ts:             float
    best_bid:       float
    best_ask:       float
    mid:            float
    spread:         float
    spread_pct:     float
    bid_vol_l1:     float   # size au best bid
    ask_vol_l1:     float   # size au best ask
    bid_vol_l5:     float   # volume top 5 bids
    ask_vol_l5:     float   # volume top 5 asks
    bid_vol_l10:    float   # volume top 10 bids
    ask_vol_l10:    float   # volume top 10 asks
    imbalance_l1:   float   # ratio L1
    imbalance_l5:   float   # ratio L5
    imbalance_l10:  float   # ratio L10
    wmp:            float   # weighted mid price
    depth_bid:      int     # nb niveaux côté bid
    depth_ask:      int     # nb niveaux côté ask
    total_volume:   float   # volume total OB


class OBFeaturesEngine:
    """
    Calcule 12 features en temps réel depuis l'orderbook.
    Maintient un historique de snapshots pour les features
    de vélocité et d'accélération.
    """

    def __init__(self, history_size: int = 120):
        # 120 snapshots × ~0.5s = 60s d'historique
        self.snapshots: deque[OBSnapshot] = deque(maxlen=history_size)
        self.trade_flow: deque[dict] = deque(maxlen=200)

    # ──────────────────────────────────────────────
    # FEATURE 1 — SPREAD DYNAMIQUE
    # ──────────────────────────────────────────────
    def spread_signal(self, ob) -> dict:
        """
        Spread étroit → consensus fort → signaux fiables
        Spread large  → incertitude → skip ou réduire size
        """
        best_ask = getattr(ob, "best_ask", 0)
        best_bid = getattr(ob, "best_bid", 0)
        mid      = getattr(ob, "mid", 0.5)
        
        spread     = best_ask - best_bid
        spread_pct = spread / max(mid, 0.001) * 100

        if spread < 0.02:
            signal, confidence_mult = "TIGHT",  1.30
        elif spread < 0.05:
            signal, confidence_mult = "NORMAL", 1.00
        elif spread < 0.08:
            signal, confidence_mult = "WIDE",   0.70
        else:
            signal, confidence_mult = "CHAOS",  0.0

        return {
            "spread":           round(spread, 4),
            "spread_pct":       round(spread_pct, 2),
            "spread_signal":    signal,
            "confidence_mult":  float(confidence_mult),
        }

    # ──────────────────────────────────────────────
    # FEATURE 2 — DEPTH IMBALANCE MULTI-NIVEAU
    # ──────────────────────────────────────────────
    def depth_imbalance(self, ob, levels: int = 10) -> dict:
        """
        Calcule l'imbalance sur plusieurs profondeurs.
        """
        bids = getattr(ob, "bids", {})
        asks = getattr(ob, "asks", {})
        
        top_bids = sorted(bids.items(), reverse=True)[:levels]
        top_asks = sorted(asks.items())[:levels]

        def imb(b_list, a_list):
            bv = sum(s for _, s in b_list)
            av = sum(s for _, s in a_list)
            return bv / max(bv + av, 0.001)

        imb_l1  = imb(top_bids[:1],  top_asks[:1])
        imb_l5  = imb(top_bids[:5],  top_asks[:5])
        imb_l10 = imb(top_bids[:10], top_asks[:10])

        # Stabilité de l'imbalance en profondeur
        consistency = 1.0 - np.std([imb_l1, imb_l5, imb_l10])

        # Direction consensus
        dominant_imb = imb_l5  # L5 = meilleur compromis
        direction = "BID_HEAVY" if dominant_imb > 0.55 else \
                    "ASK_HEAVY" if dominant_imb < 0.45 else \
                    "BALANCED"

        return {
            "imb_l1":      round(imb_l1, 4),
            "imb_l5":      round(imb_l5, 4),
            "imb_l10":     round(imb_l10, 4),
            "consistency": round(float(consistency), 4),
            "direction":   direction,
            "score":       round((dominant_imb - 0.5) * 2 * consistency, 4),
        }

    # ──────────────────────────────────────────────
    # FEATURE 3 — WEIGHTED MID PRICE (WMP)
    # ──────────────────────────────────────────────
    def weighted_mid_price(self, ob, levels: int = 5) -> float:
        """Mid price pondéré par la liquidité à chaque niveau."""
        bids = getattr(ob, "bids", {})
        asks = getattr(ob, "asks", {})
        mid  = getattr(ob, "mid", 0.5)

        top_bids = sorted(bids.items(), reverse=True)[:levels]
        top_asks = sorted(asks.items())[:levels]

        all_levels = top_bids + top_asks
        if not all_levels:
            return mid

        total_vol  = sum(s for _, s in all_levels)
        if total_vol == 0:
            return mid

        wmp = sum(p * s for p, s in all_levels) / total_vol
        return round(float(wmp), 5)

    # ──────────────────────────────────────────────
    # FEATURE 4 — MID PRICE VELOCITY
    # ──────────────────────────────────────────────
    def mid_velocity(self, window_s: float = 30.0) -> dict:
        """Vitesse de déplacement du mid price sur X secondes."""
        now     = time.time()
        cutoff  = now - window_s
        recent  = [s for s in self.snapshots if s.ts >= cutoff]

        if len(recent) < 3:
            return {
                "velocity":     0.0,
                "direction":    "FLAT",
                "acceleration": 0.0,
                "score":        0.0,
            }

        mids = [s.mid for s in recent]
        tss  = [s.ts  for s in recent]

        dt       = tss[-1] - tss[0]
        velocity = (mids[-1] - mids[0]) / max(dt, 0.001)

        mid_idx  = len(mids) // 2
        dt1 = tss[mid_idx] - tss[0]
        dt2 = tss[-1] - tss[mid_idx]
        v1 = (mids[mid_idx] - mids[0]) / max(dt1, 0.001)
        v2 = (mids[-1] - mids[mid_idx]) / max(dt2, 0.001)
        accel = v2 - v1

        direction = "UP"   if velocity >  0.0005 else \
                    "DOWN" if velocity < -0.0005 else \
                    "FLAT"

        score = np.clip(velocity * 1000, -1.0, 1.0)

        return {
            "velocity":     round(float(velocity), 6),
            "acceleration": round(float(accel), 6),
            "direction":    direction,
            "score":        round(float(score), 4),
        }

    # ──────────────────────────────────────────────
    # FEATURE 5 — VOLUME ENTRANT (Trade Flow)
    # ──────────────────────────────────────────────
    def register_trade(self, price: float, size: float, ts: float = None):
        """Enregistre un trade exécuté sur le marché."""
        self.trade_flow.append({
            "price": price,
            "size":  size,
            "ts":    ts or time.time(),
        })

    def trade_flow_signal(self, ob, window_s: float = 60.0) -> dict:
        """Analyse le flux de trades des 60 dernières secondes."""
        now    = time.time()
        cutoff = now - window_s
        recent = [t for t in self.trade_flow if t["ts"] >= cutoff]

        best_ask = getattr(ob, "best_ask", 0)
        best_bid = getattr(ob, "best_bid", 0)

        if not recent:
            return {
                "buy_pressure":  0.0,
                "sell_pressure": 0.0,
                "delta":         0.0,
                "delta_pct":     0.0,
                "score":         0.0,
            }

        buy_vol  = 0.0
        sell_vol = 0.0

        for t in recent:
            if abs(t["price"] - best_ask) < 0.005:
                buy_vol  += t["size"]
            elif abs(t["price"] - best_bid) < 0.005:
                sell_vol += t["size"]
            else:
                buy_vol  += t["size"] * 0.5
                sell_vol += t["size"] * 0.5

        total = buy_vol + sell_vol
        delta = (buy_vol - sell_vol) / max(total, 0.001)

        return {
            "buy_pressure":  round(buy_vol, 2),
            "sell_pressure": round(sell_vol, 2),
            "delta":         round(float(delta), 4),
            "delta_pct":     round(float(delta * 100), 1),
            "score":         round(float(np.clip(delta, -1, 1)), 4),
        }

    # ──────────────────────────────────────────────
    # FEATURE 6 — LIQUIDITÉ TOTALE (Market depth $)
    # ──────────────────────────────────────────────
    def total_liquidity(self, ob, levels: int = 10) -> dict:
        """Volume total en dollars dans le carnet."""
        bids = getattr(ob, "bids", {})
        asks = getattr(ob, "asks", {})
        mid  = getattr(ob, "mid", 0.5)

        top_bids = sorted(bids.items(), reverse=True)[:levels]
        top_asks = sorted(asks.items())[:levels]

        bid_liquidity = sum(p * s for p, s in top_bids)
        ask_liquidity = sum(p * s for p, s in top_asks)
        total         = bid_liquidity + ask_liquidity

        hist_totals = [s.bid_vol_l10 + s.ask_vol_l10 for s in self.snapshots]
        avg_total   = np.mean(hist_totals) if hist_totals else total
        liq_ratio   = total / max(avg_total, 0.001)

        if total > 5000:
            liq_signal = "DEEP"
        elif total > 1000:
            liq_signal = "NORMAL"
        elif total > 200:
            liq_signal = "SHALLOW"
        else:
            liq_signal = "THIN"

        size_mult = min(liq_ratio, 1.5) if liq_signal != "THIN" else 0.3

        return {
            "bid_liquidity": round(float(bid_liquidity), 2),
            "ask_liquidity": round(float(ask_liquidity), 2),
            "total":         round(float(total), 2),
            "liq_ratio":     round(float(liq_ratio), 3),
            "liq_signal":    liq_signal,
            "size_mult":     round(float(size_mult), 2),
        }

    # ──────────────────────────────────────────────
    # SNAPSHOT + HISTORIQUE
    # ──────────────────────────────────────────────
    def take_snapshot(self, ob) -> OBSnapshot:
        """Prend un snapshot complet de l'OB et le stocke."""
        bids = getattr(ob, "bids", {})
        asks = getattr(ob, "asks", {})
        mid  = getattr(ob, "mid", 0.5)
        best_bid = getattr(ob, "best_bid", 0)
        best_ask = getattr(ob, "best_ask", 0)

        top_bids_1  = sorted(bids.items(), reverse=True)[:1]
        top_bids_5  = sorted(bids.items(), reverse=True)[:5]
        top_bids_10 = sorted(bids.items(), reverse=True)[:10]
        top_asks_1  = sorted(asks.items())[:1]
        top_asks_5  = sorted(asks.items())[:5]
        top_asks_10 = sorted(asks.items())[:10]

        def vol(lev_list):
            return sum(s for _, s in lev_list)

        def imb(bv, av):
            return bv / max(bv + av, 0.001)

        b1v = vol(top_bids_1); a1v = vol(top_asks_1)
        b5v = vol(top_bids_5); a5v = vol(top_asks_5)
        b10v = vol(top_bids_10); a10v = vol(top_asks_10)

        spread = best_ask - best_bid
        snap   = OBSnapshot(
            ts           = time.time(),
            best_bid     = best_bid,
            best_ask     = best_ask,
            mid          = mid,
            spread       = spread,
            spread_pct   = spread / max(mid, 0.001),
            bid_vol_l1   = float(b1v),
            ask_vol_l1   = float(a1v),
            bid_vol_l5   = float(b5v),
            ask_vol_l5   = float(a5v),
            bid_vol_l10  = float(b10v),
            ask_vol_l10  = float(a10v),
            imbalance_l1 = float(imb(b1v, a1v)),
            imbalance_l5 = float(imb(b5v, a5v)),
            imbalance_l10= float(imb(b10v, a10v)),
            wmp          = self.weighted_mid_price(ob),
            depth_bid    = len(bids),
            depth_ask    = len(asks),
            total_volume = float(b10v + a10v),
        )
        self.snapshots.append(snap)
        return snap

    # ──────────────────────────────────────────────
    # FEATURE VECTOR COMPLET (pour le ML)
    # ──────────────────────────────────────────────
    def get_feature_vector(self, ob) -> dict:
        """Retourne TOUTES les features OB en un seul appel."""
        snap     = self.take_snapshot(ob)
        spread   = self.spread_signal(ob)
        depth    = self.depth_imbalance(ob)
        velocity = self.mid_velocity(window_s=30)
        flow     = self.trade_flow_signal(ob)
        liq      = self.total_liquidity(ob)

        return {
            "spread":              snap.spread,
            "spread_pct":          snap.spread_pct,
            "spread_conf_mult":    spread["confidence_mult"],
            "imb_l1":              depth["imb_l1"],
            "imb_l5":              depth["imb_l5"],
            "imb_l10":             depth["imb_l10"],
            "imb_consistency":     depth["consistency"],
            "imb_score":           depth["score"],
            "wmp":                 snap.wmp,
            "wmp_vs_mid":          round(snap.wmp - snap.mid, 5),
            "mid_velocity":        velocity["velocity"],
            "mid_acceleration":    velocity["acceleration"],
            "mid_vel_score":       velocity["score"],
            "buy_pressure":        flow["buy_pressure"],
            "sell_pressure":       flow["sell_pressure"],
            "flow_delta":          flow["delta"],
            "flow_score":          flow["score"],
            "total_liquidity":     liq["total"],
            "liq_ratio":           liq["liq_ratio"],
            "size_mult":           liq["size_mult"],
            "best_bid":            snap.best_bid,
            "best_ask":            snap.best_ask,
            "mid":                 snap.mid,
            "depth_bid_levels":    snap.depth_bid,
            "depth_ask_levels":    snap.depth_ask,
        }
