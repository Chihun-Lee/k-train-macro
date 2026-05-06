#!/bin/bash
# K-Train (SRT + KTX) 매크로 통합 설치 스크립트 (macOS)
# 사용법:
#   curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-train-macro/main/install.sh | bash
set -e

REPO="https://github.com/Chihun-Lee/k-train-macro.git"
INSTALL_DIR="${K_TRAIN_HOME:-$HOME/.k-train-macro}"
APP_DIR="$HOME/Applications"
RUN_APP="$APP_DIR/기차 매크로.app"
QUIT_APP="$APP_DIR/기차 매크로 종료.app"
PORT=8912

echo ""
echo "════════════════════════════════════════"
echo "  K-Train 매크로 (SRT + KTX) 설치"
echo "════════════════════════════════════════"
echo ""

echo "[1/5] Python 3.10+ 확인..."
# 가능한 후보를 순서대로 시도 — 3.13 → 3.10
PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$cand")"
    break
  fi
done

# 아무것도 없으면 기본 python3 가 3.10+ 인지 검사
if [ -z "$PYTHON_BIN" ] && command -v python3 >/dev/null 2>&1; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PYTHON_BIN="$(command -v python3)"
  fi
fi

# 그래도 없으면 Homebrew로 자동 설치
if [ -z "$PYTHON_BIN" ]; then
  echo "  → Python 3.10 이상이 없습니다. Homebrew로 자동 설치합니다."
  if ! command -v brew >/dev/null 2>&1; then
    echo "  → Homebrew도 없으므로 먼저 설치합니다 (5~10분 소요, 비밀번호 1회 입력)."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # PATH에 brew 추가 (Apple Silicon)
    if [ -x /opt/homebrew/bin/brew ]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi
  echo "  → brew install python@3.12 (1~3분)"
  brew install python@3.12 >/dev/null
  for cand in python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  done
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "  ✗ Python 3.10+ 자동 설치 실패. 수동으로 설치 후 다시 시도하세요:"
  echo "    https://www.python.org/downloads/macos/"
  read -p "  엔터 키를 누르면 창이 닫힙니다..."
  exit 1
fi
PY_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  ✓ Python $PY_VER ($PYTHON_BIN)"

echo "[2/5] 코드 다운로드..."
mkdir -p "$APP_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --quiet
  git -C "$INSTALL_DIR" reset --hard origin/main --quiet
else
  if [ -d "$INSTALL_DIR" ]; then rm -rf "$INSTALL_DIR"; fi
  git clone --quiet "$REPO" "$INSTALL_DIR"
fi
echo "  ✓ $INSTALL_DIR"

echo "[3/5] Python 환경 구성 (1~3분 소요)..."
# venv가 이미 있고 python 버전이 너무 낮으면 재생성
if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
  if ! "$INSTALL_DIR/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "  → 기존 venv 가 3.10 미만 → 재생성"
    rm -rf "$INSTALL_DIR/venv"
  fi
fi
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "  ✓ 의존성 설치 완료"

echo "[4/5] 앱 번들 생성..."
rm -rf "$RUN_APP" "$QUIT_APP"

mkdir -p "$RUN_APP/Contents/MacOS"
cat > "$RUN_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>기차 매크로</string>
  <key>CFBundleDisplayName</key><string>기차 매크로</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-train-macro</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>k-train-macro</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$RUN_APP/Contents/MacOS/k-train-macro" <<EOF
#!/bin/bash
INSTALL_DIR="$INSTALL_DIR"
PORT=$PORT
LOG="/tmp/k-train-macro.log"

EXISTING=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$EXISTING" ]; then
  kill \$EXISTING 2>/dev/null
  sleep 1
fi

cd "\$INSTALL_DIR"
nohup "\$INSTALL_DIR/venv/bin/python" server.py > "\$LOG" 2>&1 &

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS http://127.0.0.1:\$PORT/api/srt/config/status > /dev/null 2>&1; then
    open "http://127.0.0.1:\$PORT"
    osascript -e 'display notification "SRT + KTX 탭으로 사용하세요." with title "기차 매크로 시작됨" sound name "Glass"'
    exit 0
  fi
  sleep 1
done

osascript -e 'display alert "기차 매크로 시작 실패" message "로그: /tmp/k-train-macro.log" as critical'
EOF
chmod +x "$RUN_APP/Contents/MacOS/k-train-macro"

mkdir -p "$QUIT_APP/Contents/MacOS"
cat > "$QUIT_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>기차 매크로 종료</string>
  <key>CFBundleDisplayName</key><string>기차 매크로 종료</string>
  <key>CFBundleIdentifier</key><string>com.chihunlee.k-train-macro-quit</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>quit</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

cat > "$QUIT_APP/Contents/MacOS/quit" <<EOF
#!/bin/bash
PORT=$PORT
PIDS=\$(lsof -ti tcp:\$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "\$PIDS" ]; then
  kill \$PIDS
  osascript -e 'display notification "기차 매크로 종료됨" with title "기차 매크로" sound name "Pop"'
else
  osascript -e 'display notification "이미 종료된 상태입니다" with title "기차 매크로"'
fi
EOF
chmod +x "$QUIT_APP/Contents/MacOS/quit"

xattr -dr com.apple.quarantine "$RUN_APP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$QUIT_APP" 2>/dev/null || true

echo "  ✓ $RUN_APP"
echo "  ✓ $QUIT_APP"

echo "[5/5] 완료!"
echo ""
echo "════════════════════════════════════════"
echo "  ✅ 설치 완료"
echo "════════════════════════════════════════"
echo ""
echo "  사용법:"
echo "    1. Launchpad → '기차 매크로' 검색 → 더블클릭"
echo "    2. 브라우저가 자동으로 열림 (SRT / KTX 탭)"
echo "    3. 두 탭 동시 사용 가능"
echo ""
echo "  종료: Launchpad → '기차 매크로 종료' 더블클릭"
echo ""

read -p "  지금 바로 실행할까요? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
  open "$RUN_APP"
fi
