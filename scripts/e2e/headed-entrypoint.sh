#!/bin/sh
# E2E 目視確認モードの起動（D-05）。Xvfb -> fluxbox -> x11vnc -> noVNC を起動してから
# 引数（pytest）を実行する。公開するのは noVNC の 6080 のみ（compose で 127.0.0.1 に限定）。
# 根拠: plan/archive/proposals/e2e-headed-vnc.md §5.2。
set -eu

export DISPLAY=:99
RESOLUTION="${HEADED_RESOLUTION:-1440x900x24}"

# 1. 仮想ディスプレイ（TCP は閉じる）
Xvfb "$DISPLAY" -screen 0 "$RESOLUTION" -nolisten tcp >/tmp/xvfb.log 2>&1 &
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i+1)); done
[ -e /tmp/.X11-unix/X99 ] || { echo "Xvfb did not start" >&2; cat /tmp/xvfb.log >&2; exit 1; }

# 2. ウィンドウマネージャ（無いとウィンドウ位置・サイズが不定）
fluxbox >/tmp/fluxbox.log 2>&1 &

# 3. VNC（コンテナ内ローカルのみ）
x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 \
  -localhost -nopw -noxdamage -quiet >/tmp/x11vnc.log 2>&1 &

# 4. noVNC（ホストへ公開するのはこの 6080 のみ）
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &

exec "$@"
