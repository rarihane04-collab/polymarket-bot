import threading
import time
import traceback
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.progress import ProgressBar

from btc_sniper import config

# --- COMPREHENSIVE DATACLASS ---
@dataclass
class BotState:
    # 1. Market Info
    market_slug: str = ""
    market_question: str = ""
    mode: str = "DRY-RUN"
    
    # 2. Prices & OB Metrics
    btc_price: float = 0.0
    btc_prev_price: float = 0.0
    window_open: float = 0.0
    window_delta_pct: float = 0.0
    time_remaining: float = 0.0
    
    yes_mid: float = 0.5
    no_mid: float = 0.5
    implied_sum: float = 1.0
    arb_gap_cents: float = 0.0
    
    yes_best_bid: float = 0.0
    yes_best_ask: float = 0.0
    no_best_bid: float = 0.0
    no_best_ask: float = 0.0
    
    yes_updates_sec: int = 0
    no_updates_sec: int = 0
    
    yes_imbalance: float = 0.0
    no_imbalance: float = 0.0
    
    # 3. Signals
    direction: str = "WAITING"
    confidence: float = 0.0
    total_score: float = 0.0
    signals: dict = field(default_factory=dict)
    signal_breakdown: dict = field(default_factory=dict)
    
    # 4. Trading / Orders
    open_yes: int = 0
    open_no: int = 0
    fills_window: int = 0
    fills_this_window: int = 0
    last_fill: str = "—"
    last_fill_desc: str = "—"
    trade_log: list = field(default_factory=list)
    
    # 5. Stats
    start_time: str = ""
    windows: int = 0
    windows_done: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 100.0
    bankroll_history: list = field(default_factory=list)
    fees_paid: float = 0.0
    fees_saved: float = 0.0
    
    # 6. Legacy / Compatibility Counters
    trades_mode_a: int = 0
    trades_mode_b: int = 0
    trades_mode_c: int = 0
    
    # 7. System Health
    tick_count: int = 0
    candle_count: int = 0
    binance_ws_ok: bool = False
    yes_ws_ok: bool = False
    no_ws_ok: bool = False
    
    open_orders_yes: list = field(default_factory=list)
    open_orders_no: list = field(default_factory=list)
    
    # 8. Shared Objects (Order Books)
    ob_yes: object = None
    ob_no: object = None
    
    # 9. OB Depth (Top 5)
    yes_bids: list = field(default_factory=list)
    yes_asks: list = field(default_factory=list)
    no_bids: list = field(default_factory=list)
    no_asks: list = field(default_factory=list)
    
    # 10. Logs
    log_lines: list = field(default_factory=list)
    
    # 🔑 THE CRITICAL LOCKS
    _lock: threading.Lock = field(default_factory=threading.Lock)

# Global State Singleton
state = BotState(start_time=datetime.now().strftime("%H:%M:%S"))
_log_lock = threading.Lock() # Dedicated lock to prevent circular deadlocks

def log(message: str):
    """Thread-safe logging with dedicated lock."""
    ts = datetime.now().strftime("%H:%M:%S")
    msg = f"[{ts}] {message}"
    with _log_lock:
        if not state.log_lines or state.log_lines[-1] != msg:
            state.log_lines.append(msg)
            if len(state.log_lines) > 50:
                state.log_lines.pop(0)

def _safe_float(v):
    try: return float(v or 0.0)
    except: return 0.0

def _get_ob_data(ob):
    if not ob: return None
    # Use acquire with timeout to avoid hanging the dashboard
    if not hasattr(ob, "_lock") or not ob._lock.acquire(timeout=0.05):
        return None
    try:
        bids_dict = dict(ob.bids)
        asks_dict = dict(ob.asks)
        
        mid_val = getattr(ob, "mid", 0.5)
        
        b_sorted = sorted(bids_dict.items(), key=lambda x: x[0], reverse=True)[:5]
        a_sorted = sorted(asks_dict.items(), key=lambda x: x[0], reverse=False)[:5]
        
        return {
            "bids": b_sorted,
            "asks": a_sorted,
            "mid":  _safe_float(mid_val),
        }
    except Exception as e:
        return None
    finally:
        ob._lock.release()

# --- PANEL BUILDERS ---

def _render_market():
    # Attempt lock with timeout
    if not state._lock.acquire(timeout=0.1):
        return Panel("LOCK CONTENTION", title="1. MARCHÉ")
    try:
        slug = state.market_slug
        q = state.market_question
        price = _safe_float(state.btc_price)
        prev = _safe_float(state.btc_prev_price or price)
        mode = str(state.mode).upper()
        rem = _safe_float(state.time_remaining)
        
        diff = price - prev
        pct = (diff / prev * 100) if prev != 0 else 0.0
        color = "green" if diff >= 0 else "red"
        arrow = "▲" if diff >= 0 else "▼"
        
        content = Text.assemble(
            (f"{q}\n", "bold cyan"),
            (f"Slug: {slug}\n", "dim"),
            (f"T-Minus: {int(rem)}s\n\n", "bold yellow"),
            ("Binance BTC: ", "white"),
            (f"{arrow} ${price:,.2f}", f"bold {color}"),
            (f" ({pct:+.4f}%)", f"dim {color}"),
            ("\nMODE: ", "white"), (mode or "DRY-RUN", "bold reverse yellow")
        )
        return Panel(content, title="[bold white]1. MARCHÉ[/]", border_style="cyan", box=box.ROUNDED)
    finally:
        state._lock.release()

def _render_ob():
    with state._lock:
        y_ob, n_ob = state.ob_yes, state.ob_no
        gap = state.arb_gap_cents
        y_ok, n_ok = state.yes_ws_ok, state.no_ws_ok

    y_data = _get_ob_data(y_ob)
    n_data = _get_ob_data(n_ob)
    
    # Fallback to state copies if direct OB access timed out
    if not y_data:
        with state._lock:
            y_data = {"bids": list(state.yes_bids), "asks": list(state.yes_asks), "mid": state.yes_mid}
    if not n_data:
        with state._lock:
            n_data = {"bids": list(state.no_bids), "asks": list(state.no_asks), "mid": state.no_mid}

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column("YES", ratio=1)
    grid.add_column("NO", ratio=1)

    def side_table(data, color, ok):
        if not ok: return Text("OFFLINE", style="bold red")
        if not data or (not data["bids"] and not data["asks"]): return Text("WAITING...", style="dim")
        t = Table(box=None, show_header=False, padding=0, expand=True)
        for p, s in reversed(data["asks"][:5]):
            t.add_row(f"[red]{p:.4f}[/]", f"[dim]${p*s:.0f}[/]")
        t.add_row(f"[bold {color}]━ {data['mid']:.4f} ━[/]", "")
        for p, s in data["bids"][:5]:
            t.add_row(f"[green]{p:.4f}[/]", f"[dim]${p*s:.0f}[/]")
        return t

    grid.add_row(side_table(y_data, "green", y_ok), 
                 side_table(n_data, "red", n_ok))
    
    arb_c = "green" if gap > 1 else "white"
    footer = Text(f"\nARB GAP: {gap:+.2f}c", style=f"bold {arb_c}")
    from rich.console import Group
    return Panel(Group(grid, footer), title="[bold white]2. ORDER BOOK[/]", border_style="blue", box=box.ROUNDED)

def _render_signals():
    # Lock briefly to copy signals
    if not state._lock.acquire(timeout=0.1): return Panel("LOCK...")
    try:
        sigs = dict(state.signal_breakdown or state.signals or {})
        score = _safe_float(state.total_score)
        direction = str(state.direction or "—")
        conf = _safe_float(state.confidence)
    finally:
        state._lock.release()

    grid = Table.grid(padding=(0,1))
    grid.add_column("Signal", width=15)
    grid.add_column("Val", width=7, justify="right")
    grid.add_column("Bar", width=12)

    for k, v in sigs.items():
        val = _safe_float(v)
        c = "green" if val > 0 else "red" if val < 0 else "white"
        pos = int((max(-1.0, min(1.0, val)) + 1) * 5)
        bar = f"[{c}]{'█' * pos}[/][dim]{'░' * (10-pos)}[/dim]"
        grid.add_row(k, f"[{c}]{val:+.2f}[/]", bar)

    sig_c = "green" if direction == "UP" else "red" if direction == "DOWN" else "yellow"
    footer = Text.assemble(
        ("\nSCORE: ", "bold"), (f"{score:+.3f} ", f"bold {sig_c}"),
        (f"[{direction}] ", f"bold reverse {sig_c}"),
        (f" {conf*100:.1f}% confidence", "dim")
    )
    from rich.console import Group
    return Panel(Group(grid, footer), title="[bold white]3. SIGNALS[/]", border_style="magenta", box=box.ROUNDED)

def _render_paper():
    with state._lock:
        y, n = state.open_yes, state.open_no
        f = state.fills_this_window or state.fills_window
        last = state.last_fill_desc or state.last_fill
        logs = list(state.trade_log or [])[-8:]

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow", expand=True, padding=0)
    t.add_column("Heure", width=8)
    t.add_column("Dir", width=4)
    t.add_column("Prix", width=7)
    t.add_column("P&L", width=6, justify="right")

    for entry in logs:
        pnl = _safe_float(entry.get("pnl", 0.0))
        c = "green" if pnl > 0 else "red" if pnl < 0 else "white"
        t.add_row(str(entry.get("time","")), str(entry.get("dir","?")), f"{entry.get('price',0):.3f}", f"[{c}]{pnl:+.2f}[/]")

    header = f"Open: [green]Y:{y}[/] [red]N:{n}[/] | Fills: {f}\nLast: [dim]{last}[/]"
    from rich.console import Group
    return Panel(Group(Text.from_markup(header), t), title="[bold white]4. ORDRES PAPER[/]", border_style="yellow", box=box.ROUNDED)

def _render_pnl():
    with state._lock:
        start = state.start_time
        wins, loss = state.wins, state.losses
        pnl = _safe_float(state.total_pnl)
        bank = _safe_float(state.bankroll)
        fees = _safe_float(state.fees_paid)
        saved = _safe_float(state.fees_saved)
        windows = state.windows_done or state.windows

    wr = (wins / max(1, wins + loss)) * 100
    p_c = "green" if pnl >= 0 else "red"
    
    g = Table.grid(padding=(0, 1))
    g.add_row("Started:", str(start))
    g.add_row("Windows:", str(windows))
    g.add_row("Win Rate:", f"{wr:.1f}% ({wins}W/{loss}L)")
    g.add_row("Net P&L:", f"[{p_c} bold]{pnl:+.3f} USDC[/]")
    g.add_row("Fees Saved:", f"[green]{saved:,.2f}[/]")
    g.add_row("Bankroll:", f"[bold green]${bank:,.2f}[/]")

    return Panel(g, title="[bold white]5. P&L & STATS[/]", border_style="green", box=box.ROUNDED)

def _render_logs():
    with _log_lock:
        lines = list(state.log_lines or [])[-15:]
    
    with state._lock:
        b_ws = state.binance_ws_ok
        y_ws = state.yes_ws_ok
        n_ws = state.no_ws_ok
        y_rate = state.yes_updates_sec
        n_rate = state.no_updates_sec

    status = Text.assemble(
        ("BNC-WS: ", "dim"), ("OK " if b_ws else "ERR ", "green" if b_ws else "red"),
        ("YES-WS: ", "dim"), (f"{y_rate}u/s " if y_ws else "ERR ", "green" if y_ws else "red"),
        ("NO-WS: ", "dim"), (f"{n_rate}u/s" if n_ws else "ERR", "green" if n_ws else "red")
    )
    from rich.console import Group
    return Panel(Group(Text.from_markup("\n".join(lines) or "[dim]Waiting for logs...[/]"), Text("\n"), status), 
                 title="[bold white]6. LOG & HEALTH[/]", border_style="white", box=box.ROUNDED)

# --- DASHBOARD ENGINE ---

class Dashboard:
    def __init__(self):
        self._running = False
        self._thread = None
        self._live = None
        self._layout = self._build_layout()
        # Direct Layout references
        self._p1 = self._layout["p1"]
        self._p2 = self._layout["p2"]
        self._p3 = self._layout["p3"]
        self._p4 = self._layout["p4"]
        self._p5 = self._layout["p5"]
        self._p6 = self._layout["p6"]

    def _build_layout(self) -> Layout:
        l = Layout()
        l.split_column(Layout(name="r1"), Layout(name="r2"), Layout(name="r3"))
        l["r1"].split_row(Layout(name="p1"), Layout(name="p2"))
        l["r2"].split_row(Layout(name="p3"), Layout(name="p4"))
        l["r3"].split_row(Layout(name="p5"), Layout(name="p6"))
        return l

    def _refresh_cycle(self):
        tasks = [
            (self._p1, _render_market), (self._p2, _render_ob),
            (self._p3, _render_signals), (self._p4, _render_paper),
            (self._p5, _render_pnl), (self._p6, _render_logs)
        ]
        for region, func in tasks:
            try: region.update(func())
            except Exception as e: region.update(Panel(f"ERR: {e}"))

    def _run_loop(self):
        retries = 0
        log("🖥️ Dashboard engine starting")
        while self._running and retries < 5:
            try:
                # screen=False is essential for many constrained environments
                with Live(self._layout, auto_refresh=False, screen=False) as live:
                    self._live = live
                    while self._running:
                        self._refresh_cycle()
                        live.refresh()
                        time.sleep(0.125) # 8 FPS is plenty
            except Exception as e:
                log(f"❌ Dashboard error: {e}")
                retries += 1
                if self._running: time.sleep(min(3 ** retries, 20))

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, name="DashThread", daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False

_global_mgr = None
class DashboardContext:
    def __init__(self): self._dash = Dashboard()
    def __enter__(self): self._dash.start(); return self._dash
    def __exit__(self, *a): self._dash.stop()

def start_dashboard():
    global _global_mgr
    _global_mgr = DashboardContext()
    return _global_mgr

def stop_dashboard():
    global _global_mgr
    if _global_mgr:
        _global_mgr.__exit__(None, None, None)
        _global_mgr = None
