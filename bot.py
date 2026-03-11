import time
import threading
import logging
import json
import signal
import sys
import os
import shutil
import requests
from datetime import datetime
from collections import deque
from btc_sniper import config, market, strategy, execution, display
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Global State
next_market = None
session_start = datetime.now()
stats = {
    "windows": 0, "pnl": 0.0, "win_rate": 0.0, 
    "bankroll": config.INITIAL_BANKROLL, "trades": 0,
    "bankroll_history": [config.INITIAL_BANKROLL]
}
fictitious_trade = None # {dir, price, size, token_id}

logging.basicConfig(filename="logs/sniper.log", level=logging.INFO)
logger = logging.getLogger("Master")

from btc_sniper.config import debug_logger, log_trade_event, log_report

def run_startup_checks() -> bool:
    checks = []

    # Binance API
    try:
        r = requests.get(f"{config.BINANCE_REST}/ping", timeout=5)
        checks.append(("Binance API",      r.status_code==200, ""))
    except Exception as e:
        checks.append(("Binance API",      False, str(e)))

    # Polymarket Gamma API
    try:
        r = requests.get(f"{config.GAMMA_API}/markets", params={"limit":1}, timeout=5)
        checks.append(("Polymarket Gamma", r.status_code==200, ""))
    except Exception as e:
        checks.append(("Polymarket Gamma", False, str(e)))

    # Polymarket CLOB API
    try:
        r = requests.get(config.CLOB_API, timeout=5)
        checks.append(("Polymarket CLOB",  r.status_code==200, ""))
    except Exception as e:
        checks.append(("Polymarket CLOB",  False, str(e)))

    # Marché BTC 5-min actif
    try:
        m  = market.resolve_market()
        ok = m is not None
        checks.append(("BTC 5min Market",
                       ok,
                       m.get("question","")[:40] if ok else "Not found"))
    except Exception as e:
        checks.append(("BTC 5min Market",  False, str(e)))

    # Candles seedées
    try:
        feed = strategy.BinanceFeed()
        feed.seed_data()
        ok = len(feed.candles_1m) >= 14
        checks.append(("Candles (≥14)",
                       ok, f"{len(feed.candles_1m)} candles"))
    except Exception as e:
        checks.append(("Candles Seed",     False, str(e)))

    # Mode PAPER confirmed
    checks.append(("Mode",
                   config.TRADING_LEVEL == 1,
                   "📋 PAPER TRADING" if config.TRADING_LEVEL==1
                   else "⚠️ TRADING_LEVEL != 1"))

    # Affichage tableau
    console = Console()
    from rich import box
    table   = Table(title="🔍 STARTUP CHECKS", box=box.ROUNDED)
    table.add_column("Check",  style="cyan",  width=22)
    table.add_column("Status", style="white", width=8)
    table.add_column("Info",   style="dim",   width=40)

    all_ok = True
    log_report(f"""
# 🤖 SESSION DRY RUN — {config.SESSION_ID}
**Date** : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}
**Mode** : PAPER TRADING (aucun argent réel)
**Bankroll initiale** : ${config.INITIAL_BANKROLL:.2f} USDC

## ✅ Startup Checks
""")

    for name, ok, info in checks:
        icon = "✅" if ok else "❌"
        table.add_row(name, icon, str(info))
        log_report(f"- {icon} {name}: {info}")
        if not ok and name not in ("Mode",):
            all_ok = False

    debug_logger.info(
        f"STARTUP | session={config.SESSION_ID} "
        f"bankroll={config.INITIAL_BANKROLL} "
        f"mode=PAPER TRADING_LEVEL={config.TRADING_LEVEL}"
    )

    console.print(table)
    console.print(
        f"\n  Bankroll initiale : "
        f"[bold green]${config.INITIAL_BANKROLL:.2f} USDC[/]\n"
        f"  Mode             : "
        f"[bold yellow]📋 PAPER TRADING (aucun argent réel)[/]\n"
    )
    return all_ok

def pre_live_warning():
    if config.TRADING_LEVEL < 2: return
    console = Console()
    console.print(Panel(
        "[red bold]⚠️  PASSAGE EN ARGENT RÉEL DÉTECTÉ[/red bold]\n\n"
        f"TRADING_LEVEL = {config.TRADING_LEVEL}\n"
        f"MAX_ORDER_SIZE = ${config.MAX_ORDER_SIZE:.2f} USDC\n\n"
        "Avant de continuer, confirme :\n"
        "  [✓] 20+ fenêtres testées en PAPER avec win rate > 50%\n"
        "  [✓] Clé privée dans .env uniquement (jamais dans le code)\n"
        "  [✓] Fonds suffisants sur Polygon Wallet\n"
        "  [✓] Je comprends que je peux perdre tout le capital\n\n"
        "[yellow]Tape CONFIRM pour continuer ou Ctrl+C pour annuler[/yellow]",
        title="[red]🔴 LIVE TRADING WARNING[/red]",
        border_style="red",
    ))
    try:
        inp = input("\n  → ").strip().upper()
        if inp != "CONFIRM":
            console.print("[yellow]⛔ Annulé. Lance avec TRADING_LEVEL=1.[/yellow]")
            sys.exit(0)
        console.print("[green]✅ Confirmé. Trading réel activé.[/green]")
    except KeyboardInterrupt:
        console.print("\n[yellow]⛔ Annulé.[/yellow]")
        sys.exit(0)

def generate_html_report(stats_dict, trades):
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"logs/report_{ts}.html"
    
    b_hist = stats_dict.get("bankroll_history", [config.INITIAL_BANKROLL])
    min_b = min(b_hist)
    max_b = max(b_hist)
    n = len(b_hist)
    
    points = []
    if n > 1:
        for i, val in enumerate(b_hist):
            x = (i / (n - 1)) * 860 + 20
            y = 200 - ((val - min_b) / (max_b - min_b + 0.01)) * 180
            points.append(f"{x},{y}")
    else:
        points = [f"20,100", f"880,100"]
        
    pts_str = " ".join(points)
    color = "lime" if b_hist[-1] >= b_hist[0] else "red"
    
    rows = ""
    for t in trades:
        tc = "lime" if t.get('pnl', 0) > 0 else "red" if t.get('pnl', 0) < 0 else "gray"
        rows += f"<tr style='color:{tc}'><td>{t['time']}</td><td>{t['dir']}</td><td>{t['size']}</td><td>{t['pnl']:+.4f}</td></tr>"

    html = f'''
    <html><body style='background:#1a1a2e; color:white; font-family:sans-serif; padding:20px;'>
    <h1>📊 Session Report {ts}</h1>
    <div style='display:flex; gap:20px; font-size:24px; margin-bottom:20px;'>
        <div>PNL: {stats_dict['pnl']:+.4f}</div>
        <div>Windows: {stats_dict['windows']}</div>
        <div>Bankroll: ${b_hist[-1]:.2f}</div>
    </div>
    
    <svg width="900" height="220" style="background:#0f0f1a; border:1px solid #333; border-radius:8px;">
        <polyline points="{pts_str}" fill="none" stroke="{color}" stroke-width="2"/>
    </svg>
    
    <table border="1" style="margin-top:20px; border-collapse: collapse; width:900px; text-align:left;">
        <tr><th style='padding:8px'>Time</th><th style='padding:8px'>Direction</th><th style='padding:8px'>Fills</th><th style='padding:8px'>PnL</th></tr>
        {rows}
    </table>
    </body></html>
    '''
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path

class WinRateTracker:
    """Tracks per-indicator accuracy and adjusts weights."""
    def __init__(self):
        self.history = deque(maxlen=50)
        self.weights = {
            "window_delta": 7.0, "ob_imbalance": 3.0, "tick_velocity": 2.0,
            "tick_accel": 2.0, "micro_momentum": 1.5, "rsi": 1.0, 
            "ema_cross": 0.5, "volume_surge": 1.0, "ml_score": 0.5
        }

    def record_window(self, breakdown: dict, predicted: str, actual: str):
        correct = (predicted == actual)
        self.history.append({"breakdown": breakdown, "correct": correct})
        if len(self.history) >= 10:
            self._rebalance()

    def _rebalance(self):
        # Very simple logic: if indicator was in same direction as win, increase weight
        for key in self.weights:
            wins = sum(1 for h in self.history if h['correct'] and abs(h['breakdown'].get(key, 0)) > 0.1)
            losses = sum(1 for h in self.history if not h['correct'] and abs(h['breakdown'].get(key, 0)) > 0.1)
            total = wins + losses
            if total > 5:
                rate = wins / total
                if rate > 0.6: self.weights[key] = min(self.weights[key] * 1.1, 10.0)
                elif rate < 0.4: self.weights[key] = max(self.weights[key] * 0.9, 0.5)

def prefetch_next():
    global next_market
    next_ts = market.get_next_window_ts()
    logger.info(f"📡 Pre-fetching market for {next_ts}...")
    next_market = market.resolve_market(next_ts)
    if next_market:
        logger.info(f"✅ Pre-fetched: {next_market['slug']}")

def size_position(bankroll: float, confidence: float, multiplier: float = 1.0) -> float:
    """Kelly-based sizing: f = p - q. Clamp 10-60%."""
    kelly = max(confidence - (1.0 - confidence), 0.05)
    raw = bankroll * kelly * multiplier
    return round(min(max(raw, bankroll * 0.10), bankroll * 0.60), 2)

class SniperBot:
    def __init__(self, mode: str = "safe"):
        self.mode = mode
        self.feed = strategy.BinanceFeed()
        self.tracker = WinRateTracker()
        self.executor = None # Will be SmartLimitEngine
        self.running = True
        self.current_market = None
        self.ob_yes = None
        self.ob_no = None
        self.trade_placed = False
        self.prefetch_started = False
        self.last_sig = None
        self._last_health_check = 0.0 # NEW: Cooldown tracking
        self.window_start = time.time()
        self.signal_history = deque(maxlen=1000)
        
        # Initialize display state
        display.state.start_time = datetime.now().strftime("%H:%M:%S")
        display.state.bankroll = stats["bankroll"]
        display.state.mode = mode
        
    def _direction_stable_for(self, seconds: float) -> bool:
        """Vérifie stabilité sur X secondes réelles (pas X items)."""
        now    = time.time()
        cutoff = now - seconds
        recent = [s for s in self.signal_history
                  if s.get("ts", 0) >= cutoff]
        if len(recent) < 3:
            return False
        dirs     = [s["direction"] for s in recent]
        dominant = max(set(dirs), key=dirs.count)
        return dirs.count(dominant) / len(dirs) >= 0.90

    def _health_check(self):
        sig = self.last_sig
        price = self.feed.ticks[-1]['price'] if self.feed.ticks else 0.0
        yes_mid = self.ob_yes.mid if self.ob_yes else 0.0
        no_mid = self.ob_no.mid if self.ob_no else 0.0
        
        config.debug_logger.info(
            f"HEALTH "
            f"btc={price:.2f} "
            f"open={self.feed.window_open_price:.2f} "
            f"delta={((price/max(self.feed.window_open_price,1))-1)*100:+.3f}% "
            f"ticks={len(self.feed.ticks)} "
            f"candles={len(self.feed.candles_1m)} "
            f"yes_bid={self.ob_yes.best_bid if self.ob_yes else 0.0:.3f} "
            f"yes_ask={self.ob_yes.best_ask if self.ob_yes else 0.0:.3f} "
            f"yes_mid={yes_mid:.3f} "
            f"no_mid={no_mid:.3f} "
            f"sum_mid={yes_mid + no_mid:.3f} "
            f"yes_updates={self.ob_yes.update_count if self.ob_yes else 0} "
            f"conf={getattr(sig, 'confidence', 0):.3f} "
            f"dir={getattr(sig, 'direction', '?')} "
            f"score={getattr(sig, 'total_score', 0):.2f} "
            f"executor={self.executor._running if self.executor else False}"
        )

    def run(self):
        global fictitious_trade, next_market
        self.feed.start()
        
        while self.running:
            # 1. Initial Market Resolution
                if self.current_market is None:
                    m = market.resolve_market()
                    if m:
                        self.current_market = m
                        # Bug fix: use clobTokenIds and outcomePrices for LiveOrderBook
                        tokens = json.loads(m.get("clobTokenIds", "[]"))
                        outcome_prices = json.loads(m.get("outcomePrices", "[0.5, 0.5]"))
                        
                        if len(tokens) < 2:
                            display.log("⚠️ Market found but clobTokenIds missing/invalid. Skipping...")
                            self.current_market = None
                            time.sleep(1)
                            continue
                            
                        yes_price = float(outcome_prices[0])
                        no_price = float(outcome_prices[1])
                        
                        # Le YES token = celui dont le prix est outcomes[0]
                        # Sur Polymarket btc-updown : outcomes[0] = UP price
                        if float(outcome_prices[0]) > float(outcome_prices[1]):
                            # outcomes[0] est le plus probable = NO si BTC baisse
                             pass

                        self.yes_token_id = tokens[0]
                        self.no_token_id = tokens[1]
                        
                        self.ob_yes = market.LiveOrderBook(self.yes_token_id, "YES")
                        self.ob_no = market.LiveOrderBook(self.no_token_id, "NO")
                        
                        # Pass to display
                        display.state.ob_yes = self.ob_yes
                        display.state.ob_no  = self.ob_no
                        
                        self.ob_yes.start()
                        self.ob_no.start()
                        
                        self.executor = execution.SmartLimitEngine(self.ob_yes, self.ob_no, lambda: self.last_sig, lambda: market.seconds_until_next_window(), bot=self)
                        self.executor.start()
                        self.feed.set_window_open(0)
                        self.trade_placed = False
                        display.state.market_slug = m['slug']
                        display.state.market_question = m.get('question', 'BTC Up/Down?')

                        debug_logger.info(
                            f"MARKET_NEW | slug={m['slug']} "
                            f"question='{m.get('question','')}' "
                            f"end={m.get('endDate','')} "
                            f"token_yes={self.yes_token_id[:16]}... "
                            f"token_no={self.no_token_id[:16]}... "
                            f"yes_price={yes_price:.3f} "
                            f"no_price={no_price:.3f} "
                            f"sum={yes_price+no_price:.3f}"
                        )

                        log_report(f"""
---
## 📊 Marché detected: {m.get('question','')}
- **Slug** : `{m['slug']}`
- **Token YES price** : {yes_price:.3f}
- **Token NO price**  : {no_price:.3f}
- **Sum** : {yes_price+no_price:.3f} (doit être ≈1.0)
""")

                        log_trade_event("MARKET_START", {
                            "slug":       m['slug'],
                            "question":   m.get('question',''),
                            "yes_price":  yes_price,
                            "no_price":   no_price,
                            "end_time":   m.get('endDate',''),
                        })
                    else:
                        display.log("Scanning for active markets...")
                        time.sleep(1)
                        continue

                remaining = market.seconds_until_next_window()
                
                # 2. Seamless Chaining (Pre-fetch at T-15)
                if remaining <= 15 and not self.prefetch_started:
                    self.prefetch_started = True
                    threading.Thread(target=prefetch_next, daemon=True).start()

                # 3. Instant Swap at T=0
                if remaining <= 0 or remaining > 299:
                    if next_market:
                        logger.info(f"⚡ Instant switch: {next_market['slug']}")
                        
                        # Resolve P&L from ScalpEngine
                        window_pnl = self.executor.realized_pnl if self.executor else 0.0
                        total_fills = self.executor.total_fills if self.executor else 0
                        
                        display.state.total_pnl += window_pnl
                        display.state.bankroll += window_pnl
                        stats['windows'] += 1
                        stats['pnl'] += window_pnl
                        stats['bankroll'] = display.state.bankroll
                        stats['bankroll_history'].append(display.state.bankroll)

                        # ML FEEDBACK LOOP 🧠
                        try:
                            # Calculer l'outcome : UP (1) si price > open, else DOWN (0)
                            # On utilise le dernier tick Binance pour la vérité terrain
                            final_price = self.feed.ticks[-1]['price'] if self.feed.ticks else 0
                            if final_price > 0 and self.feed.window_open_price > 0:
                                outcome = 1 if final_price > self.feed.window_open_price else 0
                                # Add sample to MLEngine
                                # Note: On utilise les dernières features capturées pendant la fenêtre
                                if hasattr(self.feed, 'last_binance_feats') and hasattr(self.feed, 'last_ob_feats'):
                                    self.feed.ml_engine.add_sample(
                                        self.feed.last_binance_feats,
                                        self.feed.last_ob_feats,
                                        outcome
                                    )
                                    config.debug_logger.info(f"ML_FEEDBACK | Sample added: Outcome={outcome} (Price: {final_price:.2f} vs Open: {self.feed.window_open_price:.2f})")
                        except Exception as feedback_err:
                            config.debug_logger.error(f"ML_FEEDBACK | Error: {feedback_err}")
                        
                        if window_pnl >= 0:
                            display.state.wins += 1
                        else:
                            display.state.losses += 1
                            
                        # Log to display
                        display.state.trade_log.append({
                            "time": datetime.now().strftime("%H:%M"),
                            "dir": "SCALP",
                            "entry": 0.0,
                            "size": total_fills,
                            "pnl": window_pnl
                        })
                        display.log(f"📊 Window PnL: {window_pnl:+.4f} USDC ({total_fills} scalp fills)")

                        # Swap objects
                        old_slug = self.current_market['slug']
                        new_slug = next_market['slug']
                        config.debug_logger.info(
                            f"WINDOW_END | "
                            f"slug={old_slug} "
                            f"actual={('UP' if final_price > self.feed.window_open_price else 'DOWN') if final_price > 0 else '?'} "
                            f"correct={ ( (final_price > self.feed.window_open_price and sig.direction == 'UP') or (final_price < self.feed.window_open_price and sig.direction == 'DOWN') ) if sig and final_price > 0 else False } "
                            f"orders_placed={ (self.executor.total_fills + len(self.executor.open_buys)) if self.executor else 0 } "
                            f"fills={total_fills} "
                            f"realized_pnl={window_pnl:+.4f} "
                            f"bankroll={display.state.bankroll:.4f}"
                        )

                        actual_dir = ('UP' if final_price > self.feed.window_open_price else 'DOWN') if final_price > 0 else '?'
                        predicted_dir = sig.direction if sig else 'WAITING'
                        is_correct = ( (final_price > self.feed.window_open_price and predicted_dir == 'UP') or (final_price < self.feed.window_open_price and predicted_dir == 'DOWN') ) if final_price > 0 else False
                        btc_delta = ((final_price/max(self.feed.window_open_price,1))-1)*100 if self.feed.window_open_price > 0 else 0

                        log_trade_event("WINDOW_END", {
                            "slug":              old_slug,
                            "actual":            actual_dir,
                            "predicted":         predicted_dir,
                            "correct":           is_correct,
                            "orders_placed":     (self.executor.total_fills + len(self.executor.open_buys)) if self.executor else 0,
                            "orders_filled":     total_fills,
                            "window_pnl":        window_pnl,
                            "bankroll":          display.state.bankroll,
                            "btc_open":          self.feed.window_open_price,
                            "btc_close":         final_price,
                            "btc_delta_pct":     btc_delta,
                        })

                        log_report(f"""
### Résultat fenêtre {old_slug[-10:]}
| Métrique | Valeur |
|---|---|
| Direction réelle | **{actual_dir}** |
| Direction prédite | {predicted_dir} {'✅' if is_correct else '❌'} |
| BTC open → close | {self.feed.window_open_price:.2f} → {final_price:.2f} ({btc_delta:+.4f}%) |
| Fills | {total_fills} |
| P&L fenêtre | **{window_pnl:+.4f} USDC** |
| Bankroll | **${display.state.bankroll:.4f}** |
""")

                        self.ob_yes.stop()
                        self.ob_no.stop()
                        if self.executor: self.executor.stop()
                        self.current_market = next_market
                        next_market = None
                        
                        tokens = json.loads(self.current_market.get("clobTokenIds", "[]"))
                        if len(tokens) >= 2:
                            self.yes_token_id = tokens[0]
                            self.no_token_id = tokens[1]
                        
                        self.ob_yes = market.LiveOrderBook(self.yes_token_id, "YES")
                        self.ob_no = market.LiveOrderBook(self.no_token_id, "NO")
                        
                        # Pass to display
                        display.state.ob_yes = self.ob_yes
                        display.state.ob_no  = self.ob_no
                        
                        self.ob_yes.start()
                        self.ob_no.start()
                        self.executor = execution.SmartLimitEngine(self.ob_yes, self.ob_no, lambda: self.last_sig, lambda: market.seconds_until_next_window(), bot=self)
                        self.executor.start()
                        self.feed.set_window_open(0)
                        self.window_start = market.get_current_window_ts()
                        self.trade_placed = False
                        self.prefetch_started = False
                    
                # 4. Data & Signals
                with self.feed.lock:
                    price = self.feed.ticks[-1]['price'] if self.feed.ticks else (self.feed.window_open_price or 70000.0)
                    ticks, candles = list(self.feed.ticks), list(self.feed.candles_1m)
                
                if self.feed.window_open_price == 0 and price > 0:
                    self.feed.set_window_open(price)

                # Composite OB analytics
                yes_mid = self.ob_yes.mid if self.ob_yes else 0.5
                no_mid = self.ob_no.mid if self.ob_no else 0.5
                implied_sum = yes_mid + no_mid
                
                # Use YES book for primary signal
                weights = self.tracker.weights
                
                debug_logger.debug(
                    f"CYCLE | "
                    f"T={remaining:.1f}s "
                    f"btc={price:.2f} "
                    f"open={self.feed.window_open_price:.2f} "
                    f"delta={((price/max(self.feed.window_open_price,1))-1)*100:+.4f}% "
                    f"yes_bid={self.ob_yes.best_bid if self.ob_yes else 0:.4f} "
                    f"yes_ask={self.ob_yes.best_ask if self.ob_yes else 0:.4f} "
                    f"yes_mid={yes_mid:.4f} "
                    f"no_mid={no_mid:.4f} "
                    f"sum_mid={implied_sum:.4f} "
                    f"yes_upd={self.ob_yes.update_count if self.ob_yes else 0} "
                    f"no_upd={self.ob_no.update_count if self.ob_no else 0} "
                    f"ticks={len(ticks)} "
                    f"candles={len(candles)}"
                )

                self.last_sig = self.feed.analyze(ticks, candles, self.ob_yes, self.feed.window_open_price, weights) if self.ob_yes else None
                sig = self.last_sig

                if sig:
                    self.signal_history.append({
                        "direction":  sig.direction,
                        "confidence": sig.confidence,
                        "ts":         time.time(),
                    })
                    
                    debug_logger.debug(
                        f"SIGNAL | "
                        f"dir={sig.direction} "
                        f"conf={sig.confidence:.4f} "
                        f"score={sig.total_score:.4f} "
                        f"rsi={sig.breakdown.get('rsi',0):.3f} "
                        f"ema={sig.breakdown.get('ema_cross',0):.3f} "
                        f"momentum={sig.breakdown.get('momentum',0):.3f} "
                        f"window_delta={sig.breakdown.get('window_delta',0):.3f} "
                        f"ob_imb={sig.breakdown.get('ob_imbalance',0):.3f} "
                        f"ml={sig.breakdown.get('ml_score',0):.3f} "
                        f"reason='{sig.reasoning}'"
                    )
                else:
                    time.sleep(0.05)
                    continue
                
                # 5. Tiered Execution
                elapsed = time.time() - self.window_start
                if not self.trade_placed and sig:
                    if sig.direction in ("SKIP", "WAITING", None):
                        pass
                    elif sig.confidence >= config.CONFIDENCE_TIERS["primary"]:
                        trade_size = size_position(stats['bankroll'], sig.confidence, 1.0)
                        fictitious_trade = {'dir': sig.direction, 'price': price, 'size': trade_size, 'token': self.yes_token_id if sig.direction == "UP" else self.no_token_id}
                        stats['trades'] += 1
                        self.trade_placed = True
                        display.log(f"🎯 DRY RUN: {sig.direction} (${trade_size}) at ${price}")
                    elif elapsed >= 240 and sig.confidence >= config.CONFIDENCE_TIERS["secondary"] and self._direction_stable_for(60):
                        trade_size = size_position(stats['bankroll'], sig.confidence, 0.6)
                        fictitious_trade = {'dir': sig.direction, 'price': price, 'size': trade_size, 'token': self.yes_token_id if sig.direction == "UP" else self.no_token_id}
                        stats['trades'] += 1
                        self.trade_placed = True
                        display.log("⚡ Secondary entry at T+240s")
                        display.log(f"🎯 DRY RUN: {sig.direction} (${trade_size}) at ${price}")
                    elif elapsed >= 270 and sig.confidence >= 0.50:
                        trade_size = size_position(stats['bankroll'], sig.confidence, 0.35)
                        fictitious_trade = {'dir': sig.direction, 'price': price, 'size': trade_size, 'token': self.current_market['yes_token'] if sig.direction == "UP" else self.current_market['no_token']}
                        stats['trades'] += 1
                        self.trade_placed = True
                        display.log("🚨 Forced entry T+270s")
                        display.log(f"🎯 DRY RUN: {sig.direction} (${trade_size}) at ${price}")
                    elif elapsed >= 270:
                        display.log(f"⏭ Window SKIPPED at deadline — confidence {sig.confidence:.0f}% < 50% minimum")
                        self.trade_placed = True

                # 6. Update Display State (Thread-safe)
                now = time.time()
                dt = max(now - getattr(self, '_last_update_time', now - 0.05), 0.001)
                # 5. Dashboard Update
                display.state.btc_price = price
                display.state.btc_prev_price = self.feed.ticks[-2]['price'] if len(self.feed.ticks) > 1 else price
                display.state.window_open = self.feed.window_open_price
                display.state.window_delta_pct = ((price / max(self.feed.window_open_price, 1)) - 1) * 100
                display.state.time_remaining = remaining
                display.state.yes_mid = yes_mid
                display.state.no_mid = no_mid
                display.state.implied_sum = implied_sum
                
                if self.ob_yes:
                    display.state.yes_best_bid = self.ob_yes.best_bid
                    display.state.yes_best_ask = self.ob_yes.best_ask
                    display.state.yes_updates_sec = self.ob_yes.update_count
                if self.ob_no:
                    display.state.no_best_bid = self.ob_no.best_bid
                    display.state.no_best_ask = self.ob_no.best_ask
                    display.state.no_updates_sec = self.ob_no.update_count
                    
                display.state.direction = sig.direction if sig else "WAITING"
                display.state.confidence = sig.confidence if sig else 0.0
                display.state.total_score = sig.total_score if sig else 0.0
                display.state.signal_breakdown = sig.breakdown if sig else {}
                
                if self.executor:
                    display.state.fills_this_window = self.executor.total_fills
                    if self.executor.client and isinstance(self.executor.client, execution.PaperOrderBook):
                        if self.executor.client.fill_log:
                            last = self.executor.client.fill_log[-1]
                            display.state.last_fill_desc = f"{last['side']} @{last['price']:.3f}"
                
                # 6. Safety & Health Check
                self._last_update_time = now
                
                # Fetch OB data OUTSIDE state lock to prevent deadlocks
                y_bids = self.ob_yes.get_top_bids(5) if self.ob_yes else []
                y_asks = self.ob_yes.get_top_asks(5) if self.ob_yes else []
                y_imb  = self.ob_yes.book_imbalance if self.ob_yes else 0.5
                n_bids = self.ob_no.get_top_bids(5) if self.ob_no else []
                n_asks = self.ob_no.get_top_asks(5) if self.ob_no else []
                n_imb  = self.ob_no.book_imbalance if self.ob_no else 0.5

                with display.state._lock:
                    # Compute update rates
                    yes_count = self.ob_yes.update_count if self.ob_yes else 0
                    no_count = self.ob_no.update_count if self.ob_no else 0
                    prev_yes = getattr(self, '_prev_yes_count', yes_count)
                    prev_no = getattr(self, '_prev_no_count', no_count)
                    
                    display.state.yes_updates_sec = int((yes_count - prev_yes) / dt)
                    display.state.no_updates_sec = int((no_count - prev_no) / dt)
                    
                    self._prev_yes_count = yes_count
                    self._prev_no_count = no_count

                    display.state.yes_mid = yes_mid
                    display.state.yes_bids = y_bids
                    display.state.yes_asks = y_asks
                    display.state.yes_imbalance = y_imb
                    
                    display.state.no_mid = no_mid
                    display.state.no_bids = n_bids
                    display.state.no_asks = n_asks
                    display.state.no_imbalance = n_imb
                    
                    display.state.implied_sum = implied_sum
                    display.state.arb_gap_cents = (1.0 - implied_sum) * 100 if implied_sum < 1 else 0
                    
                    display.state.tick_count = len(ticks)
                    display.state.candle_count = len(candles)
                    display.state.binance_ws_ok = len(ticks) > 0
                    display.state.yes_ws_ok = self.ob_yes.connected if self.ob_yes else False
                    display.state.no_ws_ok = self.ob_no.connected if self.ob_no else False
                    
                    display.state.windows_done = stats['windows']
                    display.state.wins = stats.get('wins', 0)
                    display.state.losses = stats.get('losses', 0)
                    display.state.total_pnl = stats['pnl']
                    display.state.bankroll = stats['bankroll']

                time.sleep(0.05)
                now = time.time()
                if now - self._last_health_check >= 10.0:
                    self._health_check()
                    debug_logger.info(
                        f"HEALTH | "
                        f"btc={price:.2f} "
                        f"open={self.feed.window_open_price:.2f} "
                        f"delta={((price/max(self.feed.window_open_price,1))-1)*100:+.4f}% "
                        f"yes_bid={self.ob_yes.best_bid if self.ob_yes else 0.0:.4f} "
                        f"yes_ask={self.ob_yes.best_ask if self.ob_yes else 0.0:.4f} "
                        f"yes_mid={yes_mid:.4f} "
                        f"no_mid={no_mid:.4f} "
                        f"sum_mid={implied_sum:.4f} "
                        f"yes_upd={self.ob_yes.update_count if self.ob_yes else 0} "
                        f"no_upd={self.ob_no.update_count if self.ob_no else 0} "
                        f"ticks={len(ticks)} "
                        f"candles={len(candles)} "
                        f"conf={sig.confidence if sig else 0:.4f} "
                        f"dir={sig.direction if sig else '?' } "
                        f"bankroll={display.state.bankroll:.4f} "
                        f"executor={self.executor._running if self.executor else False}"
                    )
                    self._last_health_check = now

    def stop(self, *args):
        self.running = False
        duration_min = (datetime.now() - session_start).total_seconds() / 60
        windows_total = stats['windows']
        final_bankroll = display.state.bankroll
        total_pnl = final_bankroll - config.INITIAL_BANKROLL
        roi = (total_pnl / config.INITIAL_BANKROLL) * 100
        wins = display.state.wins
        losses = display.state.losses
        fills_total = stats.get('trades', 0) # approximation if trades is count of fills
        win_rate = wins / max(wins + losses, 1) * 100

        log_report(f"""
---
## 📊 RAPPORT FINAL SESSION {config.SESSION_ID}

### Résultats globaux
| Métrique | Valeur |
|---|---|
| Durée session | {duration_min:.1f} minutes |
| Fenêtres tradées | {windows_total} |
| Win rate | **{win_rate:.1f}%** |
| P&L total | **{total_pnl:+.4f} USDC** |
| ROI | **{roi:+.2f}%** |
| Bankroll finale | **${final_bankroll:.4f}** |
| Fees sauvés (maker) | ${display.state.fees_saved:.4f} |

### Breakdown par mode d'entrée
| Mode | Trades |
|---|---|
| A — Maker+Hold | {display.state.trades_mode_a} |
| B — Taker extrême | {display.state.trades_mode_b} |
| C — Maker scalp | {display.state.trades_mode_c} |

*Fichiers générés :*
- `logs/debug_{config.SESSION_ID}.log` — logs complets
- `logs/trades_{config.SESSION_ID}.jsonl` — trades JSON
- `logs/report_{config.SESSION_ID}.md` — ce rapport
""")

        debug_logger.info(
            f"SESSION_END | "
            f"duration={duration_min:.1f}min "
            f"windows={windows_total} "
            f"wins={wins} "
            f"win_rate={win_rate:.1f}% "
            f"pnl={total_pnl:+.4f} "
            f"bankroll={final_bankroll:.4f}"
        )

        if self.ob_yes: self.ob_yes.stop()
        if self.ob_no: self.ob_no.stop()
        if self.executor: self.executor.stop()
        sys.exit(0)

if __name__ == "__main__":
    if not run_startup_checks():
        print("⛔ Checks échoués. Corrige et relance.")
        sys.exit(1)
    
    pre_live_warning()
    
    import sys
    dry_run = "--live" not in sys.argv
    mode = "safe"
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        mode = sys.argv[idx+1]

    bot = SniperBot(mode=mode)
    
    # Graceful shutdown mapping
    signal.signal(signal.SIGINT,  bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)

    try:
        with display.start_dashboard():
            bot.run()
    except KeyboardInterrupt:
        bot.stop()
    finally:
        display.stop_dashboard()
