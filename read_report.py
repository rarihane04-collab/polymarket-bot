import json
import os
import glob
import sys
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich import box

def get_latest_log():
    logs = glob.glob("logs/trades_*.jsonl")
    if not logs:
        return None
    return max(logs, key=os.path.getctime)

def parse_logs(file_path):
    events = []
    with open(file_path, 'r') as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except:
                continue
    return events

def summarize(events):
    summary = {
        "start_time": None,
        "end_time": None,
        "windows": 0,
        "orders": 0,
        "fills": 0,
        "total_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "bankroll": 0.0,
        "latency_avg": 0.0,
        "modes": {"A": 0, "B": 0, "C": 0}
    }
    
    latencies = []
    
    for e in events:
        if not summary["start_time"]:
            summary["start_time"] = e["ts"]
        summary["end_time"] = e["ts"]
        
        etype = e["event"]
        
        if etype == "ORDER_PLACED":
            summary["orders"] += 1
        elif etype == "ORDER_FILLED":
            summary["fills"] += 1
            latencies.append(e.get("latency_ms", 0))
        elif etype == "WINDOW_END":
            summary["windows"] += 1
            pnl = e.get("window_pnl", 0)
            summary["total_pnl"] += pnl
            summary["bankroll"] = e.get("bankroll", summary["bankroll"])
            if pnl > 0: summary["wins"] += 1
            else: summary["losses"] += 1
            
    if latencies:
        summary["latency_avg"] = sum(latencies) / len(latencies)
        
    return summary

def display_report(file_path, summary):
    console = Console()
    
    # Header
    console.print(Panel(
        f"[bold cyan]📊 PERFORMANCE REPORT[/bold cyan]\n"
        f"File: [dim]{file_path}[/]\n"
        f"Session: [yellow]{os.path.basename(file_path).replace('trades_', '').replace('.jsonl', '')}[/]",
        box=box.DOUBLE
    ))

    # Key Stats
    stats = []
    stats.append(Panel(f"[bold green]PNL TOTAL[/bold green]\n[cyan]{summary['total_pnl']:+.4f} USDC[/]", expand=True))
    stats.append(Panel(f"[bold white]WIN RATE[/bold white]\n[yellow]{(summary['wins']/max(summary['wins']+summary['losses'],1))*100:.1f}%[/]", expand=True))
    stats.append(Panel(f"[bold blue]BANKROLL[/bold blue]\n[white]${summary['bankroll']:.2f}[/]", expand=True))
    console.print(Columns(stats))

    # Details Table
    table = Table(title="Execution Metrics", box=box.ROUNDED)
    table.add_column("Métrique", style="dim")
    table.add_column("Valeur", justify="right")
    
    table.add_row("Windows tradées", str(summary["windows"]))
    table.add_row("Total Ordres", str(summary["orders"]))
    table.add_row("Total Fills", str(summary["fills"]))
    table.add_row("Latence Moyenne", f"{summary['latency_avg']:.1f}ms")
    table.add_row("Fills/Order Ratio", f"{(summary['fills']/max(summary['orders'],1))*100:.1f}%")
    
    console.print(table)

    # Time info
    start = datetime.fromisoformat(summary["start_time"])
    end = datetime.fromisoformat(summary["end_time"])
    duration = (end - start).total_seconds() / 60
    console.print(f"\n[dim]⏱ Session duration: {duration:.1f} min[/]")

if __name__ == "__main__":
    path = get_latest_log()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        
    if not path or not os.path.exists(path):
        print("❌ No trade log found in logs/")
        sys.exit(1)
        
    events = parse_logs(path)
    summary = summarize(events)
    display_report(path, summary)
