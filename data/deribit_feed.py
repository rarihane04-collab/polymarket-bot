import asyncio
import json
import websockets
import math
from config import WSS_DERIBIT

from utils.logger import get_logger

logger = get_logger("DeribitFeed")

class DeribitFeed:
    def __init__(self):
        self.dvol = {
            "BTC": 0.0,
            "ETH": 0.0,
            "SOL": 0.0
        }
        self.running = False

    async def connect(self):
        self.running = True
        backoff = 1
        
        while self.running:
            try:
                async with websockets.connect(WSS_DERIBIT) as ws:
                    logger.info("Connected to Deribit WS.")
                    
                    channels = [
                        "deribit_volatility_index.btc_usd",
                        "deribit_volatility_index.eth_usd",
                        "deribit_volatility_index.sol_usd"
                    ]
                    
                    sub_msg = {
                        "jsonrpc": "2.0",
                        "method": "public/subscribe",
                        "id": 42,
                        "params": {
                            "channels": channels
                        }
                    }
                    await ws.send(json.dumps(sub_msg))
                    
                    backoff = 1
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        if "method" in data and data["method"] == "subscription":
                            channel = data["params"]["channel"]
                            volatility = float(data["params"]["data"]["volatility"]) / 100.0  # Convert to decimal
                            
                            if "btc" in channel:
                                self.dvol["BTC"] = volatility
                            elif "eth" in channel:
                                self.dvol["ETH"] = volatility
                            elif "sol" in channel:
                                self.dvol["SOL"] = volatility
                                
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"Deribit connection closed. Reconnecting in {backoff}s...")
            except Exception as e:
                logger.error(f"Error in DeribitFeed: {e}")
                
            if not self.running:
                break
                
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def get_annualized_vol(self, symbol: str) -> float:
        return self.dvol.get(symbol, 0.0)

    def to_4h(self, symbol: str) -> float:
        """
        Convert annualized volatility to 4H horizon.
        1 year = 8760 hours. Number of 4H periods = 2190
        sigma_4H = sigma_annuel / sqrt(2190)
        """
        ann_vol = self.get_annualized_vol(symbol)
        if ann_vol == 0.0:
            return 0.0
        return ann_vol / math.sqrt(2190)

    def stop(self):
        self.running = False
