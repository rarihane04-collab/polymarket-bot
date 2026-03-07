from config import TOTAL_FRICTION, ARB_MIN_EDGE
from utils.logger import get_logger

logger = get_logger("ArbitrageStrategy")

class ArbitrageStrategy:
    def __init__(self):
        pass

    def evaluate(self, poly_feed, token_configs) -> list:
        """
        Scanner en continu : si ask_YES + ask_NO < (1 - TOTAL_FRICTION) -> arb détecté.
        """
        signals = []
        
        for asset, config in token_configs.items():
            tokens = config.get("tokens", {})
            yes_id = tokens.get("YES")
            no_id = tokens.get("NO")
            
            if not yes_id or not no_id:
                continue
                
            yes_ob = poly_feed.orderbooks.get(yes_id)
            no_ob = poly_feed.orderbooks.get(no_id)
            
            if not yes_ob or not no_ob:
                continue
                
            ask_yes = yes_ob.best_ask
            ask_no = no_ob.best_ask
            
            if ask_yes >= 1.0 or ask_no >= 1.0:
                continue
                
            # Liquidity check (Minimum $10 available at best ask to consider an arb valid)
            # This protects against ghost orders or dust that would fail due to fixed gas/fees
            yes_size = yes_ob.asks.get(ask_yes, 0.0)
            no_size = no_ob.asks.get(ask_no, 0.0)
            
            if yes_size < 10.0 or no_size < 10.0:
                continue
                
            total_cost = ask_yes + ask_no
            gross_edge = 1.0 - total_cost
            net_edge = gross_edge - TOTAL_FRICTION
            
            if net_edge >= ARB_MIN_EDGE:
                signals.append({
                    "strategy": "ARBITRAGE",
                    "asset": asset,
                    "yes_token": yes_id,
                    "no_token": no_id,
                    "ask_yes": ask_yes,
                    "ask_no": ask_no,
                    "net_edge": net_edge
                })
                
        return signals
