#!/usr/bin/env bash
# Headless screenshots / videos of the Godot client against the mock bridge (or a running bridge).
# Renders offscreen in session 0: Forward+ on Mesa lavapipe (Vulkan) via _dl/godot/lvp, else D3D12 WARP.
#
#   bash godot/tools/shot.sh [options] [-- client args]
#     -o NAME     output name in godot/out (default: shot)            -> out/NAME.png | out/NAME.mp4
#     -t SEC      screenshot SEC seconds after the world is built (default 4)
#     -v SEC      record a video of SEC seconds instead of a screenshot
#     -f FPS      video fps (default 25)
#     -s WxH      render size (default 1600x900)
#     -r          mock without fog of war (--reveal)
#     -R FILE     mock replays a recorded engine snapshot (map/objects/heroes real, mechanics mock),
#                 e.g. -R godot/bridge/testdata/snapshot_newcomers.json
#     -a          run the --auto scenario (select hero, path, move, dialog, end turn)
#     -n          no mock: connect to an already running bridge (BRIDGE_URL, default ws://127.0.0.1:8765)
#     -d          force D3D12 (WARP) instead of lavapipe
#   examples:
#     bash godot/tools/shot.sh -o overview -r -- --cam 18,16 --dist 22
#     bash godot/tools/shot.sh -o auto -a -v 40
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GODOT_DIR="$(cd "$HERE/.." && pwd)"
PRJ="$(cd "$GODOT_DIR/.." && pwd)"
OUT="$GODOT_DIR/out"
NAME=shot; AFTER=4; VIDEO=0; FPS=25; SIZE=1600x900; REVEAL=""; AUTO=0; MOCK=1; DRIVER=auto; REPLAY=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) NAME="$2"; shift 2;;
    -t) AFTER="$2"; shift 2;;
    -v) VIDEO="$2"; shift 2;;
    -f) FPS="$2"; shift 2;;
    -s) SIZE="$2"; shift 2;;
    -r) REVEAL="--reveal"; shift;;
    -R) REPLAY=(--replay "$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"); shift 2;;
    -a) AUTO=1; shift;;
    -n) MOCK=0; shift;;
    -d) DRIVER=d3d12; shift;;
    --) shift; break;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
CLIENT_ARGS=("$@")
mkdir -p "$OUT"

LVP="$PRJ/_dl/godot/lvp/Godot_lvp.exe"
STOCK="$(ls "$PRJ"/_dl/godot/Godot_v4*_win64.exe 2>/dev/null | head -1)"
if [ "$DRIVER" = auto ] && [ -f "$LVP" ]; then
  GODOT="$LVP"; RDRV=vulkan
else
  GODOT="$STOCK"; RDRV=d3d12
fi
FFMPEG="$(ls "$PRJ"/_dl/ffmpeg/*/bin/ffmpeg.exe 2>/dev/null | head -1 || true)"

MOCK_PID=""
cleanup() { [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT
if [ "$MOCK" = 1 ]; then
  PORT=$((18700 + RANDOM % 900))
  URL="ws://127.0.0.1:$PORT"
  uv run --no-project --python 3.12 --with websockets python -I "$GODOT_DIR/bridge/mock_server.py" \
      --port "$PORT" $REVEAL "${REPLAY[@]}" --quiet > "$OUT/.mock_$NAME.log" 2>&1 &
  MOCK_PID=$!
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
    sleep 0.2
  done
else
  URL="${BRIDGE_URL:-ws://127.0.0.1:8765}"
fi

ARGS=(--server "$URL" --size "$SIZE")
[ "$AUTO" = 1 ] && ARGS+=(--auto)
LOG="$OUT/.godot_$NAME.log"
echo "godot: $GODOT ($RDRV), server $URL"
if [ "$VIDEO" = 0 ]; then
  timeout "${TIMEOUT:-300}" "$GODOT" --path "$GODOT_DIR/client" --rendering-driver "$RDRV" --resolution 1024x576 -- \
      "${ARGS[@]}" --shot "$OUT/$NAME.png" --after "$AFTER" "${CLIENT_ARGS[@]}" > "$LOG" 2>&1 || true
  grep -E "^(shot|world|terrain|objects|client|SCRIPT ERROR|ERROR: .*gd)" "$LOG" || true
  [ -f "$OUT/$NAME.png" ] && echo "-> $OUT/$NAME.png"
else
  AVI="$OUT/.$NAME.avi"
  rm -f "$AVI"
  timeout "${TIMEOUT:-1800}" "$GODOT" --path "$GODOT_DIR/client" --rendering-driver "$RDRV" --resolution 1024x576 \
      --write-movie "$AVI" --fixed-fps "$FPS" -- \
      "${ARGS[@]}" --exit-after "$VIDEO" "${CLIENT_ARGS[@]}" > "$LOG" 2>&1 || true
  grep -E "^(world|objects|client|Done recording|SCRIPT ERROR)|frames at" "$LOG" || true
  if [ -n "$FFMPEG" ] && [ -f "$AVI" ]; then
    # drop the first second (window/sky before the snapshot arrives)
    "$FFMPEG" -loglevel error -y -ss 1 -i "$AVI" -c:v libx264 -pix_fmt yuv420p -crf 20 -preset medium \
        -movflags +faststart "$OUT/$NAME.mp4" && rm -f "$AVI" && echo "-> $OUT/$NAME.mp4"
  else
    echo "-> $AVI"
  fi
fi
