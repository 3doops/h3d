"""Shared paths and helpers of the 3D asset pipeline (godot/assets3d/pipeline).

Layout:
  godot/assets3d/catalog.tsv           what to build (build_catalog.py writes it)
  godot/assets3d/prompts/               concept prompt template + per-category notes
  godot/assets3d/style_refs/            style images passed to Codex (derived from godot/ref/, no game sprites)
  godot/assets3d/work/<key>/            per-model work dir: prompt, concepts, raw TRELLIS glb, fitted glb, renders,
                                        state.json (not in git)
  godot/assets3d/sheets/                contact sheets of concepts and models
  godot/assets3d/review.tsv             manual verdicts: key <TAB> verdict (ok|reject_concept|reject_model) <TAB> note
  godot/client/assets/models/<key>.glb  published models + manifest.json
"""
import csv
import json
import os
import shutil
import time
from pathlib import Path

PIPE = Path(__file__).resolve().parent
A3D = PIPE.parent
ROOT = A3D.parents[1]
WORK = A3D / "work"
SHEETS = A3D / "sheets"
PROMPTS = A3D / "prompts"
STYLE_REFS = [A3D / "style_refs" / "style_single_object.jpg", A3D / "style_refs" / "style_map_closeup.jpg"]
CATALOG = A3D / "catalog.tsv"
REVIEW = A3D / "review.tsv"
MODELS = ROOT / "godot" / "client" / "assets" / "models"
MANIFEST = MODELS / "manifest.json"
TERRAIN = ROOT / "godot" / "client" / "assets" / "terrain"
LOG = WORK / "runner.log"

BLENDER = Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
UV = shutil.which("uv") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/uv.exe")
CODEX = shutil.which("codex") or shutil.which("codex.cmd") or "codex"
CODEX_IMAGES = Path.home() / ".codex" / "generated_images"


def log(msg, key=None):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + (f"[{key}] " if key else "") + msg
    print(line, flush=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_tsv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f, delimiter="\t") if any(row.values())]


def write_tsv(path, rows, fields):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def load_catalog():
    return read_tsv(CATALOG)


def load_review():
    """key -> (verdict, note); the last line for a key wins"""
    out = {}
    for row in read_tsv(REVIEW):
        out[row["key"]] = (row.get("verdict", ""), row.get("note", ""))
    return out


def work_dir(key):
    d = WORK / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(key):
    p = WORK / key / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"key": key, "stage": "new", "attempts": {"concept": 0, "trellis": 0, "fit": 0}, "history": []}


def save_state(key, st):
    p = work_dir(key) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def parse_footprint(s):
    """'3x2' -> (3, 2)"""
    w, h = (s or "1x1").lower().split("x")
    return int(w), int(h)
