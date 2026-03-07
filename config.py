import os
from dotenv import load_dotenv

load_dotenv()

CHAIN_ID          = 137 # Polygon
CLOB_HOST         = "https://clob.polymarket.com"
GAMMA_API         = "https://gamma-api.polymarket.com"
WSS_CLOB          = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WSS_DERIBIT       = "wss://www.deribit.com/ws/api/v2"
WSS_BINANCE       = "wss://stream.binance.com:9443/stream"

BETA_WINDOW       = 20        # Fenêtres 4H pour beta rolling
SIGNAL_THRESHOLD  = 0.05      # 5% mispricing minimum pour entrer
ARB_MIN_EDGE      = 0.005     # Edge net minimum après 3% de friction totale
TOTAL_FRICTION    = 0.03      # Frais maker 0.2% + taker 1% + gas + slippage
MAX_POSITION_PCT  = 0.10      # 10% bankroll max par trade
MAX_DRAWDOWN_PCT  = 0.15      # Kill switch à -15%
KELLY_FRACTION    = 0.5       # Half-Kelly
PARTIAL_FILL_WAIT = 15        # Secondes avant liquidation orphan

ENV = os.getenv("ENV", "paper")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
