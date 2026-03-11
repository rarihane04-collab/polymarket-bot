---
description: How to enhance and run extended testing on the Polymarket trading bot
---

# Polymarket Bot Enhancement & Extended Testing Workflow

// turbo-all

## Understanding 4-Hour Markets

Polymarket's "Up or Down" crypto markets work as follows:

- **Title format**: `Bitcoin Up or Down - March 7, 8:00AM-12:00PM ET`
- **URL slug**: `btc-updown-4h-{unix_timestamp}` (timestamp = UTC start of the 4H window)
- **Resolution**: Resolves to **"Up"** if `end_price >= start_price`, otherwise **"Down"**
- **Data sources**:
  - 4H markets (BTC): Chainlink BTC/USD data stream
  - Hourly markets (ETH, SOL): Binance Candle Data (open/close of specific 1H candle)
- **Assets with 4H markets**: BTC (always active)
- **Assets with hourly markets**: BTC, ETH, SOL, XRP
- **Payout**: $1 for correct outcome, $0 for wrong
- **Price to beat**: The "start price" at the beginning of the window (e.g., $67,990.79)
- **YES = Up = price went up or stayed same**, **NO = Down = price went down**
- New 4H windows are created automatically every 4 hours aligned to ET schedule

> **IMPORTANT**: The Gamma API `title` search does NOT find these markets. You must use the `slug` field patterns like `btc-updown-4h`, `eth-updown-hourly`, `sol-updown-hourly`.

## Step 1: Update Token Discovery for 4H/Hourly Markets

Update `scripts/fetch_token_ids.py` to search using slug patterns instead of title:

```bash
cd /Users/ramy/Desktop/polymarket_bot

# Query the Gamma API with the slug pattern to find active 4H markets
curl -s "https://gamma-api.polymarket.com/events?closed=false&limit=10&slug=btc-updown-4h" | python3 -m json.tool | head -50
```

The token fetcher should:
1. Search for events with slug containing `btc-updown-4h`, `eth-updown-hourly`, `sol-updown-hourly`
2. Pick the **currently active** window (the one whose start time <= now < end time)
3. Extract the YES/NO token IDs from the active market
4. Extract the **"Price to Beat"** (start price) from the market description if available

## Step 2: Implement "Price to Beat" Extraction

The correlation strategy currently uses Binance 4H kline open as the strike price. For proper 4H market trading:

1. Parse the start price from the market description or compute it from:
   - The Binance 4H kline that aligns with the Polymarket window start time
2. Use this as the `strike_price` in the Black-Scholes binary option pricer
3. Log: `price_to_beat`, `current_spot`, `probability_of_up`

Update `strategies/pricer.py` to accept the explicit price_to_beat from the market.

## Step 3: Enhance Signal Quality Filters

Add these filters in `strategies/correlation.py`:

1. **Time decay guard**: Don't enter trades with < 30 minutes left in the window (time_value is too low)
2. **Minimum spread filter**: Don't trade if spread > 10% (illiquid books)
3. **Realized vol tracking**: Compare implied vol (from price) vs Deribit vol — trade when they diverge
4. **Momentum confirmation**: Require BTC spot direction to match the signal direction for lag assets

## Step 4: Add Multi-Timeframe Support

Since ETH/SOL only have hourly markets:

1. Add a `MARKET_INTERVAL` config per asset: `BTC: "4h"`, `ETH: "1h"`, `SOL: "1h"`
2. Subscribe to both `kline_4h` AND `kline_1h` from Binance
3. Adjust the pricer's time-to-expiry based on the actual market interval
4. Adjust the vol conversion: `sigma_1h = sigma_annual / sqrt(8760)` vs `sigma_4h = sigma_annual / sqrt(2190)`

## Step 5: Run Extended Testing (10+ minutes)

```bash
cd /Users/ramy/Desktop/polymarket_bot

# Clear old logs
> logs/polymarket_bot.log
> logs/trades.json 2>/dev/null || true

# Run the bot for 10 minutes
./venv/bin/python3 -c "
import asyncio, sys
sys.path.insert(0, '.')

async def run():
    from main import main as bot_main
    try:
        await asyncio.wait_for(bot_main(), timeout=600)
    except asyncio.TimeoutError:
        print('Bot ran for 10min, stopping.')

asyncio.run(run())
"
```

## Step 6: Analyze Results

After the extended run:

```bash
cd /Users/ramy/Desktop/polymarket_bot

# Check for errors
grep -c "ERROR" logs/polymarket_bot.log

# Count signals generated
grep -c "SIGNAL:" logs/polymarket_bot.log

# Count trades executed
grep -c "PAPER EXEC" logs/polymarket_bot.log

# Check status reports
grep "RISK SUMMARY" -A 6 logs/polymarket_bot.log

# Check trade journal
cat logs/trades.json 2>/dev/null || echo "No trades logged"

# Check for repeated same-signal spam (should be 0)
grep "SIGNAL:" logs/polymarket_bot.log | sort | uniq -c | sort -rn | head -5
```

**Success criteria:**
- Zero crash loops (no repeating ERROR lines)
- Status reports every 60s with live price data
- Signal cooldown working (no identical signals within 60s)
- Position caps working (not buying more than Kelly target)
- If 4H markets are active: signals with realistic edges (< 15%)

## Step 7: Iterative Improvement Cycle

After each run, improve based on observations:

1. **If signals are too frequent**: Increase `SIGNAL_COOLDOWN_SECONDS` in `config.py`
2. **If edges are unrealistic (> 30%)**: The strike price is wrong — check that `open_4h` aligns with the Polymarket window
3. **If no signals at all**: Check if OBI threshold is too tight, or if markets are too illiquid
4. **If P&L tracking shows consistent losses**: The Black-Scholes model may need calibration — add a volatility premium adjustment
5. **If feeds keep disconnecting**: Increase backoff and add heartbeat monitoring

## Step 8: Long-Duration Run (1+ hours)

Once short runs are clean, do a long run:

```bash
cd /Users/ramy/Desktop/polymarket_bot

# Run for 1 hour
./venv/bin/python3 -c "
import asyncio, sys
sys.path.insert(0, '.')

async def run():
    from main import main as bot_main
    try:
        await asyncio.wait_for(bot_main(), timeout=3600)
    except asyncio.TimeoutError:
        print('Bot ran for 1hr, stopping.')

asyncio.run(run())
"
```

This captures at least one full 4H window cycle to verify:
- Trade resolution at window end
- Beta updates (every 4 hours)
- Memory stability (no leaks)
- Feed reconnection resilience
