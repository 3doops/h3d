#!/usr/bin/env bash
# End-to-end run of the real stack through the client (PLAN 4.1): bridge.py starts the original game hidden, the
# client plays its --auto scenario against it, every step is decided by the engine.
#
#   bash godot/tools/e2e.sh [main|battle] [-o DIR] [-v FPS] [-- extra client args]
#     main    GODOT_TEST.GM1: snapshot -> engine path -> hero picks up a resource (HUD +10 wood) -> second hero opens
#             a chest, the engine's dialog is answered in the client -> the town through the HUD button, 12 skeletons
#             recruited by clicks in the engine's town window -> end turn ("heroes can still move" -> yes) -> AI turns
#             -> day 2
#     battle  GODOT_BATTLE.GM1, bridge --battles manual: the hero attacks the monster next to him -> the engine's
#             battle screen in the client -> click "auto-combat" -> result, monster gone
#     menu    bridge --menu (PLAN 4.5): the game's main menu fullscreen in the client -> clicks New Game -> Single ->
#             The Newcomers -> Begin -> intro OK -> 3D world of the new map -> HUD "Меню игры" -> system options ->
#             Main menu -> confirm -> the menu screen again (shots <OUT>/menu_*.jpg, checks via --menu-demo)
#     -o DIR  outputs (default godot/out/final): <run>_*.jpg (scenario steps), <run>_town*/battle_* (engine windows),
#             client_report_<run>.json (checks + protocol timeline), bridge_log_<run>.txt
#     -v FPS  also record a video: Godot --write-movie at FPS, cut at the first frame with the world, -> <run>.mp4
#             (H.264 1280x720, max 60 s). On the CPU-only VM: ~3 s per frame, i.e. ~1 h for 55 s at 20 fps.
# Needs: the game in game/Heroes3_HotA1, Python 3.12 with frida + websockets, Godot 4.7 in _dl/godot (lavapipe
# shim _dl/godot/lvp for session 0), ffmpeg in _dl/ffmpeg for -v. Kills h3hota.exe at the end (one game per VM).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GODOT_DIR="$(cd "$HERE/.." && pwd)"
PRJ="$(cd "$GODOT_DIR/.." && pwd)"
RUN=main; OUT="$GODOT_DIR/out/final"; FPS=0
if [ $# -gt 0 ] && [[ "$1" != -* ]]; then RUN="$1"; shift; fi
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2;;
    -v) FPS="$2"; shift 2;;
    --) shift; break;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
EXTRA=("$@")
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -W 2>/dev/null || pwd)"     # Windows path for Godot (C:/...)
OUTU="$(cd "$OUT" && pwd)"

PY="${PYTHON:-$(cygpath "${LOCALAPPDATA:-C:/Users/$USERNAME/AppData/Local}")/Programs/Python/Python312/python.exe}"
PORT="${PORT:-8765}"
LVP="$PRJ/_dl/godot/lvp/Godot_lvp.exe"
STOCK="$(ls "$PRJ"/_dl/godot/Godot_v4*_win64.exe 2>/dev/null | head -1)"
if [ -f "$LVP" ]; then GODOT="$LVP"; RDRV=vulkan; else GODOT="$STOCK"; RDRV=d3d12; fi
FFMPEG="$(ls "$PRJ"/_dl/ffmpeg/*/bin/ffmpeg.exe 2>/dev/null | head -1 || true)"

case "$RUN" in
  main)
    BRIDGE_ARGS=(--load GODOT_TEST.GM1)
    # town 2 = "Вечная Тьма" (Necropolis); engine coordinates 800x600: skeleton dwelling, "max", "recruit", exit
    AUTO=(--auto-wdemo "hudtown:2;frame;wait:3;shot:$OUT/main_town.jpg;click:170,265;frame;wait:2.5;click:309,345;wait:1.5;shot:$OUT/main_recruit.jpg;click:387,345;wait:2.5;shot:$OUT/main_recruited.jpg;click:771,563;close;wait:2;shot:$OUT/main_town_closed.jpg;select:70;wait:1.5")
    ;;
  battle)
    BRIDGE_ARGS=(--load GODOT_BATTLE.GM1 --battles manual)
    # the engine's auto-combat button (167,577); the faster enemy acts first, click_close repeats the click if needed
    AUTO=(--auto-target monster --auto-no-far --auto-no-end-turn
          --auto-wdemo "open;frame;wait:3;shot:$OUT/battle_window.jpg;wait:4;click_close:167,577;wait:4;shot:$OUT/battle_after.jpg")
    ;;
  menu)
    BRIDGE_ARGS=(--menu)
    # engine coordinates 800x600 of the HotA 1.8.0 menus (bridge/test_client.py --scenario menu uses the same)
    MENU="wait:2;frame;wait:1;shot:$OUT/menu_01_main.jpg;check:menu;click:650,82;wait:1.5;click:650,82;wait:6;frame;shot:$OUT/menu_02_scenario.jpg"
    MENU+=";click:512,89;wait:2;click:41,68;wait:1.5;click:215,359;wait:1.5;frame;shot:$OUT/menu_03_newcomers.jpg"
    MENU+=";click:495,553;wait:8;frame;shot:$OUT/menu_04_intro.jpg;clickuntil:adventure:400,412;world:3;shot:$OUT/menu_05_world.jpg;check:world"
    MENU+=";sysmenu;frame;wait:2;shot:$OUT/menu_06_system_options.jpg;check:sysmenu;click:565,433;wait:2;click:358,349"
    MENU+=";inphase:menu;wait:3;frame;shot:$OUT/menu_07_back_to_menu.jpg;check:back;quit"
    CLIENT=(--menu-demo "$MENU" --auto-report "$OUT/client_report_menu.json")
    ;;
  *) echo "run: main | battle | menu" >&2; exit 2;;
esac
[ "$RUN" = menu ] || CLIENT=(--auto --auto-exit 3 --auto-shots "$OUT/$RUN" --auto-ext jpg
                             --auto-report "$OUT/client_report_$RUN.json" "${AUTO[@]}")

BLOG="$OUTU/bridge_log_$RUN.txt"
BPID=""
cleanup() {
  [ -n "$BPID" ] && kill "$BPID" 2>/dev/null || true
  taskkill //F //IM h3hota.exe > /dev/null 2>&1 || true
}
trap cleanup EXIT
"$PY" -I -u "$GODOT_DIR/bridge/bridge.py" --port "$PORT" --dialog-timeout 3600 "${BRIDGE_ARGS[@]}" > "$BLOG" 2>&1 &
BPID=$!
for _ in $(seq 1 120); do
  grep -q "^bridge on" "$BLOG" 2>/dev/null && break
  kill -0 "$BPID" 2>/dev/null || { echo "bridge.py exited, see $BLOG" >&2; exit 1; }
  sleep 1
done
grep -q "^bridge on" "$BLOG" || { echo "bridge did not come up, see $BLOG" >&2; exit 1; }
echo "bridge up ($RUN), godot: $GODOT ($RDRV)"

MOVIE=()
AVI="$OUTU/.$RUN.avi"
if [ "$FPS" != 0 ]; then rm -f "$AVI"; MOVIE=(--write-movie "$AVI" --fixed-fps "$FPS"); fi
GLOG="$OUTU/.godot_$RUN.log"
timeout "${TIMEOUT:-10800}" "$GODOT" --path "$GODOT_DIR/client" --rendering-driver "$RDRV" --resolution 1024x576 \
    "${MOVIE[@]}" -- --server "ws://127.0.0.1:$PORT" --size 1280x720 --fast "${CLIENT[@]}" "${EXTRA[@]}" \
    > "$GLOG" 2>&1 || true
grep -E "^(auto check|SCRIPT ERROR)" "$GLOG" || true

if [ "$FPS" != 0 ] && [ -f "$AVI" ]; then
  F0="$(grep -o "world ready at frame [0-9]*" "$GLOG" | head -1 | grep -o "[0-9]*$" || echo 0)"
  SS="$(awk -v f="${F0:-0}" -v r="$FPS" 'BEGIN { printf "%.2f", f / r }')"
  if [ -n "$FFMPEG" ]; then
    "$FFMPEG" -loglevel error -y -ss "$SS" -i "$AVI" -an -t 60 \
        -vf "scale=1280:720:out_range=tv,format=yuv420p" -color_range tv -c:v libx264 -crf 21 -preset medium -movflags +faststart "$OUTU/$RUN.mp4" && rm -f "$AVI" && echo "-> $OUTU/$RUN.mp4 (from ${SS}s)"
  else
    echo "no ffmpeg in _dl/ffmpeg: video left as $AVI"
  fi
fi
FAILS="$(grep -c "^auto check \[FAIL\]" "$GLOG" || true)"
echo "checks: $(grep -c "^auto check \[PASS\]" "$GLOG" || true) passed, $FAILS failed; report $OUT/client_report_$RUN.json"
[ "$FAILS" = 0 ]
