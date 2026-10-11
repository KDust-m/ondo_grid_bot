#!/usr/bin/env python3
"""
Ondo Perps 그리드 자동매매 봇 (페어 선택 가능: BTC-USD.P, WTI-USD.P, ...)

- 실행 시 거래 페어와 API Key / Secret 을 입력받습니다 (키는 화면에 표시되지 않음, 디스크에 저장하지 않음).
- 신규 시작 시 그리드 간격(기본 0.2%), 배율(기본 10x), 주문당 잔액 비율(기본 5%)을 입력받습니다.
- 현재가 기준 입력한 간격(기본 0.2%)으로 위 50개(숏), 아래 50개(롱) 지정가 주문을 1초에 1개씩 넣습니다.
- 30초마다 주문을 확인하여, 체결된 수량만큼 한 칸 반대 가격에 지정가 종료 주문을 넣습니다.
- 종료 주문이 전부 체결되면 해당 그리드 레벨에 진입 주문을 다시 넣습니다 (그리드 반복).
- 상태는 grid_state_<페어>.json 에 저장되며, 재시작 시 거래소의 미체결 주문과 대조해 부족한 주문만 추가합니다.

표준 라이브러리만 사용합니다 (Python 3.9+).
"""
from __future__ import annotations

import email.utils
import getpass
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

# ───────────────────────── 고정 설정 (사용자 조작 없음) ─────────────────────────
BASE_URL = "https://api.ondoperps.xyz"
DEFAULT_GRID_STEP_PCT = Decimal("0.2")  # 그리드 간격(%) 기본값 - 신규 시작 시 입력받음
LEVELS_PER_SIDE = 50                # 위/아래 각 50개
DEFAULT_ORDER_PCT = Decimal("5")    # 주문당 잔액 비율(%) 기본값 - 신규 시작 시 입력받음
DEFAULT_LEVERAGE = 10               # 배율 기본값 - 신규 시작 시 입력받음 (마켓 최대 배율 이하)
MAX_MARGIN_USE_PCT = Decimal("90")  # 한쪽 50개 주문이 묶는 증거금 상한 (잔액 대비 %)
ORDER_INTERVAL_SEC = 1.0            # 주문은 1초에 1개
CHECK_INTERVAL_SEC = 30.0           # 30초마다 주문 체크
REJECT_BACKOFF_SEC = 30.0           # 주문 거절 시 해당 레벨 재시도 대기
MIN_REQUEST_GAP_SEC = 0.25          # 모든 API 요청 간 최소 간격
CID_PREFIX = "og"                   # 이 봇이 낸 주문을 식별하는 clientOrderId 접두사
USER_AGENT = "ondo-grid-bot/1.0"    # 기본 Python-urllib UA 는 403 차단됨

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ONDO_DATA_DIR") or APP_DIR   # 상태/로그 저장 위치 (서버: /var/lib/ondo-grid)
CONFIG_ERROR_EXIT = 2                                    # 설정 오류 종료코드 (systemd 가 재시작하지 않음)

# 실행 시 입력받은 페어로 configure_market() 이 설정 (페어마다 상태/로그 파일 분리)
MARKET = ""
STATE_FILE = ""
LOG_FILE = ""

log = logging.getLogger("grid")


def configure_market(market: str) -> None:
    global MARKET, STATE_FILE, LOG_FILE
    MARKET = market
    STATE_FILE = os.path.join(DATA_DIR, f"grid_state_{market}.json")
    LOG_FILE = os.path.join(DATA_DIR, f"grid_bot_{market}.log")


def normalize_market(text: str) -> str:
    """'WTI-USD.P', 'wti', 'https://app.ondoperps.xyz/trade/perps/WTI-USD.P' → 'WTI-USD.P'"""
    s = text.replace("﻿", "").strip().split("?")[0].split("#")[0].rstrip("/")
    s = s.rsplit("/", 1)[-1].upper()
    if s and "-" not in s:
        s += "-USD.P"
    return s


# ───────────────────────── 유틸 ─────────────────────────
def D(x) -> Decimal:
    return Decimal(str(x))


def fmt(d: Decimal) -> str:
    """지수표기 없이 문자열로 변환 (API 전송용)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def floor_to(x: Decimal, inc: Decimal) -> Decimal:
    return (x / inc).to_integral_value(rounding=ROUND_DOWN) * inc


def round_to(x: Decimal, inc: Decimal) -> Decimal:
    return (x / inc).to_integral_value(rounding=ROUND_HALF_UP) * inc


def level_tag(level: int) -> str:
    return f"p{level}" if level > 0 else f"m{-level}"


def make_cid(base: Decimal, step_pct: Decimal, kind: str, level: int) -> str:
    # 영숫자/언더스코어/대시만 허용, 64자 이하
    base_s = fmt(base).replace(".", "d")
    step_s = fmt(step_pct).replace(".", "d")
    nonce = format(time.time_ns() // 1_000_000, "x")
    return f"{CID_PREFIX}_{base_s}_{step_s}_{kind[0]}_{level_tag(level)}_{nonce}"


def parse_cid(cid: str | None):
    """og_<기준가>_<간격%>_<e|c>_<p12|m3>_<nonce>  ->  (base, step_pct, kind, level) 또는 None
    (이전 버전 og_<기준가>_<e|c>_<레벨>_<nonce> 는 간격 0.2% 로 해석)"""
    if not cid:
        return None
    parts = cid.split("_")
    if len(parts) == 5:
        parts.insert(2, "0d2")
    if len(parts) != 6 or parts[0] != CID_PREFIX or parts[3] not in ("e", "c"):
        return None
    try:
        base = Decimal(parts[1].replace("d", "."))
        step_pct = Decimal(parts[2].replace("d", "."))
        tag = parts[4]
        n = int(tag[1:])
        if tag[0] == "p":
            level = n
        elif tag[0] == "m":
            level = -n
        else:
            return None
    except Exception:
        return None
    if level == 0 or abs(level) > LEVELS_PER_SIDE or step_pct <= 0:
        return None
    return base, step_pct, ("entry" if parts[3] == "e" else "close"), level


# ───────────────────────── API 클라이언트 ─────────────────────────
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


class NetworkError(Exception):
    """요청 결과를 알 수 없음 (타임아웃/연결 끊김 등)."""


class OndoClient:
    def __init__(self, key_id: str, secret: str, base_url: str = BASE_URL):
        self._key_id = key_id
        self._secret = secret.encode()
        self._base_url = base_url
        self._offset_ms = 0
        self._last_request = 0.0

    def __repr__(self) -> str:  # 키가 실수로 출력되지 않도록
        return "OndoClient(<redacted>)"

    # 서버 시간 동기화: 허용 범위가 서버 기준 -10초 ~ +1초이므로 약간 과거로 맞춘다.
    def sync_time(self) -> None:
        req = urllib.request.Request(self._base_url + "/status", method="GET", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                date_hdr = resp.headers.get("Date")
        except urllib.error.HTTPError as e:
            date_hdr = e.headers.get("Date")
        except (urllib.error.URLError, OSError) as e:
            log.warning("서버 시간 동기화 실패 (로컬 시간 사용): %s", e)
            return
        if not date_hdr:
            return
        server_ms = int(email.utils.parsedate_to_datetime(date_hdr).timestamp() * 1000) + 500
        self._offset_ms = server_ms - int(time.time() * 1000) - 2000
        log.info("서버 시간 보정: %+d ms", self._offset_ms)

    def _throttle(self) -> None:
        wait = MIN_REQUEST_GAP_SEC - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, auth: bool = True, _retry: int = 0):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        full_path = path + (f"?{query}" if query else "")
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if auth:
            ts = str(int(time.time() * 1000) + self._offset_ms)
            msg = ts + method.upper() + full_path + body_str
            headers["ONDO-KEY-ID"] = self._key_id
            headers["ONDO-TIMESTAMP"] = ts
            headers["ONDO-SIGN"] = hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()

        self._throttle()
        req = urllib.request.Request(self._base_url + full_path, method=method.upper(),
                                     data=body_str.encode() if body_str else None, headers=headers)
        is_get = method.upper() == "GET"
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # GET 은 재시도해도 안전. POST 는 결과를 모르므로 호출자에게 알린다.
            if is_get and _retry < 3:
                time.sleep(2 * (_retry + 1))
                return self._request(method, path, params, body, auth, _retry + 1)
            raise NetworkError(f"{method} {path}: {e}") from e

        try:
            payload = json.loads(raw.decode() or "{}")
        except ValueError:
            payload = {}

        if status == 429 or status >= 500:
            if is_get and _retry < 3:
                time.sleep(3 * (_retry + 1))
                return self._request(method, path, params, body, auth, _retry + 1)
            if status >= 500 and not is_get:
                raise NetworkError(f"{method} {path}: HTTP {status}")
        code = payload.get("error_code") or ""
        if code == "timestamp_too_far" and _retry < 1:
            # 서명 단계에서 거절되므로 POST 재시도도 안전
            self.sync_time()
            return self._request(method, path, params, body, auth, _retry + 1)
        if status >= 400 or not payload.get("success", False):
            raise ApiError(status, code or "unknown", payload.get("error") or raw.decode(errors="replace")[:300])
        return payload

    # ── 엔드포인트 ──
    def perps_markets(self, auth: bool = True) -> list[dict]:
        return self._request("GET", "/v1/markets", auth=auth)["result"]["perps"]["tradingPairs"]

    def market_config(self) -> dict:
        for p in self.perps_markets():
            if p["market"] == MARKET:
                return p
        raise RuntimeError(f"{MARKET} 마켓을 찾을 수 없습니다")

    def mark_price(self) -> Decimal:
        res = self._request("GET", "/v1/perps/mark_prices", auth=False)["result"]
        return D(res[MARKET]["markPrice"])

    def balance(self) -> dict:
        return self._request("GET", "/v1/perps/balance")["result"]

    def position_qty(self) -> Decimal:
        """롱은 +, 숏은 - 로 반환."""
        for p in self._request("GET", "/v1/perps/positions")["result"] or []:
            if p["market"] == MARKET:
                q = abs(D(p["netQuantity"]))
                return -q if p["direction"] == "short" else (q if p["direction"] == "long" else D(0))
        return D(0)

    def get_leverage(self) -> Decimal | None:
        for item in self._request("GET", "/v1/perps/leverage", {"market": MARKET})["result"] or []:
            if item["market"] == MARKET:
                return D(item["leverage"])
        return None

    def set_leverage(self, leverage: int) -> None:
        self._request("POST", "/v1/perps/leverage", body={"market": MARKET, "leverage": str(leverage)})

    def open_orders(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(50):
            payload = self._request("GET", "/v1/perps/orders",
                                    {"market": MARKET, "status": "open", "limit": 100, "cursor": cursor})
            page = payload.get("result") or []
            out.extend(page)
            cursor = (payload.get("pageInfo") or {}).get("nextCursor")
            if not page or not cursor:
                break
        return out

    def get_order(self, cid: str) -> dict:
        return self._request("GET", f"/v1/perps/orders/client:{urllib.parse.quote(cid, safe='')}")["result"]

    def create_limit_order(self, side: str, price: Decimal, size: Decimal, cid: str) -> dict:
        body = {"market": MARKET, "type": "limit", "timeInForce": "GTC", "side": side,
                "price": fmt(price), "size": fmt(size), "clientOrderId": cid}
        return self._request("POST", "/v1/perps/orders", body=body)["result"]


# ───────────────────────── 상태 저장 ─────────────────────────
def load_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("market") != MARKET:
            return None
        return st
    except Exception as e:
        log.warning("상태 파일을 읽을 수 없습니다 (%s) - 무시합니다", e)
        return None


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def archive_state() -> None:
    if os.path.exists(STATE_FILE):
        os.replace(STATE_FILE, STATE_FILE + f".{int(time.time())}.old")


def calc_order_size(capital: Decimal, price: Decimal, size_inc: Decimal,
                    order_pct: Decimal = DEFAULT_ORDER_PCT) -> Decimal:
    """시작 시 1회 계산하는 주문 수량: 잔액 × 비율 ÷ 가격, 수량단위로 절사 (달러가 아닌 수량 기준)."""
    return floor_to(capital * order_pct / 100 / price, size_inc)


def new_state(base: Decimal, capital: Decimal, order_size: Decimal,
              step_pct: Decimal = DEFAULT_GRID_STEP_PCT, leverage: int = DEFAULT_LEVERAGE,
              order_pct: Decimal = DEFAULT_ORDER_PCT) -> dict:
    return {"version": 4, "market": MARKET, "base_price": fmt(base), "grid_step_pct": fmt(step_pct),
            "leverage": int(leverage), "order_pct": fmt(order_pct),
            "capital": fmt(capital), "order_size": fmt(order_size), "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "orders": {}, "need_close": {}}


# ───────────────────────── 그리드 로직 ─────────────────────────
class GridBot:
    """
    레벨 i (±1..±50) 가격 = base × (1 + 간격 × i)   (간격 기본 0.2%)
      i > 0 : 숏 진입(sell) @P(i)  →  종료 buy  @P(i-1)
      i < 0 : 롱 진입(buy)  @P(i)  →  종료 sell @P(i+1)

    state["orders"][cid]   : 이 봇이 추적 중인 주문 (pending/open)
    state["need_close"][i] : 체결됐지만 아직 종료 주문을 내지 못한 수량
    레벨에 진입/종료 주문이 하나도 없고 need_close 도 0 이면 → 진입 주문을 (재)발주.
    """

    def __init__(self, client, state: dict, market_cfg: dict, persist: bool = True):
        self.c = client
        self.st = state
        self.persist = persist
        self.base = D(state["base_price"])
        state.setdefault("grid_step_pct", fmt(DEFAULT_GRID_STEP_PCT))  # 이전 버전 상태 파일
        self.step_pct = D(state["grid_step_pct"])
        self.step = self.step_pct / 100
        self.size_inc = D(market_cfg["baseIncrement"])
        self.price_inc = D(market_cfg["quoteIncrement"])
        if "order_size" not in state:  # 이전 버전 상태 파일
            state["order_size"] = fmt(calc_order_size(D(state["capital"]), self.base, self.size_inc,
                                                      D(state.get("order_pct", DEFAULT_ORDER_PCT))))
        self.order_size = D(state["order_size"])
        self.levels = [i for i in range(-LEVELS_PER_SIDE, LEVELS_PER_SIDE + 1) if i != 0]
        self.backoff: dict[str, float] = {}
        self.last_mark = self.base

    # ── 가격/수량 ──
    def price(self, level: int) -> Decimal:
        return round_to(self.base * (1 + self.step * level), self.price_inc)

    def owns(self, parsed) -> bool:
        """parse_cid 결과가 이 그리드(같은 기준가·간격)의 주문인지."""
        return bool(parsed) and parsed[0] == self.base and parsed[1] == self.step_pct

    def entry_side(self, level: int) -> str:
        return "sell" if level > 0 else "buy"

    def close_side(self, level: int) -> str:
        return "buy" if level > 0 else "sell"

    def close_price(self, level: int) -> Decimal:
        return self.price(level - 1 if level > 0 else level + 1)

    def entry_size(self, level: int) -> Decimal:
        # 모든 레벨 동일 수량 (시작 시 1회 계산, 재시작 시 기존 주문 수량 유지)
        return self.order_size

    # ── 상태 보조 ──
    def save(self) -> None:
        if self.persist:
            save_state(self.st)

    def need_close(self, level: int) -> Decimal:
        return D(self.st["need_close"].get(str(level), "0"))

    def add_need_close(self, level: int, qty: Decimal) -> None:
        v = self.need_close(level) + qty
        if v > 0:
            self.st["need_close"][str(level)] = fmt(v)
        else:
            self.st["need_close"].pop(str(level), None)

    def level_busy(self, level: int) -> bool:
        if self.need_close(level) >= self.size_inc:
            return True
        return any(o["level"] == level for o in self.st["orders"].values())

    def _set_filled(self, cid: str, filled: Decimal) -> None:
        o = self.st["orders"][cid]
        prev = D(o["filled"])
        if filled > prev:
            o["filled"] = fmt(filled)
            label = "진입" if o["kind"] == "entry" else "종료"
            log.info("체결: L%+d %s %s %s @%s (누적 %s/%s)", o["level"], label, o["side"],
                     fmt(filled - prev), o["price"], fmt(filled), o["size"])
        if o["kind"] == "entry":
            delta = D(o["filled"]) - D(o["hedged"])
            if delta > 0:
                self.add_need_close(o["level"], delta)
                o["hedged"] = o["filled"]

    def _remove(self, cid: str) -> None:
        o = self.st["orders"].pop(cid)
        if o["kind"] == "close":
            remaining = D(o["size"]) - D(o["filled"])
            if remaining > 0:
                # 종료 주문이 다 체결되지 못하고 끝남 → 남은 수량 다시 종료 필요
                self.add_need_close(o["level"], remaining)

    # ── 30초마다: 거래소와 대조 ──
    def check(self) -> None:
        try:
            self.last_mark = self.c.mark_price()
        except (ApiError, NetworkError) as e:
            log.warning("현재가 조회 실패: %s", e)

        try:
            open_list = self.c.open_orders()
        except (ApiError, NetworkError) as e:
            log.warning("미체결 주문 조회 실패 (다음 체크 때 재시도): %s", e)
            return

        open_by_cid = {}
        for o in open_list:
            if self.owns(parse_cid(o.get("clientOrderId"))):
                open_by_cid[o["clientOrderId"]] = o

        # 응답 유실 등으로 추적 못한 우리 주문 편입
        for cid, o in open_by_cid.items():
            if cid not in self.st["orders"]:
                _, _, kind, level = parse_cid(cid)
                self.st["orders"][cid] = {"level": level, "kind": kind, "side": o["side"], "price": o["price"],
                                          "size": o["size"], "filled": "0", "hedged": "0", "status": "open",
                                          "order_id": o.get("orderId"), "misses": 0}
                if kind == "close":
                    self.add_need_close(level, -D(o["size"]))
                log.info("미추적 주문 편입: %s", cid)

        for cid in list(self.st["orders"].keys()):
            t = self.st["orders"][cid]
            if cid in open_by_cid:
                o = open_by_cid[cid]
                t["status"] = "open"
                t["order_id"] = o.get("orderId")
                self._set_filled(cid, D(o.get("filledSize") or "0"))
                continue
            try:
                o = self.c.get_order(cid)
            except ApiError as e:
                if e.code == "order_not_found":
                    t["misses"] = t.get("misses", 0) + 1
                    if t["status"] == "pending" or t["misses"] >= 3:
                        log.warning("주문을 거래소에서 찾을 수 없어 추적 해제: %s", cid)
                        self._remove(cid)
                else:
                    log.warning("주문 조회 실패 %s: %s", cid, e)
                continue
            except NetworkError as e:
                log.warning("주문 조회 실패 %s: %s", cid, e)
                continue
            t["order_id"] = o.get("orderId")
            self._set_filled(cid, D(o.get("filledSize") or "0"))
            status = o.get("status")
            if status in ("open", "pending", "untriggered"):
                t["status"] = "open"
                continue
            if status == "canceled":
                log.warning("주문 취소됨 L%+d %s (%s, 사유=%r)", t["level"], t["kind"], cid, o.get("cancelReason"))
            self._remove(cid)

        self.save()
        self._log_summary()

    def _log_summary(self) -> None:
        entries = sum(1 for o in self.st["orders"].values() if o["kind"] == "entry")
        closes = sum(1 for o in self.st["orders"].values() if o["kind"] == "close")
        pending_close = sum((self.need_close(l) for l in self.levels), D(0))
        lo, hi = self.price(-LEVELS_PER_SIDE), self.price(LEVELS_PER_SIDE)
        log.info("현재가 %s | 진입주문 %d | 종료주문 %d | 종료대기수량 %s | 그리드 %s~%s",
                 fmt(round_to(self.last_mark, self.price_inc)), entries, closes, fmt(pending_close),
                 fmt(lo), fmt(hi))
        if not (lo <= self.last_mark <= hi):
            log.warning("현재가가 그리드 범위(±%s%%)를 벗어났습니다. 범위 밖에서는 새 진입이 없습니다.",
                        fmt(self.step_pct * LEVELS_PER_SIDE))

    # ── 다음에 낼 주문 1개 결정 ──
    def next_action(self, now: float):
        # 1) 종료 주문 우선
        for level in self.levels:
            q = floor_to(self.need_close(level), self.size_inc)
            if q >= self.size_inc and self.backoff.get(f"c{level}", 0) <= now:
                return ("close", level, q)
        # 2) 비어있는 레벨에 진입 주문 (현재가에 가까운 순)
        for level in sorted(self.levels, key=lambda l: abs(self.price(l) - self.last_mark)):
            if self.level_busy(level) or self.backoff.get(f"e{level}", 0) > now:
                continue
            size = self.entry_size(level)
            if size >= self.size_inc:
                return ("entry", level, size)
        return None

    def execute(self, action, now: float) -> None:
        kind, level, size = action
        if kind == "entry":
            side, price = self.entry_side(level), self.price(level)
        else:
            side, price = self.close_side(level), self.close_price(level)
        cid = make_cid(self.base, self.step_pct, kind, level)
        self.st["orders"][cid] = {"level": level, "kind": kind, "side": side, "price": fmt(price),
                                  "size": fmt(size), "filled": "0", "hedged": "0", "status": "pending",
                                  "order_id": None, "misses": 0}
        if kind == "close":
            self.add_need_close(level, -size)
        self.save()  # 발주 전에 기록 → 응답 유실/크래시 시에도 다음 체크에서 확인 가능

        label = "진입" if kind == "entry" else "종료"
        try:
            res = self.c.create_limit_order(side, price, size, cid)
        except ApiError as e:
            log.warning("주문 거절 L%+d %s %s %s @%s: %s", level, label, side, fmt(size), fmt(price), e)
            self._remove(cid)
            self.backoff[f"{kind[0]}{level}"] = now + REJECT_BACKOFF_SEC
            self.save()
            return
        except NetworkError as e:
            log.warning("주문 응답 없음 L%+d %s (다음 체크 때 확인): %s", level, label, e)
            return

        t = self.st["orders"][cid]
        t["status"] = "open"
        t["order_id"] = res.get("orderId")
        log.info("주문: L%+d %s %s %s @%s", level, label, side, fmt(size), fmt(price))
        self._set_filled(cid, D(res.get("filledSize") or "0"))
        if res.get("status") in ("fullyfilled", "canceled"):
            self._remove(cid)
        self.save()

    # ── 메인 루프 ──
    def run(self) -> None:
        last_check = float("-inf")
        while True:
            now = time.monotonic()
            if now - last_check >= CHECK_INTERVAL_SEC:
                self.check()
                last_check = time.monotonic()
            action = self.next_action(time.monotonic())
            if action:
                self.execute(action, time.monotonic())
                time.sleep(ORDER_INTERVAL_SEC)
            else:
                time.sleep(1.0)


# ───────────────────────── 시작 / 재시작 판별 ─────────────────────────
def open_entry_size(grid_open: list[dict], base: Decimal, step_pct: Decimal) -> Decimal | None:
    """거래소에 걸려있는 이 그리드의 진입 주문 수량 (가장 많은 값)."""
    sizes = Counter(D(o["size"]) for o in grid_open
                    if (p := parse_cid(o.get("clientOrderId"))) and p[:2] == (base, step_pct) and p[2] == "entry")
    return sizes.most_common(1)[0][0] if sizes else None


def rebuild_state_from_orders(grid_open: list[dict], base: Decimal, step_pct: Decimal, capital: Decimal,
                              order_size: Decimal) -> dict:
    st = new_state(base, capital, order_size, step_pct)
    for o in grid_open:
        parsed = parse_cid(o["clientOrderId"])
        if not parsed or parsed[:2] != (base, step_pct):
            continue
        _, _, kind, level = parsed
        filled = o.get("filledSize") or "0"
        # 상태 파일이 없으므로 이미 체결된 부분은 처리된 것으로 간주
        st["orders"][o["clientOrderId"]] = {"level": level, "kind": kind, "side": o["side"], "price": o["price"],
                                            "size": o["size"], "filled": filled, "hedged": filled,
                                            "status": "open", "order_id": o.get("orderId"), "misses": 0}
    return st


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt_ = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt_)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt_)
    log.addHandler(sh)
    log.addHandler(fh)


def config_error(msg: str):
    """서버 모드 설정 오류: 기록하고 종료 (systemd 는 이 종료코드면 재시작하지 않음)."""
    if log.handlers:
        log.error(msg)
    else:
        print(msg, file=sys.stderr)
    raise SystemExit(CONFIG_ERROR_EXIT)


def env_decimal(name: str, default: Decimal) -> Decimal:
    raw = (os.environ.get(name) or "").strip().rstrip("%").strip()
    try:
        return D(raw) if raw else default
    except Exception:
        config_error(f"설정 {name}={raw!r} 는 숫자가 아닙니다.")


def env_market() -> str:
    """서버 모드: ONDO_MARKET 환경변수의 페어를 검증해서 반환."""
    market = normalize_market(os.environ.get("ONDO_MARKET", ""))
    try:
        markets = {p["market"] for p in OndoClient("", "").perps_markets(auth=False)}
    except (ApiError, NetworkError) as e:
        raise SystemExit(f"마켓 목록을 불러올 수 없습니다 (잠시 후 재시도): {e}")
    if market not in markets:
        config_error(f"설정 ONDO_MARKET={market!r} 페어가 없습니다. 사용 가능: {', '.join(sorted(markets))}")
    return market


def prompt_market() -> str:
    try:
        markets = {p["market"]: p for p in OndoClient("", "").perps_markets(auth=False)}
    except (ApiError, NetworkError) as e:
        raise SystemExit(f"마켓 목록을 불러올 수 없습니다: {e}")
    while True:
        raw = input("거래 페어 입력 (예: BTC-USD.P, WTI-USD.P 또는 거래 화면 URL): ")
        market = normalize_market(raw)
        if market in markets:
            return market
        similar = [m for m in markets if market.split("-")[0] in m] or sorted(markets)
        print(f"'{market}' 페어를 찾을 수 없습니다. 사용 가능: {', '.join(similar)}")


def grid_step_problem(step_pct: Decimal, base: Decimal, price_inc: Decimal) -> str | None:
    """그리드 간격이 쓸 수 없으면 이유를, 괜찮으면 None 을 반환."""
    if not step_pct.is_finite() or step_pct <= 0:
        return "0보다 큰 숫자여야 합니다."
    if step_pct * LEVELS_PER_SIDE >= 100:
        return f"{LEVELS_PER_SIDE}칸 아래 가격이 0 이하가 됩니다. {fmt(D(100) / LEVELS_PER_SIDE)}% 미만으로 입력하세요."
    prices = [round_to(base * (1 + step_pct / 100 * i), price_inc)
              for i in range(-LEVELS_PER_SIDE, LEVELS_PER_SIDE + 1)]
    if len(set(prices)) != len(prices) or min(prices) <= 0:
        return f"가격단위({fmt(price_inc)})보다 간격이 좁아 그리드 가격이 겹칩니다. 더 크게 입력하세요."
    return None


def prompt_grid_step(base: Decimal, cfg: dict) -> Decimal:
    while True:
        raw = input(f"그리드 간격(%) 입력 [Enter = {fmt(DEFAULT_GRID_STEP_PCT)}]: ").replace("﻿", "")
        raw = raw.strip().rstrip("%").strip()
        try:
            step_pct = D(raw) if raw else DEFAULT_GRID_STEP_PCT
        except Exception:
            print("숫자로 입력하세요. 예: 0.2 (= 0.2%)")
            continue
        problem = grid_step_problem(step_pct, base, D(cfg["quoteIncrement"]))
        if problem is None:
            return step_pct
        print(problem)


def market_max_leverage(cfg: dict) -> int | None:
    caps = [int(D(m["maxLeverage"])) for m in cfg.get("marginInfo") or [] if m.get("maxLeverage")]
    return max(caps) if caps else None


def margin_use_pct(order_pct: Decimal, leverage: int) -> Decimal:
    """한쪽(50개) 주문이 모두 걸렸을 때 묶이는 증거금 (잔액 대비 %)."""
    return order_pct * LEVELS_PER_SIDE / leverage


def leverage_problem(leverage: int, max_lev: int | None) -> str | None:
    if leverage < 1:
        return "1 이상의 정수여야 합니다."
    if max_lev is not None and leverage > max_lev:
        return f"{MARKET} 최대 배율은 {max_lev}x 입니다."
    return None


def order_pct_problem(order_pct: Decimal, leverage: int, capital: Decimal, price: Decimal,
                      size_inc: Decimal) -> str | None:
    if not order_pct.is_finite() or order_pct <= 0 or order_pct > 100:
        return "0 초과 100 이하의 숫자여야 합니다."
    use = margin_use_pct(order_pct, leverage)
    if use > MAX_MARGIN_USE_PCT:
        limit = MAX_MARGIN_USE_PCT * leverage / LEVELS_PER_SIDE
        return (f"{leverage}x 에서 한쪽 {LEVELS_PER_SIDE}개 주문에 잔액의 {fmt(round_to(use, D('0.1')))}% 증거금이 "
                f"필요합니다. {fmt(round_to(limit, D('0.01')))}% 이하로 입력하거나 배율을 높이세요.")
    if calc_order_size(capital, price, size_inc, order_pct) < size_inc:
        return f"주문 수량이 최소 단위({fmt(size_inc)}) 미만입니다. 비율을 높이세요."
    return None


def prompt_leverage(max_lev: int | None) -> int:
    default = min(DEFAULT_LEVERAGE, max_lev) if max_lev else DEFAULT_LEVERAGE
    range_s = f"1~{max_lev}" if max_lev else "1 이상"
    while True:
        raw = input(f"배율 입력 ({range_s}) [Enter = {default}]: ").replace("﻿", "").strip().lower().rstrip("x")
        try:
            leverage = int(raw) if raw else default
        except ValueError:
            print("정수로 입력하세요. 예: 10")
            continue
        problem = leverage_problem(leverage, max_lev)
        if problem is None:
            return leverage
        print(problem)


def prompt_order_pct(leverage: int, capital: Decimal, price: Decimal, size_inc: Decimal) -> Decimal:
    while True:
        raw = input(f"주문당 잔액 비율(%) 입력 [Enter = {fmt(DEFAULT_ORDER_PCT)}]: ").replace("﻿", "")
        raw = raw.strip().rstrip("%").strip()
        try:
            order_pct = D(raw) if raw else DEFAULT_ORDER_PCT
        except Exception:
            print("숫자로 입력하세요. 예: 5 (= 5%)")
            continue
        problem = order_pct_problem(order_pct, leverage, capital, price, size_inc)
        if problem is None:
            return order_pct
        print(problem)


def apply_leverage(client: OndoClient, target: int) -> None:
    lev = client.get_leverage()
    if lev == D(target):
        log.info("레버리지 %dx", target)
        return
    try:
        client.set_leverage(target)
        log.info("레버리지 %sx → %dx 설정", fmt(lev) if lev is not None else "?", target)
    except ApiError as e:
        log.warning("레버리지 설정 실패 (현재 %s): %s", lev, e)


def prompt_credentials(headless: bool) -> tuple[str, str]:
    if headless:
        # 서버 모드: 권한 600 설정 파일 → systemd 가 환경변수로 전달. 읽은 뒤 환경변수에서 제거.
        key_id = os.environ.pop("ONDO_API_KEY_ID", "").strip()
        secret = os.environ.pop("ONDO_API_SECRET", "").strip()
    else:
        key_id = getpass.getpass("API Key ID (입력 내용은 표시되지 않습니다): ").strip()
        secret = getpass.getpass("API Secret (입력 내용은 표시되지 않습니다): ").strip()
    if not key_id.startswith("ondoKeyId_") or not secret.startswith("ondoApiSecret_"):
        config_error("키 형식이 올바르지 않습니다 (ondoKeyId_..., ondoApiSecret_...).")
    return key_id, secret


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # 서버 모드: 설정 파일(환경변수)에 키가 있으면 아무것도 묻지 않고 실행
    headless = bool(os.environ.get("ONDO_API_KEY_ID"))
    signal.signal(signal.SIGTERM, _on_sigterm)  # systemctl stop → Ctrl+C 와 동일하게 정리 후 종료
    print("=" * 60)
    print(f" Ondo Perps 그리드 봇  |  위/아래 각 {LEVELS_PER_SIDE}개 주문" + ("  |  서버 모드" if headless else ""))
    print("=" * 60)

    os.makedirs(DATA_DIR, exist_ok=True)
    configure_market(env_market() if headless else prompt_market())
    legacy = os.path.join(APP_DIR, "grid_state.json")  # 이전 버전(BTC 전용) 상태 파일 이전
    if os.path.exists(legacy) and not os.path.exists(STATE_FILE):
        try:
            with open(legacy, encoding="utf-8") as f:
                legacy_market = json.load(f).get("market")
        except Exception:
            legacy_market = None
        if legacy_market == MARKET:
            os.replace(legacy, STATE_FILE)
    setup_logging()
    log.info("거래 페어: %s", MARKET)

    key_id, secret = prompt_credentials(headless)
    client = OndoClient(key_id, secret)
    del secret

    try:
        client.sync_time()
        bal = client.balance()  # 인증 확인
    except ApiError as e:
        if e.status in (401, 403):
            config_error(f"인증 실패: {e}\n키/시크릿, 권한(scope), IP 화이트리스트를 확인하세요.")
        raise SystemExit(f"잔액 조회 실패 (잠시 후 재시도): {e}")
    log.info("인증 성공 | 사용가능 잔액 %s USDC | 마진잔액 %s USDC", bal["availableMargin"], bal["marginBalance"])

    cfg = client.market_config()
    unit = (cfg.get("pair") or {}).get("base") or MARKET.split("-")[0]
    log.info("마켓 %s | 수량단위 %s %s | 가격단위 %s", MARKET, cfg["baseIncrement"], unit, cfg["quoteIncrement"])

    open_list = client.open_orders()
    grid_open = [o for o in open_list if parse_cid(o.get("clientOrderId"))]
    others = len(open_list) - len(grid_open)
    if others:
        log.warning("봇이 내지 않은 %s 미체결 주문 %d개가 있습니다 (건드리지 않음).", MARKET, others)
    position = client.position_qty()
    state = load_state()
    state_active = bool(state and (state.get("orders") or state.get("need_close")))

    if grid_open or (state_active and position != 0):
        # ── 재시작 ──
        grids = Counter(parse_cid(o["clientOrderId"])[:2] for o in grid_open)
        if grids:
            base, step_pct = grids.most_common(1)[0][0]
            if len(grids) > 1:
                log.warning("서로 다른 그리드의 주문이 섞여 있습니다. 기준가 %s / 간격 %s%% 그리드만 운영합니다.",
                            fmt(base), fmt(step_pct))
        else:
            base = D(state["base_price"])
            step_pct = D(state.get("grid_step_pct", DEFAULT_GRID_STEP_PCT))
        size_inc = D(cfg["baseIncrement"])
        existing_size = open_entry_size(grid_open, base, step_pct)  # 이미 걸려있는 주문 수량 우선
        if (state and D(state["base_price"]) == base
                and D(state.get("grid_step_pct", DEFAULT_GRID_STEP_PCT)) == step_pct):
            saved = D(state["order_size"]) if state.get("order_size") else None
            order_size = existing_size or saved or calc_order_size(D(state["capital"]), base, size_inc)
            if saved is not None and order_size != saved:
                log.warning("상태 파일의 주문 수량(%s)과 거래소 주문 수량(%s)이 달라 거래소 기준으로 맞춥니다.",
                            fmt(saved), fmt(order_size))
            state["order_size"] = fmt(order_size)
            state["grid_step_pct"] = fmt(step_pct)
            log.info("재시작 감지: 기존 그리드(기준가 %s, 간격 %s%%)를 이어서 운영합니다. 미체결 그리드 주문 %d개",
                     fmt(base), fmt(step_pct), len(grid_open))
            if state.get("leverage"):
                apply_leverage(client, int(state["leverage"]))  # 처음 입력한 배율 유지
        else:
            capital = D(bal["marginBalance"])
            order_size = existing_size or calc_order_size(capital, base, size_inc)
            log.warning("재시작 감지: 상태 파일이 없거나 일치하지 않아 거래소 주문으로 복구합니다 "
                        "(기준가 %s, 간격 %s%%, 미체결 %d개). 봇 중단 중 체결된 주문은 종료 주문이 누락될 수 있습니다.",
                        fmt(base), fmt(step_pct), len(grid_open))
            current_lev = client.get_leverage()
            state = rebuild_state_from_orders(grid_open, base, step_pct, capital, order_size)
            state["leverage"] = int(current_lev) if current_lev else DEFAULT_LEVERAGE
            log.info("레버리지: 현재 설정(%sx) 유지", fmt(current_lev) if current_lev else "?")
        log.info("주문 수량: %s %s (%s)", fmt(order_size), unit,
                 "기존 주문 수량 유지" if existing_size else "저장된 값")
        save_state(state)
    else:
        # ── 신규 시작 ──
        if position != 0:
            config_error(f"{MARKET} 포지션({fmt(position)})이 있지만 그리드 주문이 없습니다. "
                         "포지션을 정리한 뒤 다시 실행하세요.")
        if state:
            archive_state()
        mark = client.mark_price()
        base = round_to(mark, D(cfg["quoteIncrement"]))
        capital = D(bal["availableMargin"])
        size_inc = D(cfg["baseIncrement"])
        if calc_order_size(capital, base, size_inc, D(100)) < size_inc:
            config_error(f"잔액이 부족합니다: 잔액 전체로도 최소 주문 수량({cfg['baseIncrement']} {unit})이 안 됩니다.")
        max_lev = market_max_leverage(cfg)
        if headless:
            step_pct = env_decimal("GRID_STEP_PCT", DEFAULT_GRID_STEP_PCT)
            lev_raw = env_decimal("LEVERAGE", D(min(DEFAULT_LEVERAGE, max_lev or DEFAULT_LEVERAGE)))
            if lev_raw != lev_raw.to_integral_value():
                config_error(f"설정 LEVERAGE={fmt(lev_raw)} 는 정수여야 합니다.")
            leverage = int(lev_raw)
            order_pct = env_decimal("ORDER_PCT", DEFAULT_ORDER_PCT)
            problem = (grid_step_problem(step_pct, base, D(cfg["quoteIncrement"]))
                       or leverage_problem(leverage, max_lev)
                       or order_pct_problem(order_pct, leverage, capital, base, size_inc))
            if problem:
                config_error(f"설정 오류 (GRID_STEP_PCT={fmt(step_pct)}, LEVERAGE={leverage}, "
                             f"ORDER_PCT={fmt(order_pct)}): {problem}")
        else:
            print(f"사용 가능 잔액: {fmt(capital)} USDC | 현재가: {fmt(base)}")
            step_pct = prompt_grid_step(base, cfg)
            leverage = prompt_leverage(max_lev)
            order_pct = prompt_order_pct(leverage, capital, base, size_inc)
        order_size = calc_order_size(capital, base, size_inc, order_pct)
        state = new_state(base, capital, order_size, step_pct, leverage, order_pct)
        bot = GridBot(client, state, cfg, persist=False)
        say = log.info if headless else print
        say("-" * 60)
        say(f" 신규 시작       : {MARKET}")
        say(f" 기준가          : {fmt(base)}")
        say(f" 그리드 간격     : {fmt(step_pct)}% (범위 ±{fmt(step_pct * LEVELS_PER_SIDE)}%)")
        say(f" 숏 주문 범위    : {fmt(bot.price(1))} ~ {fmt(bot.price(LEVELS_PER_SIDE))} ({LEVELS_PER_SIDE}개)")
        say(f" 롱 주문 범위    : {fmt(bot.price(-1))} ~ {fmt(bot.price(-LEVELS_PER_SIDE))} ({LEVELS_PER_SIDE}개)")
        say(f" 사용 가능 잔액  : {fmt(capital)} USDC")
        say(f" 주문 수량       : {fmt(order_size)} {unit} (모든 주문 동일, 기준가 기준 약 "
            f"{fmt(round_to(order_size * base, D('0.01')))} USDC = 잔액의 {fmt(order_pct)}% 이하)")
        say(f" 레버리지        : {leverage}x (한쪽 주문 전체 증거금 ≈ 잔액의 "
            f"{fmt(round_to(margin_use_pct(order_pct, leverage), D('0.1')))}%)")
        say("-" * 60)
        if not headless and input("위 설정으로 실제 주문을 시작하려면 y 를 입력하세요: ").strip().lower() != "y":
            raise SystemExit("취소했습니다.")
        apply_leverage(client, leverage)
        save_state(state)

    bot = GridBot(client, state, cfg)
    log.info("그리드 운영 시작 (종료: Ctrl+C, 주문은 거래소에 유지됩니다)")
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.save()
        log.info("중지했습니다. 주문은 거래소에 남아 있으며, 다시 실행하면 이어서 운영합니다.")


if __name__ == "__main__":
    main()
