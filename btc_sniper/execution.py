import threading
import time
import logging
import traceback
import numpy as np
import os
from btc_sniper import config
from btc_sniper.config import debug_logger

logger = logging.getLogger("Execution")

class PaperOrderBook:
    def __init__(self, ob_yes, ob_no):
        self.ob_yes   = ob_yes
        self.ob_no    = ob_no
        self.orders   = {}
        self._counter = 0
        self.fill_log = []

    def create_and_post_order(self, order_args) -> dict:
        self._counter += 1
        oid = f"PAPER-{self._counter:06d}"
        self.orders[oid] = {
            "orderID":      oid,
            "token_id":     order_args.token_id,
            "price":        order_args.price,
            "size":         order_args.size,
            "side":         order_args.side,
            "status":       "LIVE",
            "size_matched": 0.0,
            "placed_at":    time.time(),
        }
        config.debug_logger.info(
            f"ORDER_PLACED | "
            f"id={oid} "
            f"side={order_args.side} "
            f"price={order_args.price:.4f} "
            f"size=${order_args.size:.2f} "
            f"type={'MAKER' if 'MAKER' in getattr(order_args, 'reason', '') else 'LIMIT'} "
            f"fee=0% (maker) "
            f"token={order_args.token_id[:16]}..."
        )

        from btc_sniper.config import log_trade_event, log_report
        log_trade_event("ORDER_PLACED", {
            "order_id":   oid,
            "side":       order_args.side,
            "price":      order_args.price,
            "size":       order_args.size,
            "order_type": "MAKER" if "MAKER" in getattr(order_args, "reason", "") else "LIMIT",
            "token_id":   order_args.token_id,
        })

        log_report(
            f"| 📥 ORDER | `{oid}` | "
            f"{order_args.side} @{order_args.price:.4f} ×${order_args.size:.2f} | "
            f"maker fee=0% |"
        )
        from btc_sniper import display
        display.log(
            f"📋 PAPER {order_args.side} "
            f"@{order_args.price:.3f} ×${order_args.size:.1f} "
            f"[{oid}]"
        )
        return {"orderID": oid}

    def get_order(self, order_id: str) -> dict:
        """
        Simule un fill basé sur les vrais prix Polymarket.
        BUY  : rempli si ob.best_ask <= order_price + 0.001
        SELL : rempli si ob.best_bid >= order_price - 0.001
        """
        order = self.orders.get(order_id)
        if not order or order["status"] != "LIVE":
            return order or {"status": "NOT_FOUND"}

        ob = self.ob_yes
        if order["token_id"] == self.ob_no.token_id:
            ob = self.ob_no

        if order["side"] == "BUY":
            fill = ob.best_ask > 0 and \
                   ob.best_ask <= order["price"] + 0.001
            fp   = ob.best_ask
        else:
            fill = ob.best_bid > 0 and \
                   ob.best_bid >= order["price"] - 0.001
            fp   = ob.best_bid

        if fill:
            order["status"]       = "MATCHED"
            order["size_matched"] = order["size"]
            order["fill_price"]   = fp
            order["filled_at"]    = time.time()
            latency = (time.time() - order["placed_at"]) * 1000
            self.fill_log.append({
                "side":      order["side"],
                "price":     fp,
                "size":      order["size"],
                "latency_ms":latency,
            })
            latency_ms = (time.time() - order["placed_at"]) * 1000
            config.debug_logger.info(
                f"ORDER_FILLED | "
                f"id={order_id} "
                f"side={order['side']} "
                f"fill_price={fp:.4f} "
                f"size=${order['size']:.2f} "
                f"latency={latency_ms:.0f}ms "
                f"slippage={fp - order['price']:+.4f}"
            )

            from btc_sniper.config import log_trade_event, log_report
            log_trade_event("ORDER_FILLED", {
                "order_id":    order_id,
                "side":        order['side'],
                "order_price": order['price'],
                "fill_price":  fp,
                "size":        order['size'],
                "latency_ms":  latency_ms,
                "slippage":    fp - order['price'],
            })

            log_report(
                f"| ✅ FILL  | `{order_id}` | "
                f"{order['side']} @{fp:.4f} ×${order['size']:.2f} | "
                f"latency={latency_ms:.0f}ms |"
            )
            from btc_sniper import display
            display.log(
                f"✅ FILL {order['side']} "
                f"@{fp:.3f} ×${order['size']:.1f} "
                f"({latency:.0f}ms)"
            )
        return order

    def cancel(self, order_id: str) -> dict:
        if order_id in self.orders:
            order = self.orders[order_id]
            order["status"] = "CANCELLED"
            config.debug_logger.info(
                f"ORDER_CANCELLED | "
                f"id={order_id} "
                f"reason=USER_OR_STALE "
                f"age={(time.time() - order['placed_at'])*1000:.0f}ms"
            )

            from btc_sniper.config import log_trade_event
            log_trade_event("ORDER_CANCELLED", {
                "order_id": order_id,
                "reason":   "USER_OR_STALE",
                "age_ms":   (time.time() - order['placed_at']) * 1000,
            })
        return {"status": "CANCELLED"}

class SmartLimitEngine:
    """
    Continuously places BUY limit orders and immediately places
    a SELL limit order the moment a BUY is filled.
    Cycles: BUY -> filled -> SELL -> filled -> BUY again
    Never holds to resolution unless no sell fill before T-10s.
    """

    SPREAD_TARGET_CENTS  = 0.015   # Min profit per unit we require
    MAX_CONCURRENT_BUYS  = 3       # Max open buy orders at once
    BASE_SIZE            = 15.0    # USDC per order
    REPRICE_EVERY_S      = 2.0     # Reprice stale orders every 2s
    STALE_THRESHOLD_CENT = 0.008   # Reprice if market moved > 0.8cents

    def __init__(self, ob_yes, ob_no, signal_getter, time_remaining_getter, bot=None):
        self.ob_yes         = ob_yes
        self.ob_no          = ob_no
        self.get_signal     = signal_getter
        self.get_tr         = time_remaining_getter
        self.bot            = bot
        self.open_buys      = {}    # order_id -> {token, price, size, placed_at}
        self.pending_sells  = {}    # buy_order_id -> sell_order_id
        self.filled_buys    = []    # list of fill events
        self.session_pnl    = 0.0
        self.scalp_count    = 0
        self.realized_pnl   = 0.0
        self.total_fills    = 0
        self._lock          = threading.Lock()
        self._running       = False
        from btc_sniper import display
        self.display        = display
        
        if config.TRADING_LEVEL < 2:
            self.client = PaperOrderBook(ob_yes, ob_no)
            self.display.log("📋 MODE PAPER TRADING — aucun ordre réel")
        else:
            from py_clob_client.client import ClobClient
            self.client = ClobClient(
                host=config.CLOB_API,
                chain_id=config.CHAIN_ID,
                private_key=config.PRIVATE_KEY,
            )
            self.display.log("🔴 MODE LIVE — vrais ordres actifs")

    def start(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="scalp_engine")
        t.start()
        self.display.log("🔄 ScalpEngine started — BUY/SELL mode active")

    def stop(self):
        self._running = False
        self.cancel_all_open()

    def _loop(self):
        last_reprice = 0.0
        while self._running:
            try:
                tr     = self.get_tr()
                signal = self.get_signal()
                if not signal:
                    time.sleep(0.5)
                    continue

                if tr < 10:
                    break

                if time.time() - last_reprice >= self.REPRICE_EVERY_S:
                    self._reprice_stale_orders()
                    last_reprice = time.time()

                n_open = len(self.open_buys)
                if n_open < self.MAX_CONCURRENT_BUYS and signal.direction not in ("SKIP", "WAITING", None):
                    self._place_buy_orders(signal, tr)

                self._check_fills()

            except Exception as e:
                self.display.log(f"⚠️ ScalpEngine error: {e}")
                debug_logger.error(f"ENGINE_ERROR: {e}\n{traceback.format_exc()}")
            time.sleep(0.5)

    def _place_buy_orders(self, signal, time_remaining: float):
        direction = signal.direction
        
        ob        = self.ob_yes if direction == "UP" else self.ob_no
        token_id  = self.ob_yes.token_id if direction == "UP" else self.ob_no.token_id

        # 1. Fetch live contextual data
        if self.bot and self.bot.feed:
            current_btc_price = self.display.state.btc_price
            window_open_price = self.display.state.window_open
            candles_1m = list(self.bot.feed.candles_1m)
            ticks      = list(self.bot.feed.ticks)
            mode       = getattr(self.bot, "mode", "safe")
            bankroll   = self.display.state.bankroll
        else:
            return

        from btc_sniper.pricer import compute_smart_price

        # 2. compute_smart_price
        # ── GARDE : OB pas encore prêt ───────────────────
        ob_mid = ob.mid
        if ob_mid <= 0:
            # Fallback : essayer best_bid ou best_ask
            if ob.best_bid > 0 and ob.best_ask > 0:
                ob_mid = (ob.best_bid + ob.best_ask) / 2
            elif ob.best_bid > 0:
                ob_mid = ob.best_bid
            else:
                config.debug_logger.debug(
                    f"SKIP_NO_OB | {direction} "
                    f"ob_mid=0 upd={ob.update_count} "
                    f"bid={ob.best_bid} ask={ob.best_ask}"
                )
                return   # OB vraiment vide → skip proprement

        ob_spread = ob.best_ask - ob.best_bid
        if ob_spread < 0:
            ob_spread = 0.02   # spread par défaut si incohérent

        # 3. SELECT ENTRY MODE (MAKER vs TAKER) 💸
        from btc_sniper.pricer import select_entry_mode, taker_fee_rate
        
        # ── ENTRY MODE ───────────────────────────────────
        mode_info = select_entry_mode(
            token_price = ob_mid,      # ← valeur garantie > 0
            T_remaining = time_remaining,
            confidence  = signal.confidence,
            spread      = ob_spread,
        )

        # ── PRICER ───────────────────────────────────────
        pricing = compute_smart_price(
            S         = current_btc_price,
            K         = window_open_price,
            T_seconds = time_remaining,
            direction = direction,
            ob        = ob,
            candles   = list(candles_1m),
            ticks     = list(ticks),
            bankroll  = bankroll,
            mode      = mode,
        )

        config.debug_logger.info(
            f"ENTRY_DECISION | "
            f"mode={mode_info['mode']} "
            f"should_trade={mode_info['should_trade']} "
            f"ob_mid={ob_mid:.4f} "
            f"spread={ob_spread:.4f} "
            f"T={time_remaining:.1f}s "
            f"conf={signal.confidence:.4f} "
            f"reason='{mode_info.get('reason', '?')}'"
        )

        # Log complet pour analyse
        config.debug_logger.info(
            f"PRICER | dir={direction} "
            f"BS={pricing['bs_price']:.3f} "
            f"ask={pricing['market_ask']:.3f} "
            f"entry={pricing['entry_price']:.3f} "
            f"edge={pricing['edge_pct']:+.1f}% "
            f"ev={pricing['ev_pct']:+.1f}% "
            f"kelly={pricing['kelly_adj']:.3f} "
            f"size=${pricing['bet_size']:.2f} "
            f"sigma={pricing['sigma']:.3f} "
            f"ob_adj={pricing['ob_adj']:+.4f} "
            f"mom_adj={pricing['mom_adj']:+.4f} "
            f"→ {pricing['reason']}"
        )

        self.display.state.current_mode_label = mode_info["mode"]

        if not mode_info["should_trade"]:
            return


        # 4. Placer selon le mode
        if mode_info["order_type"] == "MAKER":
            # Prix légèrement au-dessus du best bid
            buy_price = round(ob.best_bid + 0.001, 4)
            # Vérifier qu'on reste maker (< best_ask)
            if buy_price >= ob.best_ask:
                buy_price = round(ob.best_ask - 0.002, 4)
                
            # Track fees saved (estimer ce qu'on aurait payé en Taker)
            saved = taker_fee_rate(buy_price) * pricing["bet_size"] * buy_price
            self.display.state.fees_saved += saved
            
            if mode_info["mode"] == "A_MAKER_HOLD":
                self.display.state.trades_mode_a += 1
            else:
                self.display.state.trades_mode_c += 1
        else:
            # TAKER extrêmes : payer le ask directement
            buy_price = ob.best_ask
            paid = taker_fee_rate(buy_price) * pricing["bet_size"] * buy_price
            self.display.state.fees_paid += paid
            self.display.state.trades_mode_b += 1

        size      = pricing["bet_size"]
        sell_target = pricing["sell_target"]

        try:
            if isinstance(self.client, PaperOrderBook):
                class DummyOrderArgs:
                    def __init__(self, token_id, price, size, side):
                        self.token_id = token_id
                        self.price = price
                        self.size = size
                        self.side = side
                order_args = DummyOrderArgs(
                    token_id=token_id,
                    price=buy_price,
                    size=size,
                    side="BUY"
                )
            else:
                from py_clob_client.clob_types import OrderArgs
                order_args = OrderArgs(
                    token_id  = token_id,
                    price     = buy_price,
                    size      = size,
                    side      = "BUY",
                )
            resp = self.client.create_and_post_order(order_args)
            
            if hasattr(resp, "get"):
                order_id = resp.get("orderID")
            else:
                order_id = resp["orderID"]
                
            if config.TRADING_LEVEL >= 2:
                self.display.log(f"📥 BUY placed: {direction}@{buy_price:.3f} ×${size:.1f}")

            with self._lock:
                self.open_buys[order_id] = {
                    "token_id":  token_id,
                    "direction": direction,
                    "price":     buy_price,
                    "size":      size,
                    "placed_at": time.time(),
                }
            debug_logger.debug(f"ORDER_PLACED: BUY {direction}@{buy_price} size=${size}")
            if direction == "UP": self.display.state.open_orders_yes += 1
            else: self.display.state.open_orders_no += 1
        except Exception as e:
            self.display.log(f"❌ BUY order failed: {e}")
            debug_logger.error(f"ORDER_FAIL: {e}")

    def _check_fills(self):
        for order_id, info in list(self.open_buys.items()):
            try:
                is_filled = False
                fill_price = info["price"]
                fill_size  = info["size"]
                
                status = self.client.get_order(order_id)
                if getattr(status, "get", lambda x, y=None: y)("status") in ("MATCHED", "FILLED") or (isinstance(status, dict) and status.get("status") in ("MATCHED", "FILLED")):
                    fill_price = float(status.get("price", fill_price) if isinstance(status, dict) else status.price)
                    fill_size  = float(status.get("size_matched", fill_size) if isinstance(status, dict) else status.size_matched)
                    is_filled = True

                if is_filled:
                    self.display.log(f"✅ BUY FILLED: {info['direction']}@{fill_price:.3f} ×${fill_size:.1f} — placing SELL...")
                    with self._lock:
                        del self.open_buys[order_id]
                    self._place_sell(info["token_id"], info["direction"], fill_price, fill_size, order_id)

            except Exception as e:
                self.display.log(f"⚠️ Fill check error: {e}")
                
        # Also check sell fills for PnL
        for buy_order_id, sell_info in list(self.pending_sells.items()):
            try:
                sell_id = sell_info["order_id"]
                is_filled = False
                sell_fill_price = sell_info["sell_price"]
                sell_fill_size = sell_info["size"]
                
                status = self.client.get_order(sell_id)
                if getattr(status, "get", lambda x, y=None: y)("status") in ("MATCHED", "FILLED") or (isinstance(status, dict) and status.get("status") in ("MATCHED", "FILLED")):
                    sell_fill_price = float(status.get("price", sell_fill_price) if isinstance(status, dict) else status.price)
                    sell_fill_size  = float(status.get("size_matched", sell_fill_size) if isinstance(status, dict) else status.size_matched)
                    is_filled = True

                if is_filled:
                    pnl = (sell_fill_price - sell_info["buy_price"]) * sell_fill_size
                    self.realized_pnl += pnl
                    self.total_fills += 1
                    self.display.log(f"💰 Scalp fill: {pnl:+.4f} USDC (buy@{sell_info['buy_price']:.3f} sell@{sell_fill_price:.3f})")
                    with self._lock:
                        del self.pending_sells[buy_order_id]
                        if sell_info["direction"] == "UP" and self.display.state.open_orders_yes > 0:
                            self.display.state.open_orders_yes -= 1
                        elif sell_info["direction"] == "DOWN" and self.display.state.open_orders_no > 0:
                            self.display.state.open_orders_no -= 1
                            
            except Exception as e:
                self.display.log(f"⚠️ Sell fill check error: {e}")


    def _place_sell(self, token_id: str, direction: str, buy_price: float, size: float, buy_order_id: str):
        tr = self.get_tr()
        if tr < 30:
            sell_price = round(buy_price + 0.005, 4)
        else:
            sell_price = round(buy_price + self.SPREAD_TARGET_CENTS, 4)

        sell_price = min(sell_price, 0.98)

        try:
            if isinstance(self.client, PaperOrderBook):
                class DummyOrderArgs:
                    def __init__(self, token_id, price, size, side):
                        self.token_id = token_id
                        self.price = price
                        self.size = size
                        self.side = side
                order_args = DummyOrderArgs(
                    token_id=token_id,
                    price=sell_price,
                    size=size,
                    side="SELL"
                )
            else:
                from py_clob_client.clob_types import OrderArgs
                order_args = OrderArgs(
                    token_id  = token_id,
                    price     = sell_price,
                    size      = size,
                    side      = "SELL",
                )
            resp = self.client.create_and_post_order(order_args)
            if hasattr(resp, "get"):
                sell_id = resp.get("orderID")
            else:
                sell_id = resp["orderID"]

            with self._lock:
                self.pending_sells[buy_order_id] = {
                    "order_id":  sell_id,
                    "token_id":  token_id,
                    "direction": direction,
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "size":      size,
                    "placed_at": time.time(),
                }
            gross_pnl = (sell_price - buy_price) * size
            self.display.log(f"📤 SELL placed: {direction}@{sell_price:.3f} | target P&L: +${gross_pnl:.3f}")
            debug_logger.debug(f"ORDER_PLACED: SELL {direction}@{sell_price} (buy_price={buy_price})")
        except Exception as e:
            self.display.log(f"❌ SELL order failed: {e}")
            debug_logger.error(f"SELL_FAIL: {e}")

    def _reprice_stale_orders(self):
        for order_id, info in list(self.open_buys.items()):
            ob = self.ob_yes if info["direction"] == "UP" else self.ob_no
            current_bid = ob.best_bid
            drift = abs(current_bid - info["price"])
            if drift > self.STALE_THRESHOLD_CENT:
                try:
                    try:
                        self.client.cancel(order_id)
                        if config.TRADING_LEVEL >= 2:
                            self.display.log(f"🔄 Cancelled stale BUY (drift={drift*100:.1f}¢)")
                    except Exception as e:
                        self.display.log(f"⚠️ Cancel stale error: {e}")
                            
                    with self._lock:
                        del self.open_buys[order_id]
                        if info["direction"] == "UP" and self.display.state.open_orders_yes > 0:
                            self.display.state.open_orders_yes -= 1
                        elif info["direction"] == "DOWN" and self.display.state.open_orders_no > 0:
                            self.display.state.open_orders_no -= 1
                    debug_logger.debug(f"ORDER_CANCEL: Stale BUY @{info['price']}")
                except Exception:
                    pass

    def cancel_all_open(self):
        try:
            for oid in self.open_buys.keys():
                try: self.client.cancel(oid)
                except: pass
            for sinfo in self.pending_sells.values():
                try: self.client.cancel(sinfo["order_id"])
                except: pass
        except Exception:
            pass
            
        with self._lock:
            self.open_buys.clear()
            self.pending_sells.clear()
        self.display.state.open_orders_yes = 0
        self.display.state.open_orders_no  = 0
        self.display.log("🧹 All orders cancelled — window closed")
