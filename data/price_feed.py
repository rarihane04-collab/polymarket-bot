import asyncio
import json
import websockets
from config import WSS_BINANCE

from utils.logger import get_logger

logger = get_logger("PriceFeed")

class PriceState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.spot = 0.0
        self.open_4h = 0.0
        self.high_4h = 0.0
        self.low_4h = 0.0
        self.volume_4h = 0.0
        self.kline_start_time = 0

    @property
    def ret_from_open(self) -> float:
        """
        Return from the open of the current 4H window.
        """
        if self.open_4h == 0.0:
            return 0.0
        return (self.spot - self.open_4h) / self.open_4h


class PriceFeed:
    def __init__(self):
        self.states = {
            "BTC": PriceState("BTC"),
            "ETH": PriceState("ETH"),
            "SOL": PriceState("SOL")
        }
        self.running = False
        
        self.symbol_map = {
            "btcusdt": "BTC",
            "ethusdt": "ETH",
            "solusdt": "SOL"
        }

    async def connect(self):
        self.running = True
        backoff = 1
        
        channels = [
            "btcusdt@ticker", "btcusdt@kline_4h",
            "ethusdt@ticker", "ethusdt@kline_4h",
            "solusdt@ticker", "solusdt@kline_4h"
        ]
        
        streams = "/".join(channels)
        url = f"{WSS_BINANCE}?streams={streams}"
        
        while self.running:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("Connected to Binance Combined Streams.")
                    backoff = 1
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        stream = data.get("stream", "")
                        payload = data.get("data", {})
                        
                        symbol = stream.split("@")[0]
                        asset = self.symbol_map.get(symbol)
                        if not asset:
                            continue
                            
                        state = self.states[asset]
                        
                        if "ticker" in stream:
                            # Ticker payload
                            state.spot = float(payload.get("c", state.spot))
                        elif "kline" in stream:
                            # Kline payload
                            kline = payload.get("k", {})
                            state.open_4h = float(kline.get("o", state.open_4h))
                            state.high_4h = float(kline.get("h", state.high_4h))
                            state.low_4h = float(kline.get("l", state.low_4h))
                            state.volume_4h = float(kline.get("v", state.volume_4h))
                            state.kline_start_time = int(kline.get("t", state.kline_start_time))
                            
                            # Keep spot updated with kline close if ticker hasn't kicked in
                            state.spot = float(kline.get("c", state.spot))
                            
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"Binance connection closed. Reconnecting in {backoff}s...")
            except Exception as e:
                logger.error(f"Error in PriceFeed: {e}")
                
            if not self.running:
                break
                
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def stop(self):
        self.running = False
