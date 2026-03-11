import json, websocket, time, threading

results = {"binance": False, "polymarket": False}

# Test Polymarket CLOB WS avec le BON payload
def test_polymarket(token_id: str):
    def on_open(ws):
        payload = json.dumps({
            "type":      "subscribe",
            "assets_ids": [token_id],   # ← bon champ
            "channel":   "market",
        })
        ws.send(payload)
        print(f"[POLY] Payload envoyé: {payload[:80]}")

    def on_message(ws, msg):
        results["polymarket"] = True
        print(f"[POLY] ✅ Message reçu: {msg[:80]}")
        ws.close()

    def on_error(ws, err):
        print(f"[POLY] ❌ Erreur: {err}")

    ws = websocket.WebSocketApp(
        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    time.sleep(10)
    ws.close()

# Test Binance WS
def test_binance():
    def on_message(ws, msg):
        results["binance"] = True
        data = json.loads(msg)
        print(f"[BINANCE] ✅ BTC = {data.get('data',{}).get('p','?')}")
        ws.close()

    ws = websocket.WebSocketApp(
        "wss://stream.binance.com:9443/stream"
        "?streams=btcusdt@trade",
        on_message=on_message,
    )
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    time.sleep(10)
    ws.close()

if __name__ == "__main__":
    # 1. Trouver n'importe quel vrai token_id actif
    import requests
    r = requests.get(
        "https://gamma-api.polymarket.com/markets?limit=1&active=true&closed=false",
    )
    market = r.json()[0]
    
    import ast
    token_id = ast.literal_eval(market["clobTokenIds"])[0]
    print(f"[INFO] Token trouvé: {token_id[:20]}... depuis le marché: {market['question']}")

    # 2. Lancer les deux tests en parallèle
    t1 = threading.Thread(target=test_binance)
    t2 = threading.Thread(target=test_polymarket, args=(token_id,))
    t1.start(); t2.start()
    t1.join();  t2.join()

    # 3. Résultat
    print("\n" + "═"*40)
    print(f"  Binance    : {'✅ OK' if results['binance']    else '❌ KO'}")
    print(f"  Polymarket : {'✅ OK' if results['polymarket'] else '❌ KO'}")
    print("═"*40)
