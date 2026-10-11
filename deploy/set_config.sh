#!/usr/bin/env bash
# 서버 설정 파일 작성 (root 로 실행). 키는 화면에 표시되지 않고 /etc/ondo-grid/<페어>.env (권한 600) 에만 저장됩니다.
#   사용: sudo bash /opt/ondo_grid_bot/deploy/set_config.sh BTC-USD.P
set -euo pipefail

MARKET="${1:?페어를 지정하세요. 예: set_config.sh BTC-USD.P}"
FILE="/etc/ondo-grid/${MARKET}.env"

echo "[$MARKET] 설정 파일: $FILE"
read -rsp "API Key ID (표시 안 됨): " KEY_ID; echo
read -rsp "API Secret (표시 안 됨): " SECRET; echo
case "$KEY_ID" in ondoKeyId_*) ;; *) echo "Key ID 형식 오류 (ondoKeyId_...)"; exit 1;; esac
case "$SECRET" in ondoApiSecret_*) ;; *) echo "Secret 형식 오류 (ondoApiSecret_...)"; exit 1;; esac

echo "아래 3개는 '신규 시작' 때만 사용됩니다 (Enter = 기본값). 기존 그리드를 이어받을 때는 무시됩니다."
read -rp "그리드 간격(%) [0.2]: " STEP
read -rp "배율 [10]: " LEV
read -rp "주문당 잔액 비율(%) [5]: " PCT

umask 077
install -d -m 700 -o root -g root /etc/ondo-grid
cat > "$FILE" <<EOF
ONDO_API_KEY_ID=$KEY_ID
ONDO_API_SECRET=$SECRET
GRID_STEP_PCT=${STEP:-0.2}
LEVERAGE=${LEV:-10}
ORDER_PCT=${PCT:-5}
EOF
chmod 600 "$FILE"
chown root:root "$FILE"
unset KEY_ID SECRET
echo "저장 완료: $FILE (root 만 읽기 가능)"
