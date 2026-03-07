import asyncio
import aiohttp
import json
import os
import sys

# Ajouter le rep parent au PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GAMMA_API
from utils.logger import get_logger

logger = get_logger("TokenFetcher")

async def fetch_tokens():
    url = f"{GAMMA_API}/events?closed=false&limit=1000"
    logger.info(f"Fetching active markets from {url}...")
    
    config = {
        "BTC": {"market": None, "market_id": None, "tokens": {}},
        "ETH": {"market": None, "market_id": None, "tokens": {}},
        "SOL": {"market": None, "market_id": None, "tokens": {}}
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    events = await response.json()
                    
                    for event in events:
                        title = event.get("title", "")
                        markets = event.get("markets", [])
                        
                        if not markets: continue
                        m = markets[0]
                        if m.get("closed"): continue
                        
                        tokens = m.get("clobTokenIds", "[]")
                        if isinstance(tokens, str):
                            try:
                                tokens = json.loads(tokens)
                            except json.JSONDecodeError:
                                continue
                                
                        if not isinstance(tokens, list) or len(tokens) < 2:
                            continue
                            
                        tk_yes = tokens[0]
                        tk_no = tokens[1]
                        
                        market_data = {
                            "market": title,
                            "market_id": m.get("id"),
                            "tokens": {"YES": tk_yes, "NO": tk_no}
                        }
                        
                        title_lower = title.lower()
                        if "bitcoin" in title_lower and not config["BTC"]["market"]:
                            config["BTC"] = market_data
                        elif ("ethereum" in title_lower or "eth " in title_lower) and not config["ETH"]["market"]:
                            config["ETH"] = market_data
                        elif ("solana" in title_lower or "sol " in title_lower) and not config["SOL"]["market"]:
                            config["SOL"] = market_data
                            
                        if config["BTC"]["market"] and config["ETH"]["market"] and config["SOL"]["market"]:
                            break
                            
    except Exception as e:
        logger.error(f"Error fetching tokens: {e}")
        
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config_tokens.json")
    
    # If a generic coin wasn't found, just duplicate another token so we don't crash the engine for testing
    if not config["SOL"]["market"]:
        logger.warning("Could not find a Solana market, mocking SOL to ETH token for engine testing.")
        config["SOL"] = config["ETH"]
        
    if not config["ETH"]["market"]:
        config["ETH"] = config["BTC"]

    with open(config_path, "w") as f:
        json.dump(config, f, indent=4)
        
    logger.info(f"Successfully wrote mock token IDs to {config_path} for paper trading.")
    logger.info(f"BTC: {config['BTC']['market']}")
    logger.info(f"ETH: {config['ETH']['market']}")
    logger.info(f"SOL: {config['SOL']['market']}")

if __name__ == "__main__":
    asyncio.run(fetch_tokens())
