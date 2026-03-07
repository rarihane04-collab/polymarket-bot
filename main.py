import asyncio
import json
import os
import signal
from collections import defaultdict

from config import ENV, BETA_WINDOW
from data.poly_feed import PolyFeed
from data.deribit_feed import DeribitFeed
from data.price_feed import PriceFeed
from strategies.correlation import CorrelationStrategy
from strategies.arb import ArbitrageStrategy
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from utils.logger import get_logger

logger = get_logger("Main")

class PolymarketBot:
    def __init__(self):
        logger.info(f"Initializing Polymarket Bot | ENV={ENV}")
        
        # Load tokens
        config_path = os.path.join(os.path.dirname(__file__), "config_tokens.json")
        with open(config_path, "r") as f:
            self.token_configs = json.load(f)
            
        # Extract pure token IDs (flattened)
        self.all_token_ids = []
        for asset, config in self.token_configs.items():
            tks = config.get("tokens", {})
            if tks.get("YES"): self.all_token_ids.append(tks["YES"])
            if tks.get("NO"): self.all_token_ids.append(tks["NO"])
            
        # Risk & Execution
        self.risk_manager = RiskManager(initial_capital=1000.0)
        self.order_manager = OrderManager(self.risk_manager)
        
        # Feeds
        self.poly_feed = PolyFeed(self.all_token_ids)
        self.deribit_feed = DeribitFeed()
        self.price_feed = PriceFeed()
        
        # Strategies
        self.correlation_strategy = CorrelationStrategy()
        self.arbitrage_strategy = ArbitrageStrategy()

        self.running = False
        
    async def _strategy_loop(self):
        """
        Main loop: toutes les 500ms, vérifie signaux et exécute.
        """
        logger.info("Starting Strategy Evaluation Loop (500ms).")
        while self.running:
            if self.risk_manager.kill_switch_active:
                logger.critical("Bot halted due to Risk Manager kill switch.")
                self.running = False
                break
                
            try:
                # 1. Evaluate Correlation Signals
                corr_signals = await self.correlation_strategy.evaluate(
                    self.price_feed.states,
                    self.poly_feed,
                    self.deribit_feed,
                    self.token_configs
                )
                
                for sig in corr_signals:
                    await self.order_manager.execute_correlation_signal(sig)
                    
                # 2. Evaluate Arbitrage Signals
                arb_signals = self.arbitrage_strategy.evaluate(
                    self.poly_feed,
                    self.token_configs
                )
                
                for sig in arb_signals:
                    await self.order_manager.execute_arbitrage_signal(sig)
                    
            except Exception as e:
                logger.error(f"Error in strategy loop: {e}")
                
            await asyncio.sleep(0.5)

    async def run(self):
        self.running = True
        logger.info("Starting all feeds...")
        
        # Using task group to supervise all async tasks
        try:
            # Using asyncio.gather instead of TaskGroup for backward compatibility
            poly_task = asyncio.create_task(self.poly_feed.connect())
            deri_task = asyncio.create_task(self.deribit_feed.connect())
            price_task = asyncio.create_task(self.price_feed.connect())
            
            # Attendre un peu pour que les feeds se remplissent
            logger.info("Waiting 5s for feeds to initialize...")
            await asyncio.sleep(5)
            
            strat_task = asyncio.create_task(self._strategy_loop())
            
            await asyncio.gather(poly_task, deri_task, price_task, strat_task)
        except asyncio.CancelledError:
            logger.info("Bot shutting down gracefully.")
        except Exception as e:
            logger.error(f"Fatal error in task group: {e}")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        self.poly_feed.stop()
        self.deribit_feed.stop()
        self.price_feed.stop()
        logger.info("Bot stopped.")

async def main():
    bot = PolymarketBot()
    
    def handle_sigint():
        logger.warning("SIGINT received! Shutting down...")
        bot.stop()
        
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_sigint)
        
    await bot.run()

if __name__ == "__main__":
    # Ensure logs folder exists
    os.makedirs("logs", exist_ok=True)
    asyncio.run(main())
