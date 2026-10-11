"""가짜 거래소로 그리드 로직을 검증합니다 (실제 API 호출 없음).  실행: python test_grid_logic.py"""
import logging
import unittest
from decimal import Decimal as D

import ondo_grid_bot as g

CFG = {"baseIncrement": "0.0001", "quoteIncrement": "1"}


class FakeExchange:
    def __init__(self, price):
        self.price = D(price)
        self.orders = {}   # cid -> order dict
        self.position = D(0)
        self.realized = D(0)
        self.fail_next_post = None

    # 체결 엔진: 가격이 지정가를 지나가면 전량 체결
    def _try_fill(self, o):
        if o["status"] != "open":
            return
        p = D(o["price"])
        if (o["side"] == "buy" and self.price <= p) or (o["side"] == "sell" and self.price >= p):
            qty = D(o["size"]) - D(o["filledSize"])
            o["filledSize"] = o["size"]
            o["status"] = "fullyfilled"
            self.position += qty if o["side"] == "buy" else -qty
            self.realized += (-qty * p) if o["side"] == "buy" else (qty * p)

    def move(self, price):
        self.price = D(price)
        for o in self.orders.values():
            self._try_fill(o)

    # 클라이언트 인터페이스
    def mark_price(self):
        return self.price

    def open_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    def get_order(self, cid):
        if cid not in self.orders:
            raise g.ApiError(400, "order_not_found", "nf")
        return dict(self.orders[cid])

    def create_limit_order(self, side, price, size, cid):
        if self.fail_next_post:
            exc, self.fail_next_post = self.fail_next_post, None
            if isinstance(exc, g.NetworkError):  # 주문은 들어갔지만 응답 유실
                self._add(side, price, size, cid)
            raise exc
        return dict(self._add(side, price, size, cid))

    def _add(self, side, price, size, cid):
        o = {"orderId": f"id{len(self.orders)}", "clientOrderId": cid, "side": side, "price": g.fmt(price),
             "size": g.fmt(size), "filledSize": "0", "status": "open"}
        self.orders[cid] = o
        self._try_fill(o)
        return o


def drain(bot, ex):
    """체크 1회 + 낼 수 있는 주문 전부 발주."""
    bot.check()
    n = 0
    while (a := bot.next_action(0)) is not None:
        bot.execute(a, 0)
        n += 1
        assert n < 500
    return n


def open_entries(ex):
    return [o for o in ex.orders.values() if o["status"] == "open" and "_e_" in o["clientOrderId"]]


class GridTest(unittest.TestCase):
    def setUp(self):
        g.configure_market("BTC-USD.P")
        self.ex = FakeExchange("100000")
        self.st = g.new_state(D("100000"), D("1000"), g.calc_order_size(D("1000"), D("100000"), D("0.0001")))
        self.bot = g.GridBot(self.ex, self.st, CFG, persist=False)

    def test_initial_grid(self):
        self.assertEqual(drain(self.bot, self.ex), 100)
        sells = sorted(int(o["price"]) for o in open_entries(self.ex) if o["side"] == "sell")
        buys = sorted(int(o["price"]) for o in open_entries(self.ex) if o["side"] == "buy")
        self.assertEqual(sells[0], 100200); self.assertEqual(sells[-1], 110000)
        self.assertEqual(buys[-1], 99800); self.assertEqual(buys[0], 90000)
        # 1000 × 5% ÷ 100000 = 0.0005 — 모든 주문 동일 수량
        self.assertEqual({o["size"] for o in open_entries(self.ex)}, {"0.0005"})
        self.assertEqual(drain(self.bot, self.ex), 0)  # 중복 주문 없음

    def test_order_size_fixed_on_reentry(self):
        drain(self.bot, self.ex)
        self.ex.move("100250")
        drain(self.bot, self.ex)
        self.ex.move("99990")            # 종료 체결 → 재진입
        drain(self.bot, self.ex)
        self.assertEqual({o["size"] for o in open_entries(self.ex)}, {"0.0005"})

    def test_restart_keeps_existing_order_size(self):
        drain(self.bot, self.ex)
        grid_open = self.ex.open_orders()
        self.assertEqual(g.open_entry_size(grid_open, D("100000"), D("0.2")), D("0.0005"))
        # 상태 파일 없이 복구: 잔액이 늘었어도 기존 주문 수량(0.0005)으로 계속
        st = g.rebuild_state_from_orders(grid_open, D("100000"), D("0.2"), D("5000"),
                                         g.open_entry_size(grid_open, D("100000"), D("0.2")))
        bot2 = g.GridBot(self.ex, st, CFG, persist=False)
        next(o for o in open_entries(self.ex) if o["price"] == "100200")["status"] = "canceled"
        self.assertEqual(drain(bot2, self.ex), 1)
        self.assertEqual({o["size"] for o in open_entries(self.ex)}, {"0.0005"})

    def test_legacy_state_without_order_size(self):
        st = {k: v for k, v in self.st.items() if k != "order_size"}
        self.assertEqual(g.GridBot(self.ex, st, CFG, persist=False).order_size, D("0.0005"))

    def test_short_fill_close_and_reentry(self):
        drain(self.bot, self.ex)
        self.ex.move("100450")           # L1, L2 숏 체결
        drain(self.bot, self.ex)
        closes = [o for o in self.ex.orders.values() if o["status"] == "open" and "_c_" in o["clientOrderId"]]
        self.assertEqual(sorted((o["side"], o["price"]) for o in closes), [("buy", "100000"), ("buy", "100200")])
        filled_l1 = next(o for o in self.ex.orders.values() if o["price"] == "100200" and "_e_" in o["clientOrderId"])
        self.assertEqual(next(c for c in closes if c["price"] == "100000")["size"], filled_l1["size"])
        self.assertLess(self.ex.position, 0)
        self.ex.move("99900")            # 종료 주문 체결 + L-1 롱 체결
        drain(self.bot, self.ex)
        self.assertEqual(len([o for o in open_entries(self.ex) if o["side"] == "sell"]), 50)  # 숏 재진입
        self.ex.move("100000")           # L-1 롱 종료 체결
        drain(self.bot, self.ex)
        self.assertEqual(self.ex.position, 0)
        self.assertGreater(self.ex.realized, 0)
        self.assertEqual(len(open_entries(self.ex)), 100)

    def test_partial_fill_closes_only_filled_qty(self):
        drain(self.bot, self.ex)
        o = next(o for o in open_entries(self.ex) if o["price"] == "100200")
        o["filledSize"] = "0.0001"
        self.ex.position -= D("0.0001")
        drain(self.bot, self.ex)
        closes = [c for c in self.ex.orders.values() if "_c_" in c["clientOrderId"]]
        self.assertEqual([(c["side"], c["price"], c["size"]) for c in closes], [("buy", "100000", "0.0001")])

    def test_restart_places_only_missing(self):
        drain(self.bot, self.ex)
        self.ex.move("100250")           # L1 숏 체결 (봇이 꺼져있는 동안)
        bot2 = g.GridBot(self.ex, self.st, CFG, persist=False)  # 저장된 상태로 재시작
        placed = drain(bot2, self.ex)
        self.assertEqual(placed, 1)      # 종료 주문 1개만 추가
        self.assertEqual(len(open_entries(self.ex)), 99)

    def test_restart_without_state_file(self):
        drain(self.bot, self.ex)
        grid_open = [o for o in self.ex.open_orders() if g.parse_cid(o["clientOrderId"])]
        st = g.rebuild_state_from_orders(grid_open, D("100000"), D("0.2"), D("1000"), D("0.0005"))
        bot2 = g.GridBot(self.ex, st, CFG, persist=False)
        self.assertEqual(drain(bot2, self.ex), 0)

    def test_lost_response_is_adopted_not_duplicated(self):
        self.ex.fail_next_post = g.NetworkError("timeout")
        drain(self.bot, self.ex)
        self.assertEqual(len(open_entries(self.ex)), 100)

    def test_rejected_order_backs_off(self):
        self.ex.fail_next_post = g.ApiError(400, "insufficient_margin", "x")
        self.bot.check()
        a = self.bot.next_action(0)
        self.bot.execute(a, 0)
        self.assertEqual(len(self.st["orders"]), 0)
        self.assertNotEqual(self.bot.next_action(0)[1], a[1])

    def test_other_market_wti(self):
        g.configure_market("WTI-USD.P")
        ex = FakeExchange("61.37")
        st = g.new_state(D("61.37"), D("1000"), g.calc_order_size(D("1000"), D("61.37"), D("0.01")))
        bot = g.GridBot(ex, st, {"baseIncrement": "0.01", "quoteIncrement": "0.01"}, persist=False)
        self.assertEqual(drain(bot, ex), 100)
        self.assertEqual(bot.price(1), D("61.49"))    # 61.37 × 1.002 = 61.4927
        self.assertEqual(bot.price(-50), D("55.23"))  # 61.37 × 0.9
        self.assertEqual(len({bot.price(l) for l in bot.levels}), 100)
        self.assertEqual({o["size"] for o in open_entries(ex)}, {"0.81"})  # 50 ÷ 61.37 = 0.8147 → 절사
        ex.move("61.50")
        drain(bot, ex)
        self.assertIn(("buy", "61.37"), [(o["side"], o["price"]) for o in ex.orders.values()
                                          if o["status"] == "open" and "_c_" in o["clientOrderId"]])
        g.configure_market("BTC-USD.P")

    def test_normalize_market(self):
        for raw in ("WTI-USD.P", "wti-usd.p", " WTI ", "https://app.ondoperps.xyz/trade/perps/WTI-USD.P",
                    "https://app.ondoperps.xyz/trade/perps/WTI-USD.P/?x=1"):
            self.assertEqual(g.normalize_market(raw), "WTI-USD.P")
        g.configure_market("WTI-USD.P")
        self.assertTrue(g.STATE_FILE.endswith("grid_state_WTI-USD.P.json"))
        g.configure_market("BTC-USD.P")

    def test_leverage_validation(self):
        self.assertEqual(g.market_max_leverage({"marginInfo": [{"maxLeverage": "40"}]}), 40)
        self.assertIsNone(g.market_max_leverage({}))
        self.assertIsNone(g.leverage_problem(10, 40))
        self.assertIsNone(g.leverage_problem(5, 5))
        self.assertIsNotNone(g.leverage_problem(10, 5))   # 마켓 최대 초과
        self.assertIsNotNone(g.leverage_problem(0, 40))

    def test_order_pct_validation(self):
        p = lambda pct, lev, cap="1000": g.order_pct_problem(D(pct), lev, D(cap), D("100000"), D("0.0001"))
        self.assertIsNone(p("5", 10))                     # 5% × 50 ÷ 10 = 25% 증거금
        self.assertIsNone(p("1.8", 1))                    # 90% 딱 맞음
        self.assertIsNotNone(p("2", 1))                   # 100% → 증거금 부족
        self.assertIsNotNone(p("20", 10))                 # 100% → 증거금 부족
        self.assertIsNotNone(p("0", 10))
        self.assertIsNotNone(p("101", 200))
        self.assertIsNotNone(p("0.1", 10))                # 1달러 → 0.0001 BTC 미만
        self.assertEqual(g.margin_use_pct(D("5"), 10), D("25"))

    def test_custom_order_pct_size(self):
        self.assertEqual(g.calc_order_size(D("1000"), D("100000"), D("0.0001"), D("3")), D("0.0003"))
        self.assertEqual(g.calc_order_size(D("1000"), D("100000"), D("0.0001")), D("0.0005"))  # 기본 5%
        st = g.new_state(D("100000"), D("1000"), D("0.0003"), D("0.2"), 7, D("3"))
        self.assertEqual((st["leverage"], st["order_pct"]), (7, "3"))
        bot = g.GridBot(self.ex, st, CFG, persist=False)
        drain(bot, self.ex)
        self.assertEqual({o["size"] for o in open_entries(self.ex)}, {"0.0003"})

    def test_cid_roundtrip(self):
        for lvl in (1, -1, 50, -50):
            cid = g.make_cid(D("83031"), D("0.35"), "close", lvl)
            self.assertLessEqual(len(cid), 64)
            self.assertEqual(g.parse_cid(cid), (D("83031"), D("0.35"), "close", lvl))
        cid = g.make_cid(D("0.0034567"), D("0.15"), "entry", -50)  # 저가 코인도 64자 이하
        self.assertLessEqual(len(cid), 64)
        self.assertEqual(g.parse_cid(cid), (D("0.0034567"), D("0.15"), "entry", -50))
        # 이전 버전 주문 ID → 간격 0.2% 로 인식
        self.assertEqual(g.parse_cid("og_83031_e_p3_19a2b3c4d5e"), (D("83031"), D("0.2"), "entry", 3))
        self.assertIsNone(g.parse_cid("manual-order-1"))

    def test_custom_grid_step(self):
        st = g.new_state(D("100000"), D("1000"), D("0.0005"), D("0.5"))
        bot = g.GridBot(self.ex, st, CFG, persist=False)
        self.assertEqual(drain(bot, self.ex), 100)
        sells = sorted(int(o["price"]) for o in open_entries(self.ex) if o["side"] == "sell")
        buys = sorted(int(o["price"]) for o in open_entries(self.ex) if o["side"] == "buy")
        self.assertEqual((sells[0], sells[-1]), (100500, 125000))   # +0.5% ~ +25%
        self.assertEqual((buys[-1], buys[0]), (99500, 75000))       # -0.5% ~ -25%
        self.ex.move("100600")
        drain(bot, self.ex)
        closes = [(o["side"], o["price"]) for o in self.ex.orders.values()
                  if o["status"] == "open" and "_c_" in o["clientOrderId"]]
        self.assertEqual(closes, [("buy", "100000")])                # 한 칸(0.5%) 아래에서 종료
        # 같은 기준가라도 간격이 다른 그리드 주문은 남의 주문으로 취급
        other = g.GridBot(self.ex, g.new_state(D("100000"), D("1000"), D("0.0005"), D("0.2")), CFG, persist=False)
        other.check()
        self.assertEqual(len(other.st["orders"]), 0)

    def test_grid_step_validation(self):
        p = g.grid_step_problem
        self.assertIsNone(p(D("0.2"), D("100000"), D("1")))
        self.assertIsNone(p(D("1.5"), D("100000"), D("1")))
        self.assertIsNotNone(p(D("0"), D("100000"), D("1")))
        self.assertIsNotNone(p(D("-1"), D("100000"), D("1")))
        self.assertIsNotNone(p(D("2"), D("100000"), D("1")))      # 50칸 × 2% = 100% → 가격 0
        self.assertIsNone(p(D("0.001"), D("100000"), D("1")))     # 정확히 1달러 = 1틱 → 가능
        self.assertIsNotNone(p(D("0.0005"), D("100000"), D("1")))  # 0.5달러 → 1달러 단위보다 좁음
        self.assertIsNotNone(p(D("NaN"), D("100000"), D("1")))

    def test_restart_detects_grid_step_from_orders(self):
        st = g.new_state(D("100000"), D("1000"), D("0.0005"), D("0.3"))
        drain(g.GridBot(self.ex, st, CFG, persist=False), self.ex)
        grid_open = self.ex.open_orders()
        key = g.parse_cid(grid_open[0]["clientOrderId"])[:2]
        self.assertEqual(key, (D("100000"), D("0.3")))
        st2 = g.rebuild_state_from_orders(grid_open, *key, D("1000"), g.open_entry_size(grid_open, *key))
        bot2 = g.GridBot(self.ex, st2, CFG, persist=False)
        self.assertEqual(bot2.step_pct, D("0.3"))
        self.assertEqual(drain(bot2, self.ex), 0)

    def test_legacy_state_without_grid_step(self):
        st = {k: v for k, v in self.st.items() if k != "grid_step_pct"}
        self.assertEqual(g.GridBot(self.ex, st, CFG, persist=False).step_pct, D("0.2"))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
