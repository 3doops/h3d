#!/usr/bin/env bash
# Parse-check all client scripts (headless, no rendering). Prints script errors only.
GODOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GODOT="$(ls "$GODOT_DIR"/../_dl/godot/Godot_v4*_win64_console.exe | head -1)"
for s in "$GODOT_DIR"/client/scripts/*.gd; do
  n="$(basename "$s")"
  out="$(timeout 60 "$GODOT" --headless --path "$GODOT_DIR/client" --check-only --script "res://scripts/$n" 2>&1 \
        | grep -E "SCRIPT ERROR|Parse Error|at: " | grep -v "wasapi\|audio_server\|paged_allocator")"
  [ -n "$out" ] && { echo "== $n"; echo "$out" | head -20; }
done
echo "check done"
