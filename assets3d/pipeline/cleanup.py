"""Disk hygiene for godot/assets3d/work (drive C: is small).

usage: cleanup.py [--all] [--codex-cache] [--dry-run]
  --all          compact every model folder (one-off pass); the runner calls compact(key) after each publish
  --codex-cache  also delete the originals of our concepts in ~/.codex/generated_images/<thread>/ (copies exist)
What is kept (everything a re-run needs): the current concept (as JPEG q92), the current raw TRELLIS GLB,
state.json, prompts and Codex logs, fit_*/stats.json and views/game.png + front.png (contact sheets).
What goes: concept PNGs (-> JPEG), concepts of rejected attempts, full-size cut-outs (-> 384 px), raw GLBs of
superseded seeds, fit_*/model.glb (identical copy is published in godot/client/assets/models), side views and
orientation renders, Blender fit logs of published models.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CODEX_IMAGES, WORK, load_state, save_state  # noqa: E402

CUT_SIZE = 384


def free_gb(path="C:\\"):
    return shutil.disk_usage(path).free / 2 ** 30


def to_jpeg(png, quality=92):
    jpg = png.with_suffix(".jpg")
    Image.open(png).convert("RGB").save(jpg, quality=quality)
    png.unlink()
    return jpg


def shrink_cut(cut):
    im = Image.open(cut)
    if max(im.size) > CUT_SIZE:
        im.thumbnail((CUT_SIZE, CUT_SIZE), Image.LANCZOS)
        im.save(cut, optimize=True)


def compact(key, dry=False):
    """returns bytes freed"""
    wd = WORK / key
    if not (wd / "state.json").exists():
        return 0
    st = load_state(key)
    before = sum(f.stat().st_size for f in wd.rglob("*") if f.is_file())
    if dry:
        return 0
    cur = Path(st["concept"]) if st.get("concept") else None
    # concepts: current one -> JPEG, rejected attempts -> delete (their prompts/logs stay)
    for f in list(wd.glob("concept_*.png")) + list(wd.glob("concept_*.jpg")):
        if f.name.endswith("_cut.png"):
            continue
        if cur is not None and f.stem == cur.stem:
            if f.suffix == ".png":
                st["concept"] = str(to_jpeg(f))
        elif st.get("stage") != "new":
            f.unlink()
    for f in wd.glob("concept_*_cut.png"):
        if cur is None or not f.name.startswith(cur.stem + "_cut"):
            f.unlink()
        else:
            shrink_cut(f)
    # raw GLBs of superseded seeds
    raw = Path(st["raw"]) if st.get("raw") else None
    for f in wd.glob("raw_s*.glb"):
        if st.get("upgraded"):  # high-detail model replaced the 512 one: keep that raw as the fallback
            break
        if raw is None or f.name != raw.name:
            if st.get("stage") in ("published", "fitted", "raw"):
                f.unlink()
                f.with_suffix(".json").unlink(missing_ok=True)
    # fit folders of published models: the published GLB is the same file
    if st.get("stage") == "published":
        for fd in wd.glob("fit_*"):
            if not fd.is_dir():
                if fd.suffix == ".log":
                    fd.unlink()
                continue
            (fd / "model.glb").unlink(missing_ok=True)
            shutil.rmtree(fd / "_orient", ignore_errors=True)
            for v in ("right", "back", "left"):
                (fd / "views" / f"{v}.png").unlink(missing_ok=True)
    save_state(key, st)
    after = sum(f.stat().st_size for f in wd.rglob("*") if f.is_file())
    return before - after


def codex_cache(dry=False):
    """delete Codex's own copies of images we already copied into work/ (thread ids from our jsonl logs)"""
    freed = 0
    threads = set()
    for j in WORK.rglob("codex_*.jsonl"):
        try:
            first = j.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            threads.add(json.loads(first).get("thread_id"))
        except (IndexError, json.JSONDecodeError):
            pass
    for t in threads:
        d = CODEX_IMAGES / str(t)
        if t and d.is_dir():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            if not dry:
                shutil.rmtree(d, ignore_errors=True)
    return freed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--codex-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(f"free before: {free_gb():.2f} GB")
    total = 0
    if a.all:
        for d in sorted(WORK.iterdir()):
            if d.is_dir() and (d / "state.json").exists():
                total += compact(d.name, a.dry_run)
        print(f"work/ compacted: {total / 2 ** 20:.0f} MB")
    if a.codex_cache:
        c = codex_cache(a.dry_run)
        print(f"codex cache: {c / 2 ** 20:.0f} MB")
    print(f"free after: {free_gb():.2f} GB")


if __name__ == "__main__":
    main()
