from config import MAX_POSITION_PCT, MAX_DRAWDOWN_PCT, KELLY_FRACTION
from utils.logger import get_logger

logger = get_logger("RiskManager")

class RiskManager:
    def __init__(self, initial_capital: float = 1000.0):
        self.initial_capital = initial_capital
        # Split capital intuitively or equally, here 50/50
        self.arb_capital = initial_capital * 0.5
        self.corr_capital = initial_capital * 0.5
        
        self.peak_capital = initial_capital
        self.current_capital = initial_capital
        
        self.kill_switch_active = False

    def check_drawdown(self):
        """
        Check if global drawdown exceeds MAX_DRAWDOWN_PCT.
        """
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
            
        drawdown = (self.peak_capital - self.current_capital) / self.peak_capital
        if drawdown > MAX_DRAWDOWN_PCT:
            logger.critical(f"MAX DRAWDOWN REACHED! {drawdown*100:.2f}% > {MAX_DRAWDOWN_PCT*100:.2f}%. KILL SWITCH ACTIVATED.")
            self.kill_switch_active = True
            
        return self.kill_switch_active

    def calculate_kelly_size(self, strategy: str, prob_win: float, edge: float, price: float) -> float:
        """
        Calculate Half-Kelly position size.
        For binary options paying 1 at cost 'price':
        f* = (prob_win - price) / (1 - price) = edge / (1 - price)
        """
        if edge <= 0 or price >= 1.0 or self.kill_switch_active:
            return 0.0
            
        f_star = edge / (1.0 - price)
        # Apply Half-Kelly
        f_applied = f_star * KELLY_FRACTION
        
        # Hard cap
        f_capped = min(f_applied, MAX_POSITION_PCT)
        
        bucket_capital = self.arb_capital if strategy == "ARBITRAGE" else self.corr_capital
        position_sizeUSD = bucket_capital * f_capped
        
        return position_sizeUSD

    def update_pnl(self, strategy: str, realized_pnl: float):
        """
        Update real-time P&L.
        """
        if strategy == "ARBITRAGE":
            self.arb_capital += realized_pnl
        else:
            self.corr_capital += realized_pnl
            
        self.current_capital = self.arb_capital + self.corr_capital
        self.check_drawdown()
        logger.info(f"P&L Update | {strategy}: {realized_pnl:+.2f} | Total Capital: {self.current_capital:.2f}")

