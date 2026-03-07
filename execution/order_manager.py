import asyncio
from config import PARTIAL_FILL_WAIT, ENV, TOTAL_FRICTION
from utils.logger import get_logger

logger = get_logger("OrderManager")

class OrderManager:
    def __init__(self, risk_manager):
        self.risk = risk_manager
        self.open_orders = []
        self.positions = {}  # token_id: total_usd_invested

    async def place_order(self, token_id: str, side: str, price: float, size: float):
        """
        Mock placement to Polymarket CLOB.
        In paper mode, simulates full execution implicitly with some slippage.
        """
        if ENV == "paper":
            logger.info(f"[PAPER EXEC] Placed {side} for {token_id} at {price:.3f} | Size: ${size:.2f}")
            self.positions[token_id] = self.positions.get(token_id, 0.0) + size
            await asyncio.sleep(0.1)
            return True
        else:
            logger.warning("Live execution not fully implemented.")
            return False

    async def execute_correlation_signal(self, signal: dict):
        if self.risk.kill_switch_active:
            return
            
        prob_win = signal["prob_win"]
        edge = signal["edge"]
        target_price = signal["target_price"]
        token_id = signal["token_id"]
        
        target_size = self.risk.calculate_kelly_size("CORRELATION", prob_win, edge, target_price)
        current_invested = self.positions.get(token_id, 0.0)
        
        size_to_buy = target_size - current_invested
        
        if size_to_buy <= 1.0: # Minimum order size roughly $1
            return
            
        logger.info(f"Executing CORRELATION on {signal['asset']} {signal['direction']} | Edge: {edge:.3f} | Target Size: ${target_size:.2f} | Buying: ${size_to_buy:.2f}")
        success = await self.place_order(token_id, "BUY", target_price, size_to_buy)
        
        if success:
            logger.info(f"[HEDGE] Delta-Neutral hedge requested for {signal['asset']} on Binance Futures.")

    async def execute_arbitrage_signal(self, signal: dict):
        if self.risk.kill_switch_active:
            return
            
        net_edge = signal["net_edge"]
        yes_token = signal["yes_token"]
        no_token = signal["no_token"]
        
        # Max out arb capacity
        target_size = self.risk.arb_capital * self.risk.MAX_POSITION_PCT  # Assuming MAX_POSITION_PCT is in risk config
        current_invested = max(self.positions.get(yes_token, 0.0), self.positions.get(no_token, 0.0))
        
        size_to_buy = target_size - current_invested
        if size_to_buy <= 1.0:
            return
            
        logger.info(f"Executing ARBITRAGE on {signal['asset']} | Net Edge: {net_edge:.4f} | Size per leg: ${size_to_buy:.2f}")
        
        yes_task = asyncio.create_task(self.place_order(yes_token, "BUY", signal["ask_yes"], size_to_buy))
        no_task = asyncio.create_task(self.place_order(no_token, "BUY", signal["ask_no"], size_to_buy))
        
        results = await asyncio.gather(yes_task, no_task, return_exceptions=True)
        
        yes_filled = results[0] is True
        no_filled = results[1] is True
        
        if yes_filled and no_filled:
            logger.info(f"Arbitrage successfully fully filled on {signal['asset']}! Locked profit.")
            self.risk.update_pnl("ARBITRAGE", size_to_buy * net_edge)
        elif yes_filled or no_filled:
            filled_side = "YES" if yes_filled else "NO"
            unfilled_side = "NO" if yes_filled else "YES"
            filled_price = signal["ask_yes"] if yes_filled else signal["ask_no"]
            
            logger.warning(f"PARTIAL FILL! {filled_side} filled, {unfilled_side} failed. Entering partial fill handler.")
            
            breakeven_price = 1.0 - filled_price - TOTAL_FRICTION
            logger.info(f"Placing LIMIT {unfilled_side} at breakeven {breakeven_price:.3f} waiting {PARTIAL_FILL_WAIT}s...")
            await asyncio.sleep(PARTIAL_FILL_WAIT)
            
            logger.info(f"Wait over. Liquidating {filled_side} position to MARKET to close partial leg.")
            loss = size_to_buy * TOTAL_FRICTION
            self.risk.update_pnl("ARBITRAGE", -loss)

