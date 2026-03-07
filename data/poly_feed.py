import asyncio
import json
import websockets
from collections import defaultdict
from config import WSS_CLOB

from utils.logger import get_logger

logger = get_logger("PolyFeed")

class OrderBookState:
    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids = {}  # price: size
        self.asks = {}  # price: size

    def update(self, bids: list, asks: list):
        for b in bids:
            price = b.get("price") if isinstance(b, dict) else b[0]
            size = b.get("size") if isinstance(b, dict) else b[1]
            if float(size) == 0:
                self.bids.pop(float(price), None)
            else:
                self.bids[float(price)] = float(size)
                
        for a in asks:
            price = a.get("price") if isinstance(a, dict) else a[0]
            size = a.get("size") if isinstance(a, dict) else a[1]
            if float(size) == 0:
                self.asks.pop(float(price), None)
            else:
                self.asks[float(price)] = float(size)

    @property
    def best_bid(self) -> float:
        return max(self.bids.keys(), default=0.0)

    @property
    def best_ask(self) -> float:
        return min(self.asks.keys(), default=1.0)

    @property
    def mid_price(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0 and ba < 1:
            return (bb + ba) / 2
        elif bb > 0:
            return bb
        elif ba < 1:
            return ba
        return 0.5

    @property
    def obi(self) -> float:
        """
        Order Book Imbalance = (V_bid - V_ask) / (V_bid + V_ask)
        """
        v_bid = sum(self.bids.values())
        v_ask = sum(self.asks.values())
        if v_bid + v_ask == 0:
            return 0.0
        return (v_bid - v_ask) / (v_bid + v_ask)

class PolyFeed:
    def __init__(self, token_ids: list):
        self.token_ids = token_ids
        self.orderbooks = {tid: OrderBookState(tid) for tid in token_ids}
        self.running = False

    async def connect(self):
        self.running = True
        backoff = 1
        
        while self.running:
            try:
                async with websockets.connect(WSS_CLOB) as ws:
                    logger.info(f"Connected to Polymarket CLOB. Subscribing to {len(self.token_ids)} tokens.")
                    sub_msg = {
                        "assets_ids": self.token_ids,
                        "type": "market"
                    }
                    await ws.send(json.dumps(sub_msg))
                    
                    backoff = 1  # Reset backoff on successful connection
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        # We expect "event_type": "book" or similar update format from Polymarket
                        if isinstance(data, list):
                            for event in data:
                                self._process_event(event)
                        elif isinstance(data, dict):
                            self._process_event(data)
                            
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"Connection closed. Reconnecting in {backoff}s...")
            except Exception as e:
                logger.error(f"Error in PolyFeed: {e}")
                
            if not self.running:
                break
                
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _process_event(self, event: dict):
        # The structure depends on the exact CLOB Websocket definition.
        # Generally, it sends an event with "asset_id", "bids", "asks".
        asset_id = event.get("asset_id")
        if not asset_id or asset_id not in self.orderbooks:
            return
            
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        
        if bids or asks:
            self.orderbooks[asset_id].update(bids, asks)

    def stop(self):
        self.running = False
