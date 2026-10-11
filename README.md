# Ondo Perps 그리드 봇

Ondo Perps 무기한 선물(BTC-USD.P, WTI-USD.P 등)에서 현재가 위·아래로 지정가 주문을 촘촘히 깔아 두고,
체결되면 한 칸 반대 가격에 종료 주문을 내는 그리드 자동매매 봇입니다.
Python 3.9+ 표준 라이브러리만 사용합니다 (설치할 패키지 없음).

## 동작
| 항목 | 값 |
|---|---|
| 마켓 | 페어 1개 (PC: 실행 시 입력 / 서버: 서비스 이름) |
| 그리드 | 기준가 × (1 ± 간격 × n), n = 1..50 → 위 숏 50개 / 아래 롱 50개, 범위 ±간격×50 |
| 그리드 간격 | 신규 시작 시 지정 (기본 0.2%, 2% 미만, 가격단위보다 넓어야 함) |
| 레버리지 | 신규 시작 시 지정 (기본 10x, 마켓 최대 배율 이하) |
| 주문 비율 | 신규 시작 시 지정 (기본 5%). 한쪽 50개 주문 증거금(비율×50÷배율)이 잔액의 90%를 넘으면 거부 |
| 주문 수량 | 시작 시 1회: 사용 가능 잔액 × 비율 ÷ 시작 가격, 수량단위로 절사. 모든 주문·재진입에 동일 수량 |
| 발주 속도 | 1초에 1건 |
| 체결 확인 | 30초마다. 체결된 수량만큼만 한 칸 반대 가격에 지정가 종료 주문 (부분 체결 포함) |
| 반복 | 종료 주문이 다 체결되면 그 레벨에 진입 주문 재발주 |
| 범위 이탈 | 가격이 ±범위를 벗어나면 경고만, 그쪽 새 진입은 없음 |

### 신규 시작 / 재시작 판별
- 봇 주문은 `clientOrderId` 가 `og_<기준가>_<간격>_<e|c>_<레벨>_<nonce>` 형식이며, 그 외 주문은 건드리지 않습니다.
- 거래소에 봇 주문이 남아 있으면 **재시작**: 기준가·간격·주문 수량을 기존 주문에서 읽어 그대로 이어가고, 빠진 주문만 추가합니다.
  봇이 꺼져 있던 동안 체결된 주문은 상태 파일과 대조해 종료 주문을 냅니다.
- 봇 주문이 없고 포지션도 0이면 **신규 시작**: 현재가를 기준가로 새 그리드를 만듭니다.
- 봇 주문은 없는데 포지션만 있으면 원인을 알 수 없으므로 시작하지 않습니다.

## PC에서 실행 (Windows)
```
cd "$env:USERPROFILE\OneDrive\Desktop\ondo_grid_bot"
py ondo_grid_bot.py
```
1. 거래 페어 입력 — `WTI-USD.P`, `wti`, 거래 화면 URL 모두 가능
2. API Key ID / Secret 입력 (화면에 표시되지 않으며 파일·로그에 저장되지 않음)
3. 신규 시작이면 간격·배율·비율을 입력받고, 요약을 보여준 뒤 `y` 확인 후 주문 시작
4. 중지: `Ctrl+C` — 거래소의 주문은 그대로 유지되며, 다시 실행하면 이어서 운영

상태·로그는 실행 폴더의 `grid_state_<페어>.json`, `grid_bot_<페어>.log` 에 저장됩니다.

## 서버(VPS) 운영 — Ubuntu 24.04
환경변수 `ONDO_API_KEY_ID` 가 있으면 **서버 모드**: 아무것도 묻지 않고 설정 파일 값으로 실행합니다.

### 최초 설치
```
git clone <repo> /opt/ondo_grid_bot          # 비공개 저장소: 읽기 전용 deploy key 사용 권장
bash /opt/ondo_grid_bot/deploy/install.sh     # python3·전용 계정(ondo)·systemd 서비스·방화벽·시간대 설정
bash /opt/ondo_grid_bot/deploy/set_config.sh WTI-USD.P   # API 키(표시 안 됨)·간격·배율·비율 입력
systemctl enable --now ondo-grid@WTI-USD.P
```
- 설정 파일: `/etc/ondo-grid/<페어>.env` (권한 600, root 전용). 형식은 `deploy/config.example.env` 참고.
- 간격·배율·비율(`GRID_STEP_PCT`, `LEVERAGE`, `ORDER_PCT`)은 **신규 시작 때만** 사용됩니다.
- 상태·로그: `/var/lib/ondo-grid/`
- 서버 재부팅·오류 시 30초 뒤 자동 재시작. 설정 오류(키 틀림, 잘못된 값, 포지션만 남음)는 종료코드 2로 끝나며 재시작하지 않습니다.

### 일상 관리
```
python3 /opt/ondo_grid_bot/deploy/admin.py status WTI-USD.P   # 미체결 주문·포지션·잔액·서비스 상태
journalctl -u ondo-grid@WTI-USD.P -f                          # 실시간 로그
systemctl stop  ondo-grid@WTI-USD.P                           # 중지 (주문은 거래소에 유지)
systemctl start ondo-grid@WTI-USD.P                           # 시작 (이어서 운영)
python3 /opt/ondo_grid_bot/deploy/admin.py cancel WTI-USD.P   # 봇 주문만 취소 (봇이 꺼져 있을 때만 동작)
```

### 설정 변경 (간격·배율·비율)
기존 그리드가 있으면 재시작으로 인식해 새 설정이 적용되지 않으므로, 그리드를 비우고 신규 시작합니다.
1. `systemctl stop ondo-grid@<페어>`
2. 웹에서 전체 취소, 또는 `admin.py cancel <페어>` (포지션이 있으면 먼저 정리)
3. `/etc/ondo-grid/<페어>.env` 의 값 수정 (또는 `set_config.sh` 다시 실행)
4. `systemctl start ondo-grid@<페어>`

### 코드 업데이트
```
git -C /opt/ondo_grid_bot pull && systemctl restart ondo-grid@WTI-USD.P
```

### 주의
- **봇이 켜져 있을 때 웹에서 봇 주문을 취소하지 마세요.** 비어 있는 레벨로 보고 30초 안에 다시 채웁니다.
- **같은 페어를 PC와 서버에서 동시에 돌리지 마세요.**
- 주식·원자재 페어(WTI 등)는 주말(금 16:00 ~ 일 20:00 ET)에 가격 범위가 제한되고, 재개 시 가격이 점프하면 여러 주문이 한꺼번에 체결될 수 있습니다.

## 보안
- API 키는 저장소에 절대 커밋하지 않습니다. PC는 실행할 때 입력, 서버는 root 전용 설정 파일에만 보관합니다.
- 거래소 API 키는 **출금 권한 없이** 거래 권한만 주고, **IP 화이트리스트**에 서버 IP만 등록하세요.
- 서버는 SSH 키 로그인만 허용(비밀번호 로그인 차단), 방화벽은 SSH만 허용, 봇은 권한 없는 전용 계정으로 실행됩니다.

## 파일 구성
| 파일 | 설명 |
|---|---|
| `ondo_grid_bot.py` | 봇 본체 |
| `test_grid_logic.py` | 가짜 거래소로 그리드 로직 검증 (실제 주문 없음): `py test_grid_logic.py` |
| `deploy/install.sh` | 서버 1회 설치 |
| `deploy/set_config.sh` | 서버 설정 파일 작성 (키 입력) |
| `deploy/admin.py` | 서버 상태 조회 / 봇 주문 취소 |
| `deploy/ondo-grid@.service` | systemd 서비스 (페어별 인스턴스) |
| `deploy/config.example.env` | 설정 파일 예시 |
