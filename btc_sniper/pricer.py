import numpy as np
import time
from scipy.stats import norm

def bs_binary_price(S: float, K: float, T_seconds: float, sigma_annual: float, direction: str) -> float:
    """
    Prix théorique Black-Scholes d'une option binaire.
    
    S      : prix BTC actuel
    K      : prix BTC au début de la fenêtre (window_open)
    T_s    : secondes restantes avant expiration
    sigma  : volatilité annualisée BTC (ex: 0.85 = 85%)
    
    Retourne un float entre 0.0 et 1.0.
    """
    if T_seconds <= 0 or sigma_annual <= 0 or K <= 0:
        return 0.5   # fallback neutre

    T_annual = T_seconds / 31_536_000.0
    d2 = np.log(S / K) / (sigma_annual * np.sqrt(T_annual))

    if direction == "UP":
        return float(norm.cdf(d2))
    else:
        return float(norm.cdf(-d2))


def realized_volatility(candles: list, window: int = 15) -> float:
    """
    Calcule la volatilité annualisée BTC sur les N dernières candles 1-minute.
    """
    if not candles:
        return 0.85

    closes = [float(c.get("close", c.get("c", 0))) if isinstance(c, dict) else float(c.close) for c in candles[-window:]]
    if len(closes) < 2:
        return 0.85   # default BTC ~85% vol annualisée

    log_returns = np.diff(np.log(closes))
    sigma_1min  = float(np.std(log_returns))
    if sigma_1min == 0:
        return 0.85
    sigma_ann   = sigma_1min * np.sqrt(525_600)

    # Clamp entre 20% et 300% (valeurs réalistes BTC)
    return float(np.clip(sigma_ann, 0.20, 3.00))


def compute_edge(theoretical: float, market_ask: float, direction: str) -> dict:
    """
    Calcule notre edge sur le trade.
    """
    MIN_EDGE = 0.02   # 2 cents minimum pour trader

    edge     = theoretical - market_ask
    edge_pct = edge * 100

    # Kelly fraction pour paris binaires :
    p = theoretical
    q = 1 - p
    b = (1 - market_ask) / max(market_ask, 0.001)
    kelly = (p * b - q) / max(b, 0.001)
    kelly = max(min(kelly, 0.25), 0.0)   # cap 25%

    return {
        "edge":       float(round(edge, 4)),
        "edge_pct":   float(round(edge_pct, 2)),
        "theo":       float(round(theoretical, 4)),
        "market":     float(round(market_ask, 4)),
        "has_edge":   edge >= MIN_EDGE,
        "kelly_size": float(round(kelly, 4)),
    }


def ob_pressure_adjustment(ob, depth: int = 5) -> float:
    """
    Calcule l'ajustement de prix basé sur la pression de l'orderbook.
    """
    if not hasattr(ob, "bids") or not hasattr(ob, "asks") or not ob.bids or not ob.asks:
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
    wmp      = (bid_vwap + ask_vwap) / 2

    simple_mid = (ob.best_bid + ob.best_ask) / 2
    adjustment = wmp - simple_mid

    imbalance = bid_vol / max(bid_vol + ask_vol, 0.001)

    pressure_adj = adjustment + (imbalance - 0.5) * 0.01

    return float(np.clip(pressure_adj, -0.02, 0.02))


def momentum_adjustment(candles: list, ticks: list, direction: str) -> float:
    """
    Ajuste le prix selon le momentum BTC des 30 dernières secondes.
    """
    if len(ticks) < 10:
        return 0.0

    # Ticks are normally dicts with 'ts_ns', 'price', 'qty'
    last_tick = ticks[-1]
    now_ts = last_tick.get("ts_ns", time.time() * 1e9) if isinstance(last_tick, dict) else getattr(last_tick, "ts_ns", time.time() * 1e9)
    now = now_ts / 1e9
    
    recent = []
    for t in reversed(ticks):
        ts = t.get("ts_ns", 0) / 1e9 if isinstance(t, dict) else getattr(t, "ts_ns", 0) / 1e9
        if now - ts <= 30:
            recent.insert(0, t)
        else:
            break

    if len(recent) < 3:
        return 0.0

    prices = [float(t.get("price", t.get("p", 0))) if isinstance(t, dict) else float(getattr(t, "price", getattr(t, "p", 0))) for t in recent]
    sizes  = [float(t.get("qty", t.get("q", 1))) if isinstance(t, dict) else float(getattr(t, "qty", getattr(t, "q", 1))) for t in recent]

    total_vol = sum(sizes)
    if total_vol == 0:
        return 0.0

    vwap_30s = sum(p * s for p, s in zip(prices, sizes)) / total_vol
    current  = prices[-1]

    mom_up   = current > vwap_30s
    aligned  = (mom_up and direction == "UP") or (not mom_up and direction == "DOWN")

    pct_diff = abs(current - vwap_30s) / max(vwap_30s, 1)
    intensity = min(pct_diff * 100, 1.0)   # 0 à 1

    if aligned:
        adj = +0.010 * intensity
    else:
        adj = -0.015 * intensity

    return float(np.clip(adj, -0.015, 0.015))


def compute_smart_price(
    S: float, K: float, T_seconds: float, direction: str, 
    ob: object, candles: list, ticks: list, bankroll: float, mode: str = "safe"
) -> dict:
    """
    LE PRICER COMPLET.
    """
    # ── ÉTAPE 1 : Volatilité réalisée ───────────────
    sigma = realized_volatility(candles, window=15)

    # ── ÉTAPE 2 : Prix Black-Scholes ────────────────
    bs_price = bs_binary_price(S, K, T_seconds, sigma, direction)

    # ── ÉTAPE 3 : Prix marché actuel ────────────────
    best_ask = getattr(ob, "best_ask", 0.0)
    best_bid = getattr(ob, "best_bid", 0.0)
    mid      = getattr(ob, "mid", 0.5)
    
    market_ask = best_ask if best_ask > 0 else mid + 0.01
    market_bid = best_bid if best_bid > 0 else mid - 0.01

    # ── ÉTAPE 4 : Edge ──────────────────────────────
    edge_info = compute_edge(bs_price, market_ask, direction)

    # ── ÉTAPE 5 : Ajustements ───────────────────────
    ob_adj  = ob_pressure_adjustment(ob)
    
    import time
    mom_adj = momentum_adjustment(candles, ticks, direction)

    # ── ÉTAPE 6 : Prix d'entrée final ───────────────
    raw_entry = market_ask + ob_adj + mom_adj

    if T_seconds < 60:
        time_urgency = (60 - T_seconds) / 60 * 0.005
        raw_entry += time_urgency

    entry_price = round(np.clip(raw_entry, 0.01, 0.97), 4)

    # ── ÉTAPE 7 : Taille Kelly ──────────────────────
    mode_caps = {"safe": 0.20, "aggressive": 0.35, "degen": 0.50}
    cap       = mode_caps.get(mode, 0.20)

    kelly_raw = edge_info["kelly_size"]
    kelly_adj = min(kelly_raw * cap / 0.25, cap)  # scale au mode
    bet_size  = round(min(bankroll * kelly_adj, 50.0), 2)        # hard cap $50

    # ── ÉTAPE 8 : Prix de sortie cible ──────────────
    SPREAD_TARGET = 0.018
    sell_target   = round(min(bs_price + SPREAD_TARGET, 0.97), 4)

    # ── ÉTAPE 9 : Expected Value ─────────────────────
    p_win   = bs_price
    profit  = (sell_target - entry_price) / max(entry_price, 0.001)
    loss    = 1.0   # perte totale si résolution adverse
    ev      = p_win * profit - (1 - p_win) * loss
    ev_pct  = round(ev * 100, 2)

    # ── DÉCISION FINALE ─────────────────────────────
    should_trade = bool(
        edge_info["has_edge"]          # edge > 2 cents
        and ev > 0                     # EV positif
        and entry_price < 0.85         # pas trop cher
        and T_seconds > 30             # assez de temps
        and bet_size >= 1.0            # mise minimale $1
    )

    return {
        "S":            S,
        "K":            K,
        "T_seconds":    T_seconds,
        "sigma":        round(sigma, 4),
        "direction":    direction,
        "bs_price":     round(bs_price, 4),
        "market_ask":   round(market_ask, 4),
        "entry_price":  entry_price,
        "sell_target":  sell_target,
        "ob_adj":       round(ob_adj, 4),
        "mom_adj":      round(mom_adj, 4),
        "edge":         edge_info["edge"],
        "edge_pct":     edge_info["edge_pct"],
        "kelly_raw":    round(kelly_raw, 4),
        "kelly_adj":    round(kelly_adj, 4),
        "bet_size":     bet_size,
        "ev":           round(ev, 4),
        "ev_pct":       ev_pct,
        "should_trade": should_trade,
        "reason":       ("✅ TRADE" if should_trade else f"❌ SKIP edge={edge_info['edge_pct']:.1f}% ev={ev_pct:.1f}% T={T_seconds:.0f}s"),
    }

def taker_fee_rate(price: float) -> float:
    """
    Taux de fee taker selon le prix du token.
    Formule parabole : max 1.56% à price=0.50
    Quasi 0% aux extrêmes (price < 0.10 ou > 0.90)

    fee = 1.56% × 4 × p × (1-p)
    """
    fee = 0.0156 * 4 * price * (1 - price)
    return round(fee, 6)

def net_pnl_taker(entry: float, exit_price: float, size: float) -> float:
    """
    P&L net après fees taker des deux côtés.
    """
    fee_entry = taker_fee_rate(entry) * size * entry
    fee_exit  = taker_fee_rate(exit_price) * size * exit_price
    gross_pnl = (exit_price - entry) * size
    return round(gross_pnl - fee_entry - fee_exit, 4)

def net_pnl_maker(entry: float, exit_price: float, size: float) -> float:
    """
    P&L net avec stratégie maker/maker (fee = 0%).
    """
    return round((exit_price - entry) * size, 4)

def select_entry_mode(
    token_price:   float,
    T_remaining:   float,
    confidence:    float,
    spread:        float,
) -> dict:
    """
    Décide le MODE D'ENTRÉE optimal selon la situation.
    3 modes possibles :
    MODE A — MAKER + HOLD (meilleur dans la majorité)
    MODE B — TAKER + HOLD EXTRÊMES (opportunité tardive)
    MODE C — MAKER + SCALP (spread capture)
    """
    fee    = taker_fee_rate(token_price)
    spread_ok  = spread < 0.06
    price_mid  = 0.30 < token_price < 0.70
    price_ext  = token_price > 0.82 or token_price < 0.18
    time_ok    = T_remaining > 90
    time_early = T_remaining > 150

    # MODE B — Extrêmes quasi-gratuits
    if price_ext and T_remaining < 120 and confidence > 0.70:
        exp_profit = abs(1.0 - token_price) / max(token_price, 0.001)
        exp_net    = exp_profit - fee
        if exp_net > 0.05:   # minimum 5% net
            return {
                "mode":         "B_TAKER_EXTREME",
                "order_type":   "TAKER",
                "fee_rate":     fee,
                "exp_net_pct":  round(exp_net * 100, 2),
                "should_trade": True,
                "reason": (
                    f"✅ MODE B | price={token_price:.3f} "
                    f"fee={fee*100:.2f}% "
                    f"exp_net={exp_net*100:.1f}% "
                    f"T={T_remaining:.0f}s"
                ),
            }

    # MODE A — Maker + Hold (zone médiane)
    if price_mid and time_ok and confidence > 0.35 and spread_ok:
        exp_profit = abs(1.0 - token_price) / max(token_price, 0.001)
        exp_net    = exp_profit  # fee = 0% maker
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
                    f"exp_net={exp_net*100:.1f}% "
                    f"T={T_remaining:.0f}s"
                ),
            }

    # MODE C — Maker scalp (capture spread)
    if 0.40 < token_price < 0.60 and time_early \
            and spread > 0.015 and spread_ok \
            and 0.30 <= confidence < 0.50:
        exp_net = spread * 0.4   # on capture ~40% du spread
        # Pour le scalp, on garde un seuil plus bas sinon on ne scalpe jamais
        # mais la règle "Jamais entrer si exp_net < 0.05" est globale
        # Si c'est absolu, alors Mode C doit aussi être > 0.05 (5 cents)
        # Mais 5 cents de spread capture c'est énorme. 
        # Je vais suivre la consigne à la lettre : 0.05 (5% aka 5 cents)
        if exp_net > 0.05:
            return {
                "mode":         "C_MAKER_SCALP",
                "order_type":   "MAKER",
                "fee_rate":     0.0,
                "exp_net_pct":  round(exp_net * 100, 2),
                "should_trade": True,
                "reason": (
                    f"✅ MODE C | spread={spread:.4f} "
                    f"capture={exp_net*100:.2f}% "
                    f"fee=0% maker/maker"
                ),
            }

    # SKIP
    reasons = []
    if not spread_ok:  reasons.append(f"spread={spread:.3f}>0.06")
    if not time_ok and not (price_ext and confidence > 0.70): reasons.append(f"T={T_remaining:.0f}s<90s") # Allowing Mode B with less time
    if not price_mid and not price_ext:
        reasons.append(f"price={token_price:.3f} zone morte")

    return {
        "mode":         "SKIP",
        "order_type":   None,
        "fee_rate":     fee,
        "exp_net_pct":  0.0,
        "should_trade": False,
        "reason":       f"❌ SKIP | {' | '.join(reasons)}",
    }
