#!/usr/bin/env bash
# Ubuntu 서버 1회 설치 스크립트 (root 로 실행). 코드는 /opt/ondo_grid_bot 에 있어야 합니다.
set -euo pipefail

REPO_DIR=/opt/ondo_grid_bot

apt-get update -qq
apt-get install -y -qq python3 git ufw

# 봇 전용 계정 (로그인 불가)
id ondo &>/dev/null || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin ondo

# 설정 파일 폴더 (root 만 접근)
install -d -m 700 -o root -g root /etc/ondo-grid

# 서비스 등록
install -m 644 "$REPO_DIR/deploy/ondo-grid@.service" /etc/systemd/system/ondo-grid@.service
systemctl daemon-reload

# 로그 시간 한국 기준, 시간 동기화 확인 (API 서명에 시각 사용)
timedatectl set-timezone Asia/Seoul || true
timedatectl set-ntp true || true

# 방화벽: SSH 만 허용 (봇은 바깥으로 나가는 연결만 사용)
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

python3 -B -c "import ast,sys; ast.parse(open('$REPO_DIR/ondo_grid_bot.py',encoding='utf-8').read()); print('python', sys.version.split()[0], 'OK')"
echo "설치 완료. 다음: /etc/ondo-grid/<페어>.env 작성 후 systemctl enable --now ondo-grid@<페어>"
