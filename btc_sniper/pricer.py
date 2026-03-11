"""
pricer.py — Binary Option Pricer for BTC
Corrections appliquées :
  - EV calculation correcte (perte = entry_price, pas 1.0)
  - Mode C threshold réaliste (0.008 au lieu de 0.05)
  - Import time déplacé en haut
  - BS avec drift optionnel (r param)
  - Volatilité Garman-Klass si OHLC dispo
  - Kelly : half-Kelly standard + cap de mode
  - ob_pressure normalisé en %
  - momentum_adjustment utilise candles pour confirmation
  - time_urgency corrigé (réduit sell_target, n'augmente plus entry)
  - net_pnl_taker : paramètre 'contracts' explicite
  - select_entry_mode : Mode B threshold corrigé
"""

import time
import numpy as np
from scipy.stats import norm


# ─────────────────────────────────────────────
#  CONSTANTES GLOBALES
# ─────────────────────────────────────────────

MIN_EDGE          = 0.02    # edge minimum pour trader (2 cents)
SPREAD_TARGET     = 0.018   # spread cible maker
MODE_C_MIN_NET    = 0.008   # 0.8 cent minimum pour scalp Mode C
MINUTES_PER_YEAR  = 525_600
SECONDS_PER_YEAR  = 31_536_000.0
DEFAULT_SIGMA     = 0.85    # vol BTC par défaut ~85% annualisée


# ─────────────────────────────────────────────
#  1. BLACK-SCHOLES BINARY
# ─────────────────────────────────────────────

def bs_binary_price(
    S: float,
    K: float,
    T_seconds: float,
    sigma_annual: float,
    direction: str,
    r: float = 0.0,
) -> float:
    """
    Prix théorique Black-Scholes d'une option binaire cash-or-nothing.

    S            : prix BTC actuel
    K            : prix BTC au début de la fenêtre (window_open / strike)
    T_seconds    : secondes restantes avant expiration
    sigma_annual : volatilité annualisée BTC (ex: 0.85 = 85%)
    direction    : "UP" ou "DOWN"
    r            : taux sans risque annualisé (défaut 0.0 pour crypto CT)

    Retourne un float entre 0.0 et 1.0.
    """
    if T_seconds <= 0 or sigma_annual <= 0 or K <= 0 or S <= 0:
        return 0.5  # fallback neutre

    T_annual = T_seconds / SECONDS_PER_YEAR

    # d2 avec drift complet (r - 0.5σ²)
    d2 = (
        np.log(S / K) + (r - 0.5 * sigma_annual ** 2) * T_annual
    ) / (sigma_annual * np.sqrt(T_annual))

    if direction == "UP":
        return float(norm.cdf(d2))
    else:
        return float(norm.cdf(-d2))


# ─────────────────────────────────────────────
#  2. VOLATILITÉ RÉALISÉE (Garman-Klass + fallback)
# ─────────────────────────────────────────────

def realized_volatility(candles: list, window: int = 15) -> float:
    """
    Calcule la volatilité annualisée BTC sur les N dernières candles 1-minute.

    Priorité :
      1. Garman-Klass (OHLC) — 4× plus efficace statistiquement
      2. Close-to-close classique
      3. Défaut 85%
    """
    if not candles:
        return DEFAULT_SIGMA

    recent = candles[-window:]

    # ── Tentative Garman-Klass ──────────────────────────────────────────────
    try:
        hl_terms = []
        for c in recent:
            if isinstance(c, dict):
                h = float(c.get("high", c.get("h", 0)))
                l = float(c.get("low",  c.get("l", 0)))
            else:
                h = float(getattr(c, "high", 0))
                l = float(getattr(c, "low",  0))

            if h > 0 and l > 0 and h >= l:
                hl_terms.append(0.5 * np.log(h / l) ** 2)

        if len(hl_terms) >= 2:
            sigma_1min = float(np.sqrt(np.mean(hl_terms)))
            if sigma_1min > 0:
                return float(np.clip(sigma_1min * np.sqrt(MINUTES_PER_YEAR), 0.20, 3.00))
    except (KeyError, TypeError, ValueError):
        pass

    # ── Fallback close-to-close ─────────────────────────────────────────────
    try:
        closes = []
        for c in recent:
            if isinstance(c, dict):
                closes.append(float(c.get("close", c.get("c", 0))))
            else:
                closes.append(float(getattr(c, "close", 0)))

        closes = [p for p in closes if p > 0]
        if len(closes) < 2:
            return DEFAULT_SIGMA

        log_returns = np.diff(np.log(closes))
        sigma_1min  = float(np.std(log_returns))
        if sigma_1min == 0:
            return DEFAULT_SIGMA

        return float(np.clip(sigma_1min * np.sqrt(MINUTES_PER_YEAR), 0.20, 3.00))

    except (TypeError, ValueError):
        return DEFAULT_SIGMA


# ─────────────────────────────────────────────
#  3. EDGE
# ─────────────────────────────────────────────

def compute_edge(theoretical: float, market_ask: float, direction: str) -> dict:
    """
    Calcule notre edge sur le trade.

    Kelly binaire :  (p·b − q) / b
    avec b = gain net par dollar risqué = (1 − ask) / ask
    """
    edge     = theoretical - market_ask
    edge_pct = edge * 100

    p = theoretical
    q = 1.0 - p
    b = (1.0 - market_ask) / max(market_ask, 0.001)

    kelly_raw = (p * b - q) / max(b, 0.001)
    kelly_raw = float(max(min(kelly_raw, 0.25), 0.0))   # cap brut 25%

    return {
        "edge":       float(round(edge, 4)),
        "edge_pct":   float(round(edge_pct, 2)),
        "theo":       float(round(theoretical, 4)),
        "market":     float(round(market_ask, 4)),
        "has_edge":   edge >= MIN_EDGE,
        "kelly_size": kelly_raw,
    }


# ─────────────────────────────────────────────
#  4. ORDERBOOK PRESSURE
# ─────────────────────────────────────────────

def ob_pressure_adjustment(ob, depth: int = 5) -> float:
    """
    Calcule l'ajustement de prix basé sur la pression de l'orderbook.

    Retourne un ajustement normalisé en fraction de prix (pas en dollars BTC).
    Clampé entre -0.02 et +0.02.
    """
    if not hasattr(ob, "bids") or not hasattr(ob, "asks"):
        return 0.0
    if not ob.bids or not ob.asks:
        return 0.0

    top_bids = sorted(ob.bids.items(), reverse=True)[:depth]
    top_asks = sorted(ob.asks.items())[:depth]

    if not top_bids or not top_asks:
        return 0.0

    bid_vol = sum(s for _, s in top_bids)
    ask_vol = sum(s for _, s in top_asks)
    if bid_vol == 0 or ask_vol == 0:
        return 0.0

    bid_vwap = sum(p * s for p, s in top_bids) / bid_vol
    ask_vwap = sum(p * s for p, s in top_asks) / ask_vol
    wmp      = (bid_vwap + ask_vwap) / 2.0

    simple_mid = (ob.best_bid + ob.best_ask) / 2.0

    # ✅ Normalisation : ajustement en % du mid (pas en dollars bruts)
    norm_adj  = (wmp - simple_mid) / max(simple_mid, 1.0)

    imbalance = bid_vol / max(bid_vol + ask_vol, 0.001)
    pressure_adj = norm_adj + (imbalance - 0.5) * 0.01

    return float(np.clip(pressure_adj, -0.02, 0.02))


# ─────────────────────────────────────────────
#  5. MOMENTUM ADJUSTMENT
# ─────────────────────────────────────────────

def momentum_adjustment(candles: list, ticks: list, direction: str) -> float:
    """
    Ajuste le prix selon le momentum BTC des 30 dernières secondes (ticks)
    et confirme avec le momentum 1-minute (candles).

    - Signal fort   (ticks + candles alignés)  → ±0.015
    - Signal faible (ticks seuls)              → ±0.010
    - Contradiction (ticks vs candles)         → 0.0
    """
    if len(ticks) < 10:
        return 0.0

    # ── Signal ticks (30s) ──────────────────────────────────────────────────
    last_tick = ticks[-1]
    now_ts = (
        last_tick.get("ts_ns", time.time() * 1e9)
        if isinstance(last_tick, dict)
        else getattr(last_tick, "ts_ns", time.time() * 1e9)
    )
    now = now_ts / 1e9

    recent = []
    for t in reversed(ticks):
        ts = (
            t.get("ts_ns", 0) / 1e9
            if isinstance(t, dict)
            else getattr(t, "ts_ns", 0) / 1e9
        )
        if now - ts <= 30:
            recent.insert(0, t)
        else:
            break

    if len(recent) < 3:
        return 0.0

    def _price(t):
        return float(t.get("price", t.get("p", 0)) if isinstance(t, dict)
                     else getattr(t, "price", getattr(t, "p", 0)))

    def _qty(t):
        return float(t.get("qty", t.get("q", 1)) if isinstance(t, dict)
                     else getattr(t, "qty", getattr(t, "q", 1)))

    prices    = [_price(t) for t in recent]
    sizes     = [_qty(t)   for t in recent]
    total_vol = sum(sizes)

    if total_vol == 0:
        return 0.0

    vwap_30s  = sum(p * s for p, s in zip(prices, sizes)) / total_vol
    current   = prices[-1]

    tick_up   = current > vwap_30s
    pct_diff  = abs(current - vwap_30s) / max(vwap_30s, 1.0)
    intensity = min(pct_diff * 100, 1.0)

    # ── Confirmation candles (1-minute) ─────────────────────────────────────
    candle_up = None
    if len(candles) >= 2:
        try:
            def _close(c):
                return float(
                    c.get("close", c.get("c", 0))
                    if isinstance(c, dict)
                    else getattr(c, "close", 0)
                )
            c_prev  = _close(candles[-2])
            c_last  = _close(candles[-1])
            if c_prev > 0:
                candle_up = c_last > c_prev
        except (TypeError, AttributeError):
            pass

    tick_aligned   = (tick_up and direction == "UP") or (not tick_up and direction == "DOWN")
    candle_aligned = (candle_up is None) or \
                     (candle_up and direction == "UP") or \
                     (not candle_up and direction == "DOWN")

    # Contradiction entre ticks et candles → signal annulé
    if candle_up is not None and (tick_up != candle_up):
        return 0.0

    if tick_aligned and candle_aligned:
        # Signal fort : les deux timeframes confirment
        adj = +0.015 * intensity
    elif tick_aligned:
        # Signal faible : ticks seuls
        adj = +0.010 * intensity
    else:
        # Contre le sens → pénalité
        adj = -0.015 * intensity

    return float(np.clip(adj, -0.015, 0.015))


# ─────────────────────────────────────────────
#  6. SMART PRICER COMPLET
# ─────────────────────────────────────────────

def compute_smart_price(
    S: float,
    K: float,
    T_seconds: float,
    direction: str,
    ob: object,
    candles: list,
    ticks: list,
    bankroll: float,
    mode: str = "safe",
    r: float = 0.0,
) -> dict:
    """
    LE PRICER COMPLET.

    Retourne un dict avec tous les signaux, prix, tailles et la décision finale.
    """
    # ── ÉTAPE 1 : Volatilité réalisée ───────────────────────────────────────
    sigma = realized_volatility(candles, window=15)

    # ── ÉTAPE 2 : Prix Black-Scholes ────────────────────────────────────────
    bs_price = bs_binary_price(S, K, T_seconds, sigma, direction, r=r)

    # ── ÉTAPE 3 : Prix marché actuel ────────────────────────────────────────
    best_ask = getattr(ob, "best_ask", 0.0)
    best_bid = getattr(ob, "best_bid", 0.0)
    mid      = getattr(ob, "mid", 0.5)

    market_ask = best_ask if best_ask > 0 else mid + 0.01
    market_bid = best_bid if best_bid > 0 else mid - 0.01
    bid_ask_spread = round(market_ask - market_bid, 4)

    # ── ÉTAPE 4 : Edge ──────────────────────────────────────────────────────
    edge_info = compute_edge(bs_price, market_ask, direction)

    # ── ÉTAPE 5 : Ajustements ───────────────────────────────────────────────
    ob_adj  = ob_pressure_adjustment(ob)
    mom_adj = momentum_adjustment(candles, ticks, direction)

    # ── ÉTAPE 6 : Prix d'entrée final ───────────────────────────────────────
    raw_entry   = market_ask + ob_adj + mom_adj
    entry_price = round(float(np.clip(raw_entry, 0.01, 0.97)), 4)

    # ── ÉTAPE 7 : Prix de sortie cible ──────────────────────────────────────
    sell_target = round(min(bs_price + SPREAD_TARGET, 0.97), 4)

    # ✅ Urgence temporelle : on réduit la cible de vente (plus de marge)
    #    au lieu d'augmenter le prix d'entrée
    if T_seconds < 60:
        time_urgency = (60 - T_seconds) / 60 * 0.005
        sell_target  = round(max(sell_target - time_urgency, entry_price + 0.002), 4)

    # ── ÉTAPE 8 : Taille Kelly ──────────────────────────────────────────────
    mode_caps = {"safe": 0.20, "aggressive": 0.35, "degen": 0.50}
    cap       = mode_caps.get(mode, 0.20)

    # ✅ Half-Kelly standard (plus conservateur) puis cap selon le mode
    kelly_raw = edge_info["kelly_size"]
    half_kelly = kelly_raw * 0.5
    kelly_adj  = float(min(half_kelly, cap))
    bet_size   = round(min(bankroll * kelly_adj, 50.0), 2)   # hard cap $50

    # ── ÉTAPE 9 : Expected Value ─────────────────────────────────────────────
    # ✅ EV correct : gain = sell_target - entry_price
    #                 perte = entry_price (ce qu'on a misé, pas 1.0)
    p_win    = bs_price
    p_loss   = 1.0 - p_win
    gain     = sell_target - entry_price
    ev       = p_win * gain - p_loss * entry_price
    ev_pct   = round(ev * 100, 2)

    # ── DÉCISION FINALE ─────────────────────────────────────────────────────
    should_trade = bool(
        edge_info["has_edge"]          # edge > 2 cents
        and ev > 0                     # EV positif
        and entry_price < 0.85         # pas trop cher
        and T_seconds > 45             # ✅ 45s au lieu de 30s (entrée plus prudente)
        and bet_size >= 1.0            # mise minimale $1
    )

    reason = (
        "✅ TRADE"
        if should_trade
        else (
            f"❌ SKIP "
            f"edge={edge_info['edge_pct']:.1f}% "
            f"ev={ev_pct:.1f}% "
            f"T={T_seconds:.0f}s "
            f"entry={entry_price:.3f}"
        )
    )

    return {
        # Inputs
        "S":              S,
        "K":              K,
        "T_seconds":      T_seconds,
        "direction":      direction,
        "mode":           mode,
        # Volatilité
        "sigma":          round(sigma, 4),
        # Prix
        "bs_price":       round(bs_price, 4),
        "market_ask":     round(market_ask, 4),
        "market_bid":     round(market_bid, 4),
        "bid_ask_spread": bid_ask_spread,
        "entry_price":    entry_price,
        "sell_target":    sell_target,
        # Ajustements
        "ob_adj":         round(ob_adj, 4),
        "mom_adj":        round(mom_adj, 4),
        # Edge
        "edge":           edge_info["edge"],
        "edge_pct":       edge_info["edge_pct"],
        # Kelly
        "kelly_raw":      round(kelly_raw, 4),
        "kelly_adj":      round(kelly_adj, 4),
        "bet_size":       bet_size,
        # EV
        "ev":             round(ev, 4),
        "ev_pct":         ev_pct,
        # Décision
        "should_trade":   should_trade,
        "reason":         reason,
    }


# ─────────────────────────────────────────────
#  7. FEES
# ─────────────────────────────────────────────

def taker_fee_rate(price: float) -> float:
    """
    Taux de fee taker selon le prix du token.
    Formule parabolique : max 1.56% à price=0.50
    Quasi 0% aux extrêmes (price < 0.10 ou > 0.90)

        fee = 1.56% × 4 × p × (1 − p)
    """
    price = float(np.clip(price, 0.0, 1.0))
    fee   = 0.0156 * 4 * price * (1.0 - price)
    return round(fee, 6)


def net_pnl_taker(entry: float, exit_price: float, contracts: float) -> float:
    """
    P&L net après fees taker des deux côtés.

    entry, exit_price : prix en fraction [0, 1]
    contracts         : nombre de contrats (unité homogène des deux côtés)
    """
    notional_entry = entry       * contracts
    notional_exit  = exit_price  * contracts

    fee_entry  = taker_fee_rate(entry)      * notional_entry
    fee_exit   = taker_fee_rate(exit_price) * notional_exit
    gross_pnl  = notional_exit - notional_entry

    return round(gross_pnl - fee_entry - fee_exit, 4)


def net_pnl_maker(entry: float, exit_price: float, contracts: float) -> float:
    """
    P&L net avec stratégie maker/maker (fee = 0%).

    contracts : nombre de contrats
    """
    return round((exit_price - entry) * contracts, 4)


# ─────────────────────────────────────────────
#  8. ENTRY MODE SELECTOR
# ─────────────────────────────────────────────

def select_entry_mode(
    token_price: float,
    T_remaining: float,
    confidence:  float,
    spread:      float,
) -> dict:
    """
    Décide le MODE D'ENTRÉE optimal selon la situation.

    MODE A — MAKER + HOLD   : zone médiane, meilleur dans la majorité des cas
    MODE B — TAKER EXTRÊMES : opportunité tardive sur prix quasi-certain
    MODE C — MAKER + SCALP  : capture de spread en zone neutre
    SKIP                    : aucune condition satisfaite
    """
    fee        = taker_fee_rate(token_price)
    spread_ok  = spread < 0.06
    price_mid  = 0.30 < token_price < 0.70
    price_ext  = token_price > 0.82 or token_price < 0.18
    time_ok    = T_remaining > 90
    time_early = T_remaining > 150

    # ── MODE B — Extrêmes quasi-gratuits ────────────────────────────────────
    if price_ext and T_remaining < 120 and confidence > 0.70:
        # ✅ threshold adapté : extrêmes à faible fee, seuil 2% net
        exp_profit = abs(1.0 - token_price) / max(token_price, 0.001)
        exp_net    = exp_profit - fee
        if exp_net > 0.02:
            return {
                "mode":         "B_TAKER_EXTREME",
                "order_type":   "TAKER",
                "fee_rate":     fee,
                "exp_net_pct":  round(exp_net * 100, 2),
                "should_trade": True,
                "reason": (
                    f"✅ MODE B | price={token_price:.3f} "
                    f"fee={fee * 100:.2f}% "
                    f"exp_net={exp_net * 100:.1f}% "
                    f"T={T_remaining:.0f}s"
                ),
            }

    # ── MODE A — Maker + Hold ───────────────────────────────────────────────
    if price_mid and time_ok and confidence > 0.35 and spread_ok:
        exp_profit = abs(1.0 - token_price) / max(token_price, 0.001)
        exp_net    = exp_profit   # fee = 0% maker
        if exp_net > 0.05:
            return {
                "mode":         "A_MAKER_HOLD",
                "order_type":   "MAKER",
                "fee_rate":     0.0,
                "exp_net_pct":  round(exp_net * 100, 2),
                "should_trade": True,
                "reason": (
                    f"✅ MODE A | price={token_price:.3f} "
                    f"fee=0% maker "
                    f"exp_net={exp_net * 100:.1f}% "
                    f"T={T_remaining:.0f}s"
                ),
            }

    # ── MODE C — Maker scalp ────────────────────────────────────────────────
    # ✅ threshold réaliste : 0.8 cents net au lieu de 5 cents impossible
    if (
        0.40 < token_price < 0.60
        and time_early
        and spread > 0.015
        and spread_ok
        and 0.30 <= confidence < 0.50
    ):
        exp_net = spread * 0.4   # capture ~40% du spread
        if exp_net > MODE_C_MIN_NET:
            return {
                "mode":         "C_MAKER_SCALP",
                "order_type":   "MAKER",
                "fee_rate":     0.0,
                "exp_net_pct":  round(exp_net * 100, 2),
                "should_trade": True,
                "reason": (
                    f"✅ MODE C | spread={spread:.4f} "
                    f"capture={exp_net * 100:.2f}% "
                    f"fee=0% maker/maker"
                ),
            }

    # ── SKIP ────────────────────────────────────────────────────────────────
    reasons = []
    if not spread_ok:
        reasons.append(f"spread={spread:.3f}>0.06")
    if not time_ok and not (price_ext and confidence > 0.70):
        reasons.append(f"T={T_remaining:.0f}s<90s")
    if not price_mid and not price_ext:
        reasons.append(f"price={token_price:.3f} zone morte")
    if confidence <= 0.35:
        reasons.append(f"confidence={confidence:.2f}<0.35")

    return {
        "mode":         "SKIP",
        "order_type":   None,
        "fee_rate":     fee,
        "exp_net_pct":  0.0,
        "should_trade": False,
        "reason":       f"❌ SKIP | {' | '.join(reasons) if reasons else 'no condition met'}",
    }
