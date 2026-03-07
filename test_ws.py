import asyncio
import websockets

async def test_ws(url):
    print(f"Testing {url}...")
    try:
        async with websockets.connect(url) as ws:
            print("Connected!")
    except Exception as e:
        print(f"Error: {e}")

async def main():
    await test_ws("wss://ws-subscriptions-clob.polymarket.com/ws/market")
    await test_ws("wss://ws-subscriptions-clob.polymarket.com/ws/")
    
asyncio.run(main())
