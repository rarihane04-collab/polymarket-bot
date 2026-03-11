import os
import time
import requests
import argparse
import pandas as pd
from datetime import datetime
from collections import deque
from types import SimpleNamespace
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.panel import Panel

# Importer la stratégie et la config (SANS importer market)
from btc_sniper import strategy, config

BINANCE_REST = "https://api.binance.com/api/v3"

def generate_windows(days_back: int = 7) -> list[dict]:
    """
    Génère toutes les fenêtres 5-min des X derniers jours.
    Zéro appel API. Pur calcul arithmétique.
    
    Sur 7 jours : 7 × 24 × 12 = 2016 fenêtres
    Sur 30 jours : 30 × 24 × 12 = 8640 fenêtres
    """
    now      = int(time.time())
    end_ts   = (now   // 300) * 300   # fenêtre actuelle exclue
    start_ts = end_ts - (days_back * 86400)
    start_ts = (start_ts // 300) * 300

    windows = []
    ts = start_ts
    while ts < end_ts:
        windows.append({
            "start_ts": ts,
            "end_ts":   ts + 300,
            "slug":     f"btc-updown-5m-{ts}",
        })
        ts += 300

    print(f"[INFO] {len(windows)} fenêtres générées "
          f"sur {days_back} jours "
          f"({days_back * 24 * 12} attendues)")
    return windows

def fetch_window_data(start_ts: int) -> Optional[dict]:
    """
    Récupère toutes les données Binance pour une fenêtre 5-min.
    
    - 1 candle 5m  → résultat réel (UP/DOWN)
    - 50 candles 1m → indicateurs (RSI, EMA, momentum...)
    
    Aucune clé API requise.
    """
    start_ms = start_ts * 1000   # Binance attend des millisecondes

    try:
        # Candle 5m — pour le résultat réel
        r5 = requests.get(
            f"{BINANCE_REST}/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  "5m",
                "startTime": start_ms,
                "limit":     1,
            },
            timeout=8
        )
        r5.raise_for_status()
        raw5 = r5.json()
        if not raw5:
            return None
        c = raw5[0]

        open_p  = float(c[1])
        close_p = float(c[4])
        actual  = "UP" if close_p >= open_p else "DOWN"
        delta   = (close_p - open_p) / open_p * 100

        # Candles 1m — pour les indicateurs du signal
        # IMPORTANT : endTime = start_ms - 1 pour éviter de lire le futur !
        r1 = requests.get(
            f"{BINANCE_REST}/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  "1m",
                "endTime":   start_ms - 1,
                "limit":     50, # 50 candles pour que le RSI et les EMA se chauffent
            },
            timeout=8
        )
        r1.raise_for_status()
        candles_1m = [
            {
                "open_time": int(k[0]),
                "open":      float(k[1]),
                "high":      float(k[2]),
                "low":       float(k[3]),
                "close":     float(k[4]),
                "volume":    float(k[5]),
            }
            for k in r1.json()
        ]

        return {
            "open":       open_p,
            "close":      close_p,
            "high":       float(c[2]),
            "low":        float(c[3]),
            "volume":     float(c[5]),
            "actual":     actual,       # "UP" ou "DOWN"
            "delta_pct":  delta,        # ex: +0.12 ou -0.08
            "candles_1m": candles_1m,
        }

    except Exception as e:
        return None   # fenêtre ignorée silencieusement

def fetch_seed_candles(n: int = 1000) -> deque:
    """
    Fetch les N dernières candles 1m pour entraîner le ML.
    Appelé UNE SEULE FOIS avant la boucle de backtest.
    """
    r = requests.get(
        f"{BINANCE_REST}/klines",
        params={
            "symbol":   "BTCUSDT",
            "interval": "1m",
            "limit":    n,
        },
        timeout=15
    )
    r.raise_for_status()
    candles = deque(maxlen=n)
    for k in r.json():
        candles.append({
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        })
    print(f"[ML] {len(candles)} candles seedées pour training")
    return candles

def simulate_trade_smart(window_data, signal, bankroll, mode):
    from btc_sniper.pricer import compute_smart_price
    
    class FakeOB:
        def __init__(self, mid):
            self.mid = mid
            self.best_bid = mid - 0.01
            self.best_ask = mid + 0.01
            self.bids = {self.best_bid: 1000}
            self.asks = {self.best_ask: 1000}

    pricing = compute_smart_price(
        S         = window_data["close"],   # prix de clôture
        K         = window_data["open"],    # prix d'ouverture
        T_seconds = 150.0,                  # milieu de fenêtre
        direction = signal["direction"],
        ob        = FakeOB(0.50), # OB neutre
        candles   = window_data["candles_1m"],
        ticks     = [],
        bankroll  = bankroll,
        mode      = mode,
    )
    if not pricing["should_trade"]:
        return 0.0, True   # skip

    actual = window_data["actual"]
    if signal["direction"] == actual:
        pnl = pricing["bet_size"] * pricing["edge"]
    else:
        pnl = -pricing["bet_size"] * pricing["market_ask"]
    return pnl, False

def run_backtest(days_back:       int   = 7,
                 min_confidence:  float = 0.0,
                 mode:            str   = "safe",
                 initial_bankroll:float = 100.0,
                 max_workers:     int   = 8):
    """
    Lance le backtest complet 100% Binance.
    
    Utilise ThreadPoolExecutor pour fetcher les candles
    en parallèle (8 threads → ~8x plus rapide).
    """
    windows  = generate_windows(days_back)
    bankroll = initial_bankroll
    results  = []

    # Seed ML une fois
    seed_candles = fetch_seed_candles(1000)
    ml_model     = strategy.train_ml_model_async(seed_candles)
    print(f"[ML] Modèle entraîné sur {len(seed_candles)} candles")

    # Fetch en parallèle pour aller vite
    print(f"[FETCH] Fetching {len(windows)} fenêtres avec {max_workers} threads...")
    
    window_data = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_window_data, w["start_ts"]): w
            for w in windows
        }
        done = 0
        for future in as_completed(futures):
            w    = futures[future]
            data = future.result()
            if data:
                window_data[w["start_ts"]] = data
            done += 1
            if done % 200 == 0:
                print(f"  [{done}/{len(windows)}] fetched...")

    print(f"[FETCH] {len(window_data)} fenêtres avec données ({len(windows)-len(window_data)} vides ignorées)")

    # Simulation dans l'ordre chronologique
    for window in sorted(windows, key=lambda x: x["start_ts"]):
        data = window_data.get(window["start_ts"])
        if not data:
            continue

        # Construire les inputs de analyze()
        candles = deque(data["candles_1m"], maxlen=50)
        ticks   = deque(
            [{"price": c["close"], "qty": 1.0,
              "ts_ns": c["open_time"] * 1_000_000,
              "ts": c["open_time"] * 1_000_000}
             for c in data["candles_1m"]],
            maxlen=600
        )
        ob_neutral = SimpleNamespace(
            mid=0.50, best_bid=0.495, best_ask=0.505,
            book_imbalance=0.50, update_count=1,
            bids=[], asks=[],
        )

        # Signal
        sig = strategy.analyze(
            ticks, candles, ob_neutral,
            data["open"]
        )

        actual = data["actual"]

        # Skip si confidence insuffisante
        if (sig.direction in ("SKIP", "WAITING", None)
                or sig.confidence < min_confidence):
            skipped = True
            pnl     = 0.0
            would_win = False
            if sig.direction in ("UP", "DOWN"):
                would_win = (sig.direction == actual)
        else:
            pnl, skipped = simulate_trade_smart(
                window_data=data,
                signal={"direction": sig.direction, "confidence": sig.confidence},
                bankroll=bankroll,
                mode=mode
            )
            would_win = (sig.direction == actual)

        bankroll += pnl
        results.append({
            "slug":       window["slug"],
            "start_ts":   window["start_ts"],
            "predicted":  sig.direction,
            "actual":     actual,
            "confidence": round(sig.confidence, 3),
            "score":      round(getattr(sig, "total", 0), 3),
            "delta_pct":  round(data["delta_pct"], 4),
            "pnl":        pnl,
            "bankroll":   round(bankroll, 4),
            "skipped":    skipped,
            # Pour analyse : aurait-on gagné si on avait tradé ?
            "would_win":  would_win,
        })

    return results, bankroll

def print_report(results: list, initial_bankroll: float,
                 final_bankroll: float, mode: str):
    df      = pd.DataFrame(results)
    if len(df) == 0:
        print("[WARN] Aucun résultat à afficher.")
        return

    trades  = df[~df["skipped"]]
    skipped = df[df["skipped"]]
    wins    = trades[trades["pnl"] > 0]

    win_rate    = (len(wins) / max(len(trades), 1)) * 100
    skip_rate   = (len(skipped) / max(len(df), 1)) * 100
    total_pnl   = final_bankroll - initial_bankroll
    roi         = (total_pnl / initial_bankroll) * 100

    # Max drawdown
    peak = df["bankroll"].cummax()
    dd   = ((df["bankroll"] - peak) / peak).min() * 100

    # Sharpe
    if len(trades) > 1 and trades["pnl"].std() > 0:
        sharpe = (trades["pnl"].mean() / trades["pnl"].std() * (252 ** 0.5))
    else:
        sharpe = 0.0

    # "Would-have-won" sur les skips
    if "would_win" in df.columns:
        would_win_pct = df["would_win"].mean() * 100
    else:
        would_win_pct = 0.0

    avg_conf = trades["confidence"].mean() * 100 if len(trades) > 0 else 0.0

    console = Console()
    console.print(Panel(
        f"[bold]Windows total    :[/bold] {len(df)}\n"
        f"[bold]Trades placed    :[/bold] {len(trades)} "
            f"({skip_rate:.0f}% skipped)\n"
        f"[bold]Win rate         :[/bold] "
            f"[{'green' if win_rate>50 else 'red'}]"
            f"{win_rate:.1f}%[/]\n"
        f"[bold]Direction acc.   :[/bold] "
            f"{would_win_pct:.1f}% (toutes fenêtres)\n\n"
        f"[bold]Total P&L        :[/bold] "
            f"[{'green' if total_pnl>=0 else 'red'}]"
            f"{total_pnl:+.2f} USDC[/]\n"
        f"[bold]ROI              :[/bold] {roi:+.1f}%\n"
        f"[bold]Max Drawdown     :[/bold] {dd:.1f}%\n"
        f"[bold]Sharpe Ratio     :[/bold] {sharpe:.2f}\n\n"
        f"[bold]Avg confidence   :[/bold] "
            f"{avg_conf:.1f}%\n"
        f"[bold]Mode             :[/bold] {mode}",
        title="📊 BACKTEST RESULTS — Binance Only",
        border_style="cyan",
    ))

    # Sauvegarde
    os.makedirs("logs", exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = f"logs/backtest_{ts}.csv"
    df.to_csv(csv_path, index=False)
    print(f"💾 CSV: {csv_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Backtest BTC 5-min — 100% Binance")
    p.add_argument("--days",           type=int,   default=7)
    p.add_argument("--bankroll",       type=float, default=100.0)
    p.add_argument("--mode",           type=str,   default="safe",
                   choices=["safe","aggressive","degen"])
    p.add_argument("--min-confidence", type=float, default=0.0)
    p.add_argument("--workers",        type=int,   default=8)
    args = p.parse_args()

    results, final_bk = run_backtest(
        days_back        = args.days,
        min_confidence   = args.min_confidence,
        mode             = args.mode,
        initial_bankroll = args.bankroll,
        max_workers      = args.workers,
    )
    print_report(results, args.bankroll, final_bk, args.mode)
