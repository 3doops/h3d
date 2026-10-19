#!/usr/bin/env bash
# Export of the Godot client for the player's PC (platform = first argument, default windows):
#   windows -> dist/HotA3D-client-win64.zip
#              (inside: HotA3D-client/ HotA3D.exe + HotA3D.pck + HotA3D.console.exe + README.txt + bridge/)
#   linux   -> dist/HotA3D-client-linux-x86_64.tar.gz
#              (inside: HotA3D-client-linux-x86_64/ HotA3D.x86_64 + run.sh + doctor.sh (0755) + HotA3D.pck +
#              README.txt + bridge/ + python/; the binary is checked to be ELF x86-64)
# bridge/ = the Python bridge for the local mode (the client starts game + bridge itself): *.py sources without
# tests, the Frida agents *.js, requirements.txt, README.md, testdata/*.GM1 (test saves) + the recorded snapshot.
# python/ = portable Windows Python for the bridge (make_bridge_python.py: python.org embeddable 3.12 + frida,
# websockets, Pillow from _dl/bridge-python/), so neither package needs a Python install (Linux: only Wine).
# Both packages are checked: pck holds the models (GLB names in its file table) + manifest, python/python.exe there.
# Disk is tight: by default only the package is kept, the unpacked dist/<folder>/ is deleted after it is checked.
#
#   bash godot/tools/export_client.sh               # Windows: export -> zip -> check -> delete the folder
#   bash godot/tools/export_client.sh linux         # Linux x86_64: export -> tar.gz -> check -> delete the folder
#   bash godot/tools/export_client.sh --test        # (Windows only) + before zipping, run the exported exe here (Mesa
#                                                   #   lavapipe, like shot.sh) against bridge.py --mock --replay:
#                                                   #   godot/out/export_test.png, godot/out/export_connect.png (panel)
#   bash godot/tools/export_client.sh [linux] --keep-dir    # keep the unpacked folder too
#   bash godot/tools/export_client.sh [linux] --no-zip      # export only (keeps the folder)
#   bash godot/tools/export_client.sh [linux] --zip-only    # package an existing folder (-> check -> delete it)
#   bash godot/tools/export_client.sh --test-only   # (Windows only) test the existing folder or zip
#   bash godot/tools/export_client.sh linux --pck-from-win  # low disk: no Godot export; HotA3D.pck and python/ are
#                                                   #   streamed from dist/HotA3D-client-win64.zip into the tar.gz
#                                                   #   (the pck is platform-independent: both presets are identical,
#                                                   #   the Linux binary is the release template as is)
#
# The client loads GLBs, manifest.json and terrain JPEGs at runtime as raw files (GLTFDocument, Image.load_from_file,
# FileAccess), so they must be in the .pck verbatim, not as imported scenes/textures. The export runs on a staging
# copy (_work/export-client: code copied, assets hard-linked) where every asset gets an `importer="keep"` .import
# sidecar: the editor neither imports ~340 MB of GLBs nor converts them, the export copies them as they are, and
# godot/client itself (its .godot/ cache, its .gdignore files) is not touched.
# Export templates: _dl/godot-templates-<ver>/ (godot/tools/fetch_export_templates.py --platform P streams only the
# files of that platform out of the official .tpz, run automatically if missing); TEMPLATES=dir overrides.
# Runs on Windows (Git Bash) and on Linux (CI: .github/workflows/release.yml). Paths default to the h3-regress layout
# (this repo = its godot/), the environment overrides them:
#   DIST, WORK          output and staging dirs (default <parent>/dist, <parent>/_work)
#   DL                  downloads: Godot, templates, bridge-python (default <parent>/_dl)
#   GODOT, GODOT_VERSION  Godot editor binary and its version (default: _dl/godot/Godot_v<ver>-stable_win64_console.exe)
#   VERSION             client version: project config/version + VERSION.txt in the package
#   PYTHON              host Python 3 (default python)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GODOT_DIR="$(cd "$HERE/.." && pwd)"
PRJ="$(cd "$GODOT_DIR/.." && pwd)"
CLIENT="$GODOT_DIR/client"
NAME=HotA3D
DIST="${DIST:-$PRJ/dist}"
WORK="${WORK:-$PRJ/_work}"
DL="${DL:-$PRJ/_dl}"
STAGE="$WORK/export-client"
TEST="$WORK/export-test"
OUT="$GODOT_DIR/out"
PY="${PYTHON:-python}"
VERSION="${VERSION:-}"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) ON_WIN=1;; *) ON_WIN=0;; esac

PLATFORM=windows; DO_EXPORT=1; DO_ZIP=1; DO_TEST=0; KEEP_DIR=0; REUSE=""
for a in "$@"; do
  case "$a" in
    windows|win|win64) PLATFORM=windows;;
    linux) PLATFORM=linux;;
    --test) DO_TEST=1;;
    --keep-dir) KEEP_DIR=1;;
    --no-zip) DO_ZIP=0; KEEP_DIR=1;;
    --zip-only) DO_EXPORT=0;;
    --test-only) DO_EXPORT=0; DO_ZIP=0; DO_TEST=1; KEEP_DIR=1;;
    --pck-from-win) REUSE="$DIST/HotA3D-client-win64.zip";;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0;;
    *) echo "unknown option $a" >&2; exit 2;;
  esac
done
case "$PLATFORM" in
  windows) PRESET="Windows Desktop"; TPL_FILE=windows_release_x86_64.exe; BIN="$NAME.exe"
           OUTDIR="$DIST/HotA3D-client"; PKG="$DIST/HotA3D-client-win64.zip"; LOG="$DIST/.export_client.log";;
  linux)   PRESET="Linux"; TPL_FILE=linux_release.x86_64; BIN="$NAME.x86_64"
           OUTDIR="$DIST/HotA3D-client-linux-x86_64"; PKG="$DIST/HotA3D-client-linux-x86_64.tar.gz"
           LOG="$DIST/.export_client_linux.log";;
esac
if [ "$PLATFORM" = linux ] && [ "$DO_TEST" = 1 ]; then
  echo "--test runs the export here (Windows + lavapipe): Windows only; run the Linux build on Linux (README.txt)" >&2
  exit 2
fi

GODOT="${GODOT:-$(ls "$DL"/godot/Godot_v4*-stable_win64_console.exe 2>/dev/null | head -1)}"
[ -n "$GODOT" ] && [ -f "$GODOT" ] || { echo "no Godot: put it in $DL/godot or set GODOT" >&2; exit 1; }
VER="${GODOT_VERSION:-$(basename "$GODOT" | sed -E 's/^Godot_v([0-9.]+)-stable.*/\1/')}"
TPL="${TEMPLATES:-$DL/godot-templates-$VER}"
win() { if [ "$ON_WIN" = 1 ]; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }
BPID=""
cleanup() { [ -n "$BPID" ] && kill "$BPID" 2>/dev/null || true; rm -rf "$TEST" "$STAGE/assets"; }
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------ export
if [ "$DO_EXPORT" = 1 ]; then
  if [ ! -f "$TPL/$TPL_FILE" ]; then
    echo "== export templates $VER ($PLATFORM) -> $TPL"
    "$PY" -I "$HERE/fetch_export_templates.py" --version "$VER" --platform "$PLATFORM" --out "$(win "$TPL")"
  fi

  if [ -n "$REUSE" ]; then
  [ "$PLATFORM" = linux ] && [ -f "$REUSE" ] || { echo "--pck-from-win: linux only, needs $REUSE" >&2; exit 1; }
  echo "== no export: $BIN = release template, HotA3D.pck + python/ from $(basename "$REUSE")"
  rm -rf "$OUTDIR"
  mkdir -p "$OUTDIR"
  cp "$TPL/$TPL_FILE" "$OUTDIR/$BIN"
  else
  echo "== staging $STAGE"
  mkdir -p "$STAGE"
  # fresh copy of everything except assets/ and the import cache (kept between runs: it holds no assets)
  find "$STAGE" -mindepth 1 -maxdepth 1 ! -name .godot -exec rm -rf {} +
  for e in "$CLIENT"/* "$CLIENT"/.[!.]*; do
    [ -e "$e" ] || continue
    case "$(basename "$e")" in assets|.godot) continue;; esac
    cp -r "$e" "$STAGE/"
  done
  # assets: hard links (no extra disk; removed again on exit), never edited in place: the original .import /
  # .gdignore links are deleted and own `keep` sidecars written instead
  cp -al "$CLIENT/assets" "$STAGE/assets" 2>/dev/null || cp -r "$CLIENT/assets" "$STAGE/assets"
  find "$STAGE/assets" \( -name '*.import' -o -name '.gdignore' \) -delete
  # the model generator keeps publishing into assets/models: the hard-linked copy is the snapshot that gets
  # exported; its manifest must only name GLBs that are in it
  "$PY" -I - "$(win "$STAGE/assets/models")" <<'EOF'
import json, os, re, sys
d = sys.argv[1]
with open(os.path.join(d, 'manifest.json'), encoding='utf-8') as f:
    text = f.read()
json.loads(text)
refs = set(re.findall(r'res://assets/models/([^"]+)', text))
have = {f for f in os.listdir(d) if f.lower().endswith('.glb')}
miss = sorted(refs - have)
if miss:
    sys.exit('manifest.json names GLBs that are not there: %s' % miss[:10])
print('   models: %d GLB, manifest names %d of them (all present)' % (len(have), len(refs)))
EOF
  n=0
  while IFS= read -r f; do
    printf '[remap]\n\nimporter="keep"\n' > "$f.import"
    n=$((n + 1))
  done < <(find "$STAGE/assets" -type f \( -iname '*.glb' -o -iname '*.gltf' -o -iname '*.jpg' -o -iname '*.jpeg' \
                                            -o -iname '*.png' -o -iname '*.webp' \))
  echo "   $n assets as raw files ($(du -sh "$STAGE/assets" | cut -f1), hard links)"
  # release template of every preset (chosen by its platform= line) -> the downloaded file
  awk -v w="$(win "$TPL/windows_release_x86_64.exe")" -v l="$(win "$TPL/linux_release.x86_64")" '
      /^platform=/ { plat = $0 }
      /^custom_template\/release=/ {
        if (plat ~ /Windows/) $0 = "custom_template/release=\"" w "\""
        else if (plat ~ /Linux/) $0 = "custom_template/release=\"" l "\""
      }
      { print }' "$CLIENT/export_presets.cfg" > "$STAGE/export_presets.cfg"
  if [ -n "$VERSION" ]; then
    awk -v v="$VERSION" '/^config\/version=/ { next } { print } /^config\/name=/ { print "config/version=\"" v "\"" }' \
        "$CLIENT/project.godot" > "$STAGE/project.godot"
  fi

  echo "== export ($VER, release, preset \"$PRESET\")"
  rm -rf "$OUTDIR"
  mkdir -p "$OUTDIR"
  set +e
  if [ ! -d "$STAGE/.godot" ]; then      # fresh staging (CI): build the import cache first
    timeout 1800 "$GODOT" --headless --path "$(win "$STAGE")" --import > "$LOG.import" 2>&1
  fi
  timeout 1800 "$GODOT" --headless --path "$(win "$STAGE")" --export-release "$PRESET" \
      "$(win "$OUTDIR")/$BIN" > "$LOG" 2>&1
  rc=$?
  set -e
  rm -rf "$STAGE/assets"
  grep -E "ERROR|WARNING" "$LOG" | grep -v "wasapi\|audio_server\|paged_allocator" | tail -20 || true
  if [ $rc != 0 ] || [ ! -f "$OUTDIR/$BIN" ] || [ ! -f "$OUTDIR/$NAME.pck" ]; then
    echo "export failed (rc $rc), log: $LOG" >&2; exit 1
  fi
  echo "   in pck: $(grep -c 'savepack.*res://assets/models/.*\.glb' "$LOG") GLB," \
       "$(grep -c 'savepack.*res://assets/terrain/' "$LOG") terrain textures," \
       "manifest $(grep -q 'savepack.*assets/models/manifest.json' "$LOG" && echo yes || echo NO)"
  fi
  if [ "$PLATFORM" = windows ]; then
    cat > "$OUTDIR/README.txt" <<'EOF'
HotA 3D client (Godot 4.7.2, Windows x64)

3D-клиент поверх оригинальной Heroes 3 HotA: вся механика считается в оригинальной игре (h3hota.exe) через
мост (bridge\bridge.py), клиент только рисует мир и шлёт команды. Нужна видеокарта с Vulkan (Forward+).

ЛОКАЛЬНЫЙ РЕЖИМ (игра на этом ПК; по умолчанию при первом запуске): распаковать и запустить HotA3D.exe. Клиент
сам находит игру, проверяет, всё ли есть, запускает игру + мост и закрывает их при выходе. Нужна только
установленная Heroes 3 Complete + HotA 1.8.0 (папка с h3hota.exe, HotA.dll, Data\*.lod); не нашлась - клиент
сразу откроет выбор папки (позже - кнопка "Папка игры..." на панели F3).
Python ставить не нужно: в python\ лежит свой (python.org embeddable 3.12 + frida, websockets, Pillow); если в
системе есть Python с frida и websockets - берётся он.
  HotA3D.exe                                   - локальный режим (или адрес, если раньше подключались к мосту)
  HotA3D.exe --local --game-dir "D:\Games\HoMM 3 Complete"   - явно: локальный режим и папка игры
  python\python.exe bridge\doctor.py           - та же проверка из консоли: чего не хватает и как исправить
Окно самой игры (800x600) тоже откроется, и играть можно в обоих сразу: что сделано в 3D-клиенте, видно в игре,
а что сделано в окне игры (ход героя, сундук, город, конец хода, диалоги), клиент повторяет; на диалог можно
ответить в любом окне. Окно игры можно свернуть - игра не остановится. Не нужно - снять галку "Окно игры на
экране" на панели F3 (или HotA3D.exe --game-window off), тогда игра работает скрыто (со следующего запуска).

УДАЛЁННЫЙ МОСТ (игра на другой машине):
  HotA3D.exe --url ws://10.10.0.108:8765       - сразу к мосту на ВМ (bridge.py --host 0.0.0.0)
  HotA3D.exe --url ws://127.0.0.1:8765         - через SSH-туннель: ssh -N -L 8765:127.0.0.1:8765 <ВМ>
  HotA3D.console.exe ...                       - то же с консолью (журнал клиента)
F2 - панель адреса (там же "Игра на этом ПК..." - назад в локальный режим), F3 - локальная игра (проверка, папка), Tab - поверхность/подземелье, Пробел - к герою, Enter - конец хода, H - скрыть HUD.
Подробно: godot/README.md в репозитории.
EOF
  else
    cat > "$OUTDIR/run.sh" <<'EOF'
#!/bin/sh
# HotA 3D client. Without arguments: local mode - the client checks Wine, the bundled Windows Python (python/) and
# the game folder (asks for it on first start), then starts game + bridge under Wine and stops them on exit.
#   ./run.sh                                  local mode (or the bridge address used last time)
#   ./run.sh --game-dir "$HOME/Games/HoMM3"   local mode with this game folder
#   ./run.sh --url ws://<IP>:8765             a bridge on another machine
# Wine prefix: ~/.local/share/HotA3D/wine (created on first start; WINEPREFIX=... overrides).
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/HotA3D.x86_64" "$@"
EOF
    cat > "$OUTDIR/doctor.sh" <<'EOF'
#!/bin/sh
# What the local mode needs and how to fix what is missing:
#   Wine present + version, Wine runs the bundled Windows Python with frida + websockets, game files, port.
# With python3 on the host: the full check (bridge/doctor.py: also 32-bit support, game version, display...).
# Without python3 (or HOTA3D_DOCTOR_SH=1): the same essentials here.
#   ./doctor.sh [--game-dir DIR] [--port N]
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$HOTA3D_DOCTOR_SH" ] && command -v python3 >/dev/null 2>&1; then
  exec python3 "$HERE/bridge/doctor.py" "$@"
fi
GAME=""; PORT=8765; FAIL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --game-dir) GAME="$2"; shift;;
    --port) PORT="$2"; shift;;
  esac
  shift
done
ok()   { echo "[ OK ] $1"; }
fail() { echo "[FAIL] $1"; [ -n "$2" ] && echo "        -> $2"; FAIL=1; }
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
if [ -z "$GAME" ]; then GAME="$HOTA_GAME_DIR"; fi
if [ -z "$GAME" ]; then
  CFG="$DATA/godot/app_userdata/HotA 3D Client/local.cfg"
  [ -f "$CFG" ] && GAME="$(sed -n 's/^dir="\(.*\)"$/\1/p' "$CFG" | head -1)"
fi
if [ -z "$GAME" ]; then
  fail "Папка игры не указана" "./run.sh спросит её при запуске, или ./doctor.sh --game-dir ПАПКА"
else
  miss=""
  for f in h3hota.exe HotA.dll Data/H3bitmap.lod Data/H3sprite.lod Data/HotA.lod; do
    [ -n "$(find "$GAME" -maxdepth 2 -ipath "$GAME/$f" 2>/dev/null | head -1)" ] || miss="$miss $f"
  done
  if [ -z "$miss" ]; then ok "Папка игры: $GAME"; else fail "Папка игры $GAME: нет$miss" "нужна Heroes 3 Complete + HotA 1.8.0"; fi
fi
WINE="${WINE:-wine}"
if ! WV="$("$WINE" --version 2>/dev/null)"; then
  fail "Wine не найден" "Debian/Ubuntu: sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install wine wine32 wine64; Fedora: sudo dnf install wine; Arch: [multilib] + sudo pacman -S wine"
else
  ok "Wine: $WV"
  PY="${HOTA3D_WINE_PYTHON:-$HERE/python/python.exe}"
  export WINEPREFIX="${WINEPREFIX:-$DATA/HotA3D/wine}" WINEDEBUG=-all
  if [ ! -f "$WINEPREFIX/system.reg" ]; then
    echo "[INFO] создаю префикс Wine $WINEPREFIX (один раз, до минуты)"
    mkdir -p "$WINEPREFIX"
    WINEDLLOVERRIDES="mscoree=d;mshtml=d" "$WINE" wineboot -i >/dev/null 2>&1
  fi
  if [ -f "$WINEPREFIX/system.reg" ]; then ok "Префикс Wine: $WINEPREFIX"; else fail "Префикс Wine не создан: $WINEPREFIX" "WINEPREFIX=\"$WINEPREFIX\" wineboot -u"; fi
  if [ ! -f "$PY" ]; then
    fail "Нет $PY" "распакуйте пакет целиком (папка python/ рядом с bridge/)"
  elif R="$("$WINE" "$PY" -c "import frida, websockets; print('frida', frida.__version__, 'websockets', websockets.__version__)" 2>&1)" \
       && echo "$R" | grep -q '^frida '; then
    ok "Windows-Python под Wine: $(echo "$R" | grep '^frida ') ($PY)"
  else
    fail "Wine не запускает python.exe из пакета с frida + websockets" "$(echo "$R" | tail -2 | tr '\n' ' ')"
  fi
fi
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q "[:.]$PORT "; then echo "[WARN] Порт $PORT занят (клиент возьмёт следующий свободный)"; else ok "Порт $PORT: свободен"; fi
else
  echo "[INFO] Порт $PORT: не проверен (нет ss)"
fi
echo
[ $FAIL = 0 ] && echo "Всё нужное есть: ./run.sh" || echo "Исправьте пункты [FAIL] и запустите проверку снова."
exit $FAIL
EOF
    cat > "$OUTDIR/README.txt" <<'EOF'
HotA 3D client (Godot 4.7.2, Linux x86_64)

3D-клиент поверх оригинальной Heroes 3 HotA: вся механика считается в оригинальной игре (h3hota.exe, программа
для Windows) через мост (bridge/bridge.py), клиент только рисует мир и шлёт команды.

БЫСТРЫЙ СТАРТ (игра на этом ПК). Нужны: Wine, Vulkan-драйвер и папка с Heroes 3 Complete + HotA 1.8.0.
  1. Wine:  Debian/Ubuntu: sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install wine wine32 wine64
            Fedora:        sudo dnf install wine
            Arch:          включить [multilib] в /etc/pacman.conf, затем sudo pacman -S wine
  2. tar xzf HotA3D-client-linux-x86_64.tar.gz && cd HotA3D-client-linux-x86_64 && ./run.sh
  3. Клиент сам ищет игру (сохранённая папка, HOTA_GAME_DIR, префиксы Wine, Lutris, Bottles, Steam); не нашёл -
     сразу открывает выбор папки: укажите папку с h3hota.exe (выбор запоминается; позже - F3, "Папка игры...").
Python ставить не нужно: мост работает под Wine на Windows-Python из пакета (python/: python.org embeddable 3.12
+ frida, websockets, Pillow):  wine python/python.exe bridge/bridge.py --game-dir Z:\...
Префикс Wine - свой: ~/.local/share/HotA3D/wine (создаётся при первом запуске, ~1 мин). Игра из другого префикса
(~/.wine, Lutris...) запускается как есть через Z:, тот префикс не меняется. Свой префикс: WINEPREFIX=... ./run.sh;
свой Windows-Python: HOTA3D_WINE_PYTHON=/путь/python.exe (тогда префикс - тот, где лежит игра).
  ./doctor.sh     - проверка: Wine и версия, Wine запускает python.exe из пакета с frida + websockets, файлы игры,
                    порт 8765 (с python3 на хосте - полная проверка bridge/doctor.py, без него - основное)
Окно самой игры (800x600) под Wine открыто рядом, играть можно в обоих сразу (клиент повторяет то, что сделано в
окне игры); скрыть - галка "Окно игры на экране" на панели F3 или ./run.sh --game-window off.
ВНИМАНИЕ: Frida внутри Wine здесь не проверялась (сборка сделана на Windows; на Windows этот же python/ запускает
игру и мост полностью). Если мост не стартует - журнал: ~/.local/share/godot/app_userdata/HotA 3D Client/local_run/bridge.log

УДАЛЁННЫЙ МОСТ (игра на Windows-машине): ./run.sh --url ws://<IP>:8765 (там: bridge.py --host 0.0.0.0);
через SSH-туннель: ssh -N -L 8765:127.0.0.1:8765 <машина>, затем ./run.sh --url ws://127.0.0.1:8765.

Нужен Vulkan (Forward+): Mesa (AMD radv / Intel anv) или NVIDIA с драйвером Vulkan; проверка - vulkaninfo --summary
(пакет vulkan-tools). Без Vulkan Godot сам переходит на OpenGL 3.3 (Compatibility); на нём клиент не проверялся.
F2 - панель адреса, F3 - локальная игра (проверка, папка игры), Tab - поверхность/подземелье, Пробел - к герою, Enter - конец хода, H - скрыть HUD.
Подробно: godot/README.md в репозитории.
EOF
  fi
  # the bridge for the local mode (python sources, agents, test saves)
  mkdir -p "$OUTDIR/bridge/testdata"
  for f in "$GODOT_DIR"/bridge/*.py "$GODOT_DIR"/bridge/*.js "$GODOT_DIR"/bridge/requirements.txt \
           "$GODOT_DIR"/bridge/README.md; do
    case "$(basename "$f")" in test_*) continue;; esac
    cp "$f" "$OUTDIR/bridge/"
  done
  cp "$GODOT_DIR"/bridge/testdata/*.GM1 "$GODOT_DIR"/bridge/testdata/snapshot_newcomers.json "$OUTDIR/bridge/testdata/"
  echo "   bridge/: $(ls "$OUTDIR/bridge" | wc -l) entries ($(du -sh "$OUTDIR/bridge" | cut -f1))"
  # portable Windows Python for the bridge (Windows: when no system Python has frida; Linux: run under Wine)
  if [ -z "$REUSE" ]; then
    "$PY" -I "$HERE/make_bridge_python.py" "$(win "$OUTDIR/python")" --src "$(win "$DL/bridge-python")"
    if [ "$ON_WIN" = 1 ]; then
      "$OUTDIR/python/python.exe" -c "import frida, websockets, PIL; print('   python/ check: frida', frida.__version__, 'websockets', websockets.__version__)"
    fi
  fi
  if [ -n "$VERSION" ]; then
    printf 'HotA 3D client %s\n%s\n' "$VERSION" "${COMMIT:-$(git -C "$GODOT_DIR" rev-parse HEAD 2>/dev/null || true)}" \
        > "$OUTDIR/VERSION.txt"
  fi
  du -sh "$OUTDIR"/* | sed 's/^/   /'
fi

# ------------------------------------------------------------------------------------------------ test (Windows)
if [ "$DO_TEST" = 1 ]; then
  # the exported exe + Mesa lavapipe: vulkan-1.dll shim and vulkan_lvp.dll next to it, own folder (removed on exit)
  rm -rf "$TEST"
  mkdir -p "$TEST" "$OUT"
  if [ -f "$OUTDIR/$NAME.pck" ]; then
    cp -l "$OUTDIR/$NAME.exe" "$OUTDIR/$NAME.pck" "$TEST/"
  else
    "$PY" -I -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); [open(sys.argv[2] + '/' + n.split('/')[-1], 'wb').write(z.read(n)) for n in z.namelist() if n.endswith(('.exe', '.pck')) and 'console' not in n]" \
        "$(win "$PKG")" "$(win "$TEST")"
  fi
  cp -l "$DL/godot/lvp/vulkan-1.dll" "$DL/godot/lvp/vulkan_lvp.dll" "$TEST/"
  PORT=$((18700 + RANDOM % 900))
  "$PY" -I "$GODOT_DIR/bridge/bridge.py" --mock --replay "$(win "$GODOT_DIR/bridge/testdata/snapshot_newcomers.json")" \
      --port "$PORT" --quiet > "$OUT/.export_bridge.log" 2>&1 &
  BPID=$!
  for _ in $(seq 1 100); do (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break; sleep 0.2; done
  echo "== test: exported exe vs bridge.py --mock --replay on :$PORT (lavapipe; first frame compiles shaders, minutes)"
  rm -f "$OUT/export_test.png" "$OUT/export_connect.png"
  timeout "${TIMEOUT:-900}" "$TEST/$NAME.exe" --rendering-driver vulkan --resolution 1024x576 \
      --url "ws://127.0.0.1:$PORT" -- --size 1600x900 --shot "$(win "$OUT/export_test.png")" --after 4 \
      > "$OUT/.export_test.log" 2>&1 || true
  grep -E "^(client|objects|terrain|world|shot)|SCRIPT ERROR|cannot load" "$OUT/.export_test.log" | head -20 || true
  [ -f "$OUT/export_test.png" ] && echo "-> $OUT/export_test.png" || { echo "test: no screenshot" >&2; exit 1; }
  echo "== test: connect panel (no bridge on the port)"
  timeout 600 "$TEST/$NAME.exe" --rendering-driver vulkan --resolution 1024x576 --url "ws://127.0.0.1:1" -- \
      --connect-shot "$(win "$OUT/export_connect.png")" > "$OUT/.export_connect.log" 2>&1 || true
  grep -E "^(client|connect-shot)|SCRIPT ERROR" "$OUT/.export_connect.log" || true
  [ -f "$OUT/export_connect.png" ] && echo "-> $OUT/export_connect.png"
  kill "$BPID" 2>/dev/null || true; BPID=""
  rm -rf "$TEST"
fi

# ------------------------------------------------------------------------------------------------ package
if [ "$DO_ZIP" = 1 ]; then
  [ -f "$OUTDIR/$NAME.pck" ] || [ -n "$REUSE" ] || { echo "no $OUTDIR to package" >&2; exit 1; }
  [ -n "$REUSE" ] || { [ -f "$OUTDIR/python/python.exe" ] && [ -d "$OUTDIR/python/Lib/site-packages/frida" ]; } \
      || { echo "no bundled python in $OUTDIR/python" >&2; exit 1; }
  [ -n "$REUSE" ] || "$PY" -I - "$(win "$OUTDIR/$NAME.pck")" <<'EOF'
import mmap, re, sys
with open(sys.argv[1], 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
    glb = set(re.findall(rb'assets/models/([\w.\-]+\.glb)', m))
    man = re.search(rb'assets/models/manifest\.json', m) is not None
if len(glb) < 100 or not man:
    sys.exit('pck check: %d GLB, manifest %s' % (len(glb), man))
print('   pck check: %d GLB names + manifest.json in %s' % (len(glb), sys.argv[1].split('/')[-1]))
EOF
  if [ "$PLATFORM" = windows ]; then
    echo "== zip $PKG"
    "$PY" -I - "$(win "$OUTDIR")" "$(win "$PKG")" <<'EOF'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
base = os.path.basename(os.path.normpath(src))
tmp = dst + ".part"
files = []
for root, dirs, fs in os.walk(src):
    dirs.sort()
    files += [os.path.join(root, f) for f in sorted(fs)]
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
    for p in files:
        z.write(p, os.path.join(base, os.path.relpath(p, src)).replace(os.sep, "/"))
# check: every member reads back with the right CRC and size
with zipfile.ZipFile(tmp) as z:
    bad = z.testzip()
    sizes = {i.filename: i.file_size for i in z.infolist()}
for p in files:
    n = os.path.join(base, os.path.relpath(p, src)).replace(os.sep, "/")
    if bad or sizes.get(n) != os.path.getsize(p):
        os.remove(tmp)
        sys.exit("zip check failed: %s" % (bad or n))
os.replace(tmp, dst)
print("   %d files, CRC ok" % len(files))
EOF
  else
    echo "== tar.gz $PKG"
    "$PY" -I - "$(win "$OUTDIR")" "$(win "$PKG")" "$BIN" ${REUSE:+"$(win "$REUSE")"} <<'EOF'
import os, re, struct, sys, tarfile, zipfile
src, dst, binname = sys.argv[1:4]
reuse = sys.argv[4] if len(sys.argv) > 4 else None    # Windows zip: HotA3D.pck + python/ streamed from it
base = os.path.basename(os.path.normpath(src))
EXEC = {binname, "run.sh", "doctor.sh"}
with open(os.path.join(src, binname), "rb") as f:     # ELF, 64-bit, little endian, e_machine 62 = x86-64
    h = f.read(20)
if h[:4] != b"\x7fELF" or h[4] != 2 or h[5] != 1 or struct.unpack("<H", h[18:20])[0] != 62:
    sys.exit("%s is not an ELF x86-64 binary" % binname)
files, dirs = [], []
for root, ds, fs in os.walk(src):
    ds.sort()
    rel = os.path.relpath(root, src).replace(os.sep, "/")
    rel = "" if rel == "." else rel + "/"
    dirs += [rel + d for d in ds]
    files += [rel + f for f in sorted(fs)]
want = {}
tmp = dst + ".part"
with tarfile.open(tmp, "w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as t:
    for dn in [""] + dirs:
        d = tarfile.TarInfo((base + "/" + dn).rstrip("/"))
        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o755, int(os.path.getmtime(src))
        t.addfile(d)
    for n in files:
        p = os.path.join(src, *n.split("/"))
        ti = tarfile.TarInfo(base + "/" + n)           # uid/gid 0, no owner names
        ti.size, ti.mtime = os.path.getsize(p), int(os.path.getmtime(p))
        ti.mode = 0o755 if n in EXEC else 0o644        # NTFS has no x bit: set it here (top-level tools only)
        want[ti.name] = (ti.size, ti.mode)
        with open(p, "rb") as f:
            t.addfile(ti, f)
    if reuse:
        with zipfile.ZipFile(reuse) as z:
            ms = [i for i in z.infolist() if not i.is_dir() and (i.filename.endswith("/HotA3D.pck")
                                                                 or "/python/" in i.filename)]
            seen = set()
            for i in ms:
                rel = i.filename.split("/", 1)[1]
                for k in range(1, rel.count("/") + 1):  # directory entries of python/...
                    dn = "/".join(rel.split("/")[:k])
                    if dn not in seen:
                        seen.add(dn)
                        d = tarfile.TarInfo(base + "/" + dn)
                        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o755, int(os.path.getmtime(src))
                        t.addfile(d)
                ti = tarfile.TarInfo(base + "/" + rel)
                ti.size, ti.mode = i.file_size, 0o644
                ti.mtime = int(__import__("time").mktime(i.date_time + (0, 0, -1)))
                want[ti.name] = (ti.size, ti.mode)
                with z.open(i) as f:
                    t.addfile(ti, f)
            files += [i.filename for i in ms]
            if not any(i.filename.endswith("/python/python.exe") for i in ms):
                sys.exit("no python/python.exe in " + reuse)
# check: stream every member back (gzip CRC is verified at the end), sizes and modes
got, glb, man = {}, set(), False
with tarfile.open(tmp, "r|gz") as t:
    for ti in t:
        if ti.isfile():
            f, size, tail = t.extractfile(ti), 0, b""
            pck = ti.name.endswith("/HotA3D.pck")
            for b in iter(lambda: f.read(1 << 20), b""):
                size += len(b)
                if pck:                                # models in the pck's file table (names are plain)
                    chunk = tail + b
                    glb.update(re.findall(rb'assets/models/([\w.\-]+\.glb)', chunk))
                    man = man or b"assets/models/manifest.json" in chunk
                    tail = chunk[-200:]
            got[ti.name] = (size, ti.mode)
if reuse and (len(glb) < 100 or not man):
    os.remove(tmp)
    sys.exit("pck check (in tar): %d GLB, manifest %s" % (len(glb), man))
if reuse:
    print("   pck check (in tar): %d GLB names + manifest.json" % len(glb))
if got != want:
    os.remove(tmp)
    sys.exit("tar.gz check failed: %r != %r" % (got, want))
os.replace(tmp, dst)
print("   %d files, gzip CRC ok, ELF x86-64; modes %s" % (
    len(files), ", ".join("%s %o" % (n.split("/")[-1], m) for n, (_, m) in sorted(got.items()) if m == 0o755)))
EOF
  fi
  echo "   $(du -h "$PKG" | cut -f1)  $PKG"
  if [ "$KEEP_DIR" = 0 ]; then
    rm -rf "$OUTDIR"
    echo "   removed $OUTDIR (--keep-dir keeps it)"
  fi
fi
