"""Concept image for one catalog row via `codex exec` (built-in image_gen) with the style references.

usage: concept.py <key> [--attempt N] [--dry-run]       (key from catalog.tsv)
Writes work/<key>/prompt_<N>.txt, codex_<N>.jsonl/.err, concept_<N>.png and prints the png path.

Codex quirks on this VM (see memory codex-imagegen): its sandbox cannot write the workspace, so the images stay in
~/.codex/generated_images/<thread_id>/ and are copied from there; the prompt goes via stdin; reasoning effort is
lowered to medium and the unreachable MCP server of the user config is disabled.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with python -I: add our own dir only
from common import CODEX, CODEX_IMAGES, PROMPTS, STYLE_REFS, load_catalog, log, parse_footprint, work_dir

TEMPLATE = PROMPTS / "concept.txt"
CATEGORIES = PROMPTS / "categories.json"


def build_prompt(row, attempt=1):
    cats = json.loads(CATEGORIES.read_text(encoding="utf-8"))
    cat = cats.get(row["category"], cats["_default"])
    w, h = parse_footprint(row.get("footprint"))
    back = cats.get(row["category"], {}).get("fit", {}).get("back", 0.0)
    d = max(1, round(h + back)) if w > 1 else h  # 1-tile-wide objects: keep them square in the concept
    fmt = {"description": row["description"].strip(), "w": w, "h": h, "d": d,
           "ratio": f"{w}:{d}", "name": row.get("name", "")}
    parts = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in cat.items()}
    text = TEMPLATE.read_text(encoding="utf-8").format(
        description=fmt["description"],
        size_note=parts.get("size", ""),
        base_note=parts.get("base", ""),
        view_note=parts.get("view", cats["_default"]["view"]),
        extra=parts.get("extra", ""),
    )
    if attempt > 1:
        text += ("\nThis is retry number %d: the previous picture was rejected (wrong framing, extra objects, text, "
                 "cut-off parts or background). Follow the composition rules strictly.\n" % attempt)
    return text


def run_codex(prompt, wd, tag, refs=STYLE_REFS, timeout=1200):
    """Runs one codex exec; returns the list of generated image paths (newest last)."""
    jsonl, err, last = wd / f"codex_{tag}.jsonl", wd / f"codex_{tag}.err", wd / f"codex_{tag}.last.txt"
    cmd = [CODEX, "exec", "-C", str(wd), "--skip-git-repo-check",
           "-c", 'model_reasoning_effort="medium"', "-c", "mcp_servers.progress.enabled=false"]
    for r in refs:
        cmd += ["-i", str(r)]
    cmd += ["--json", "-o", str(last)]
    start = time.time()
    with jsonl.open("w", encoding="utf-8") as out, err.open("w", encoding="utf-8") as er:
        proc = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", stdout=out, stderr=er,
                              timeout=timeout)
    thread = None
    for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started":
            thread = ev.get("thread_id")
            break
    images = []
    if thread and (CODEX_IMAGES / thread).is_dir():
        images = sorted((CODEX_IMAGES / thread).glob("*.png"), key=lambda p: p.stat().st_mtime)
    return images, {"rc": proc.returncode, "thread": thread, "seconds": round(time.time() - start, 1)}


def generate(row, attempt=1, dry_run=False):
    key = row["key"]
    wd = work_dir(key)
    prompt = build_prompt(row, attempt)
    (wd / f"prompt_{attempt}.txt").write_text(prompt, encoding="utf-8")
    if dry_run:
        print(prompt)
        return None, {}
    images, info = run_codex(prompt, wd, str(attempt))
    if not images:
        log(f"codex attempt {attempt}: no image ({info})", key)
        return None, info
    out = wd / f"concept_{attempt}.jpg"  # JPEG q92: ~5x smaller than Codex's PNG, fine for TRELLIS (disk is tight)
    from PIL import Image
    Image.open(images[-1]).convert("RGB").save(out, quality=92)
    info["source"] = str(images[-1])
    info["count"] = len(images)
    shutil.rmtree(images[-1].parent, ignore_errors=True)  # Codex keeps its own copy on C:, which is short of space
    log(f"concept {out.name} in {info['seconds']} s", key)
    return out, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    row = next((r for r in load_catalog() if r["key"] == args.key), None)
    if row is None:
        sys.exit(f"{args.key}: not in catalog")
    out, info = generate(row, args.attempt, args.dry_run)
    print(out, json.dumps(info))


if __name__ == "__main__":
    main()
