import asyncio
import aiohttp
import numpy as np
import time

from config import BETA_WINDOW, SIGNAL_THRESHOLD, KELLY_FRACTION, ENV
from strategies.pricer import price_binary_call
from utils.logger import get_logger

logger = get_logger("CorrelationStrategy")

class CorrelationStrategy:
    def __init__(self):
        self.betas = {"ETH": 1.0, "SOL": 1.0}
        self.last_beta_update = 0
        self.lead_asset = "BTC"
        self.lag_assets = ["ETH", "SOL"]
        self.expected_win_rate = 0.55  # Base assumption for Kelly

    async def _fetch_historical_returns(self, symbol: str, limit: int = BETA_WINDOW) -> list:
        # Fetch 4H klines from Binance API
        url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol + "USDT",
            "interval": "4h",
            "limit": limit + 1
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    # data element: [Open time, Open, High, Low, Close, Volume, ...]
                    closes = [float(candle[4]) for candle in data]
                    # Compute returns: (close_n - close_{n-1}) / close_{n-1}
                    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
                    return returns
        return [0.0] * limit

    async def update_betas(self):
        current_time = time.time()
        # Update betas every 4 hours or at startup
        if current_time - self.last_beta_update > 4 * 3600:
            logger.info("Updating betas...")
            try:
                btc_returns = await self._fetch_historical_returns(self.lead_asset)
                for lag in self.lag_assets:
                    lag_returns = await self._fetch_historical_returns(lag)
                    
                    if len(btc_returns) > 1 and len(lag_returns) == len(btc_returns):
                        covariance = np.cov(lag_returns, btc_returns)[0][1]
                        variance = np.var(btc_returns)
                        if variance > 0:
                            self.betas[lag] = covariance / variance
                            logger.info(f"Updated beta for {lag} vs BTC: {self.betas[lag]:.4f}")
            except Exception as e:
                logger.error(f"Failed to update betas: {e}")
            finally:
                self.last_beta_update = current_time

    async def evaluate(self, price_feeds, poly_feed, deribit_feed, token_configs) -> list:
        """
        Evaluates signals for lag assets based on lead asset movement.
        Returns a list of signals (dictionaries dictating orders).
        """
        await self.update_betas()
        
        signals = []
        btc_state = price_feeds["BTC"]
        btc_ret = btc_state.ret_from_open
        
        for lag in self.lag_assets:
            lag_state = price_feeds[lag]
            lag_open = lag_state.open_4h
            if lag_open == 0:
                continue
                
            beta = self.betas.get(lag, 1.0)
            theo_lag_ret = beta * btc_ret
            theo_spot = lag_open * (1 + theo_lag_ret)
            
            # Sigma and Time for pricing
            sigma_4h = deribit_feed.to_4h(lag)
            if sigma_4h == 0:
                sigma_4h = deribit_feed.to_4h("BTC")  # Fallback

            window_duration_ms = 4 * 60 * 60 * 1000
            current_time_ms = int(time.time() * 1000)
            elapsed_ms = current_time_ms - lag_state.kline_start_time
            hours_left = max(0.0, (window_duration_ms - elapsed_ms) / (1000 * 60 * 60))
            
            # Fair implied prob of YES
            fair_yes = price_binary_call(theo_spot, lag_open, sigma_4h, hours_left)
            fair_no = 1.0 - fair_yes
            
            # Polymarket mid_prices
            token_ids = token_configs.get(lag, {}).get("tokens", {})
            yes_token = token_ids.get("YES")
            no_token = token_ids.get("NO")
            
            if not yes_token or not no_token:
                continue
                
            yes_ob = poly_feed.orderbooks.get(yes_token)
            no_ob = poly_feed.orderbooks.get(no_token)
            
            if not yes_ob or not no_ob:
                continue
                
            poly_yes = yes_ob.mid_price
            poly_no = no_ob.mid_price
            
            # Dynamic Threshold built on Volatility. If sigma_4h is 0.05 (5% in 4H), threshold scaling
            # ensures we don't trade small edges in highly volatile (noisy) environments.
            dynamic_threshold = max(SIGNAL_THRESHOLD, sigma_4h * 1.5)
            
            # Signal detection with OBI confirmation
            # OBI > -0.2 means the sell wall is not completely overwhelming the bids
            if (fair_yes - poly_yes) > dynamic_threshold and yes_ob.obi > -0.5:
                signals.append({
                    "strategy": "CORRELATION",
                    "asset": lag,
                    "direction": "YES",
                    "token_id": yes_token,
                    "edge": fair_yes - poly_yes,
                    "target_price": yes_ob.best_ask,
                    "prob_win": fair_yes
                })
            elif (fair_no - poly_no) > dynamic_threshold and no_ob.obi > -0.5:
                signals.append({
                    "strategy": "CORRELATION",
                    "asset": lag,
                    "direction": "NO",
                    "token_id": no_token,
                    "edge": fair_no - poly_no,
                    "target_price": no_ob.best_ask,
                    "prob_win": fair_no
                })
                
        return signals
