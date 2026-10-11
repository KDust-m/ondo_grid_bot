#!/usr/bin/env python3
"""
서버 관리 도구 (root 로 실행, 키는 /etc/ondo-grid/<페어>.env 에서 읽고 출력하지 않음)

  python3 /opt/ondo_grid_bot/deploy/admin.py status WTI-USD.P   # 미체결 주문·포지션·잔액 조회
  python3 /opt/ondo_grid_bot/deploy/admin.py cancel WTI-USD.P   # 이 봇이 낸(og_) 미체결 주문만 취소

cancel 전에는 반드시 봇을 먼저 멈추세요 (systemctl stop ondo-grid@<페어>). 켜져 있으면 봇이 다시 채웁니다.
"""
import os
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ondo_grid_bot as g  # noqa: E402


def client_for(market: str):
    env = {}
    with open(f"/etc/ondo-grid/{market}.env", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                env[k.strip()] = v.strip()
    g.configure_market(market)
    c = g.OndoClient(env["ONDO_API_KEY_ID"], env["ONDO_API_SECRET"])
    c.sync_time()
    return c


def status(c, market: str) -> None:
    orders = c.open_orders()
    groups = Counter()
    for o in orders:
        p = g.parse_cid(o.get("clientOrderId"))
        kind = "봇 주문 없음"
        if p:
            kind = f"봇 {('진입' if p[2] == 'entry' else '종료')} (기준가 {g.fmt(p[0])}, 간격 {g.fmt(p[1])}%, 수량 {o['size']})"
        else:
            kind = f"직접 낸 주문 (수량 {o['size']})"
        groups[kind] += 1
    b = c.balance()
    print(f"[{market}]")
    print(f"  미체결 주문: {len(orders)}개")
    for k, n in groups.items():
        print(f"    - {k}: {n}개")
    print(f"  포지션: {g.fmt(c.position_qty())}")
    print(f"  마진잔액 {b['marginBalance']} | 사용가능 {b['availableMargin']} | 사용중 {b['usedMargin']} USDC")
    print(f"  레버리지: {c.get_leverage()}x")
    svc = subprocess.run(["systemctl", "is-active", f"ondo-grid@{market}"], capture_output=True, text=True).stdout.strip()
    print(f"  봇 서비스: {svc}")


def cancel(c, market: str) -> None:
    svc = subprocess.run(["systemctl", "is-active", f"ondo-grid@{market}"], capture_output=True, text=True).stdout.strip()
    if svc == "active":
        raise SystemExit(f"봇이 실행 중입니다. 먼저 멈추세요: systemctl stop ondo-grid@{market}")
    orders = [o for o in c.open_orders() if g.parse_cid(o.get("clientOrderId"))]
    print(f"[{market}] 봇 미체결 주문 {len(orders)}개 취소")
    ok = fail = 0
    for o in orders:
        try:
            c._request("DELETE", f"/v1/perps/orders/{o['orderId']}")
            ok += 1
        except (g.ApiError, g.NetworkError) as e:
            fail += 1
            print("  실패:", o["clientOrderId"], e)
        time.sleep(0.3)
    print(f"  취소 {ok}개, 실패 {fail}개")
    status(c, market)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("status", "cancel"):
        raise SystemExit(__doc__)
    cmd, market = sys.argv[1], g.normalize_market(sys.argv[2])
    c = client_for(market)
    status(c, market) if cmd == "status" else cancel(c, market)


if __name__ == "__main__":
    main()
