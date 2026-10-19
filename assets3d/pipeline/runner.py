"""Resumable queue runner: catalog row -> Codex concept -> auto check -> TRELLIS.2 -> Blender fit -> auto check ->
godot/client/assets/models/<key>.glb -> manifest.json. Also assembles tree/forest clumps from finished tree models.

usage (from godot/assets3d/pipeline):
  uv run --no-project --python 3.12 --with requests --with pillow --with numpy python -I runner.py [options]
    --max-priority N   only rows with priority <= N (default 9 = all)
    --keys a,b         only these keys
    --codex-workers N  parallel Codex jobs (default 3)
    --lookahead N      max concepts waiting for TRELLIS before Codex pauses (default 24)
    --once             stop when nothing is left to do instead of polling for catalog/review changes
    --no-codex / --no-trellis / --no-blender   disable a stage (e.g. only re-fit)
Everything is resumable: per-model state lives in work/<key>/state.json; a killed run continues where it stopped
(an interrupted Codex/TRELLIS job is simply redone). Only one runner at a time (work/runner.lock with the PID).
Manual review: add lines to godot/assets3d/review.tsv (key, verdict, note) with verdict
  reject_concept -> new concept (attempt+1), reject_model -> new TRELLIS seed, refit -> Blender again,
  ok -> mark reviewed. The runner picks the changes up within a minute.
"""
import argparse
import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_concept  # noqa: E402
import cleanup  # noqa: E402
import concept  # noqa: E402
import trellis  # noqa: E402
from common import (BLENDER, MANIFEST, MODELS, PIPE, PROMPTS, WORK, load_catalog, load_review,  # noqa: E402
                    load_state, log, parse_footprint, save_state, work_dir)

# TRELLIS.2 returns the object with the facade seen in the concept facing -Y in Blender (glTF +Z, the front),
# even for 3/4 concepts (checked on necrocastle hero_B_s0.glb: turn 0 shows the facade in the front view).
# Per-row override: catalog column fit, e.g. "turn=-30".
TURN = float(os.environ.get("A3D_TURN", "0"))
RESOLUTION = os.environ.get("A3D_RES", "512")  # TRELLIS.2 voxel resolution: 512 | 1024 (per row: fit "res=1024")
MAX_CONCEPT_ATTEMPTS = 3
CLUMP_VERSION = "2"  # bump to re-assemble every forest clump after changing assemble_clump.py
MAX_TRELLIS_ATTEMPTS = 3

lock = threading.Lock()
codex_pause_until = 0.0  # set when Codex reports its usage limit
claimed = set()
stop = threading.Event()
stats = {"concepts": 0, "models": 0, "published": 0, "errors": 0}


def categories():
    return json.loads((PROMPTS / "categories.json").read_text(encoding="utf-8"))


def row_fit(row):
    cats = categories()
    fit = dict(cats["_default"]["fit"])
    fit.update(cats.get(row["category"], {}).get("fit", {}))
    w, h = parse_footprint(row.get("footprint"))
    fit.update({"w": w, "h": h, "turn": TURN, "flag": row.get("flag") == "1"})
    for item in (row.get("fit") or "").replace(",", ";").split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            fit[k] = v if k == "mode" else (v.lower() in ("1", "true") if k == "flag" else float(v))
    return fit


def row_defs(row):
    """[(def, (w, h))] from 'def:WxH,def2:WxH'"""
    out = []
    for item in (row.get("defs") or "").replace(" ", ",").split(","):
        if not item:
            continue
        d, _, fp = item.partition(":")
        out.append((d.lower(), parse_footprint(fp) if fp else parse_footprint(row.get("footprint"))))
    return out


# ---------------------------------------------------------------- review
def desc_hash(row):
    return hashlib.md5((row.get("category", "") + "|" + row.get("description", "")).encode("utf-8")).hexdigest()[:10]


def apply_review(rows):
    review = load_review()
    for row in rows:
        key = row["model_key"]
        if row.get("kind") == "gen" and key not in claimed and (WORK / key / "state.json").exists():
            st = load_state(key)
            if st.get("desc_hash") and st["desc_hash"] != desc_hash(row):
                st.update(stage="new", desc_hash=desc_hash(row))
                for k in ("concept", "raw", "fit_dir", "failed", "retry_after"):
                    st.pop(k, None)
                st["history"].append({"t": time.time(), "note": "description changed -> new concept"})
                save_state(key, st)
                log("description/category changed in the catalog -> new concept", key)
        if key not in review:
            continue
        verdict, note = review[key]
        st = load_state(key)
        tag = f"{verdict}|{note}"
        if st.get("review_applied") == tag:
            continue
        st["review_applied"] = tag
        st["history"].append({"t": time.time(), "review": verdict, "note": note})
        if verdict == "reject_concept":
            st.update(stage="new", rejected=True)
            st.pop("concept", None)
            st.pop("raw", None)
            st.pop("fit_dir", None)
            log(f"review: reject concept ({note}) -> new concept", key)
        elif verdict == "reject_model":
            st.update(stage="concept_ok", rejected=True)
            st["seed"] = st.get("seed", 0) + 1
            st.pop("raw", None)
            st.pop("fit_dir", None)
            log(f"review: reject model ({note}) -> TRELLIS seed {st['seed']}", key)
        elif verdict.startswith("turn="):  # facade on the wrong side: extra turn (degrees, + = counterclockwise
            st["turn_extra"] = float(verdict.split("=", 1)[1])  # seen from above) and fit again
            if st.get("raw"):
                st.update(stage="raw")
                log(f"review: extra turn {st['turn_extra']} ({note})", key)
        elif verdict == "refit":
            if st.get("raw"):
                st.update(stage="raw")
                log(f"review: refit ({note})", key)
        elif verdict == "ok":
            st["reviewed"] = True
            st.pop("rejected", None)
        save_state(key, st)


# ---------------------------------------------------------------- selection
def pending(rows, stage_wanted):
    """rows (in priority order) whose state is at stage_wanted and that nobody works on"""
    out = []
    for row in rows:
        key = row["model_key"]
        if key in claimed:
            continue
        st = load_state(key)
        if st.get("stage", "new") == stage_wanted and not st.get("failed") \
                and st.get("retry_after", 0) <= time.time():
            out.append((row, st))
    return out


def claim(rows, stage):
    with lock:
        for row, st in pending(rows, stage):
            claimed.add(row["model_key"])
            return row, st
    return None, None


def release(key):
    with lock:
        claimed.discard(key)


# ---------------------------------------------------------------- stages
def do_concept(row, st):
    key = row["model_key"]
    attempt = st["attempts"]["concept"] + 1
    st["attempts"]["concept"] = attempt
    save_state(key, st)
    crow = dict(row, key=key)
    out, info = concept.generate(crow, attempt)
    st = load_state(key)
    st["history"].append({"t": time.time(), "concept": attempt, **{k: info.get(k) for k in ("rc", "seconds")}})
    if out is None:
        text = ""
        for ext in ("jsonl", "err"):
            f = WORK / key / f"codex_{attempt}.{ext}"
            if f.exists():
                text += f.read_text(encoding="utf-8", errors="replace")[-3000:]
        low = text.lower()
        if "usage limit" in low or "rate limit" in low or "too many requests" in low:
            # the ChatGPT plan's image quota is used up: not this model's fault; all Codex workers wait until
            # the time Codex names ("try again at 4:47 AM"), else 30 min
            global codex_pause_until
            until = time.time() + 1800
            m = re.search(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", text)
            if m:
                hh, mm = int(m.group(1)) % 12 if m.group(3) else int(m.group(1)), int(m.group(2))
                if m.group(3) == "PM":
                    hh += 12
                now = time.localtime()
                t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
                if t < time.time() - 60:
                    t += 86400
                until = t + 120
            codex_pause_until = max(codex_pause_until, until)
            log("codex usage limit: Codex paused until " + time.strftime("%H:%M", time.localtime(until)), key)
            st["attempts"]["concept"] -= 1
            save_state(key, st)
            return "pause"
        st["retry_after"] = time.time() + 300 * attempt
        if attempt >= MAX_CONCEPT_ATTEMPTS + 3:
            st["failed"] = "codex produced no image"
        save_state(key, st)
        return "error"
    res = check_concept.check(out)
    st["auto_concept"] = res
    st["concept"] = str(out)
    st["desc_hash"] = desc_hash(row)
    if res["ok"]:
        st["stage"] = "concept_ok"
        stats["concepts"] += 1
    else:
        log(f"concept {attempt} auto-check failed: {res['problems']}", key)
        if attempt >= MAX_CONCEPT_ATTEMPTS:
            # keep the best we have; a human decides from the sheet
            st["stage"] = "concept_ok"
            st["auto_concept_forced"] = True
            log("max attempts reached, using the last concept anyway", key)
        else:
            st["stage"] = "new"
    save_state(key, st)
    return "ok"


def do_trellis(row, st):
    key = row["model_key"]
    fit = row_fit(row)
    seed = st.get("seed", 0)
    raw = WORK / key / f"raw_s{seed}.glb"
    faces = int(fit.get("tris", 20000))
    infra = st.get("infra_errors", 0)
    # 1024 runs out of GPU memory in CuMesh on the shared RTX 3090 since 2026-10-17 evening; 512 works (~50 s)
    resolution = str(int(fit.get("res", RESOLUTION)))
    if infra >= 2:
        resolution = "512"
    try:
        meta = trellis.generate(st["concept"], raw, faces=max(10000, faces), seed=seed, resolution=resolution,
                                texture_size="2048" if int(fit.get("tex", 1024)) > 1024 else "1024")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "out of memory" in msg or "CUDA" in msg or isinstance(e, (OSError, ConnectionError)) \
                or msg.startswith(("502", "503", "504")) or "Timeout" in type(e).__name__:
            # GPU host problem (memory held by another job, server down): not the model's fault -> retry later,
            # with resolution 512 after two such errors for this key
            st["infra_errors"] = infra + 1
            st["retry_after"] = time.time() + 600 * min(st["infra_errors"], 6)
            st["history"].append({"t": time.time(), "trellis_infra_error": msg[:200]})
            save_state(key, st)
            log(f"TRELLIS infrastructure error #{st['infra_errors']} (pausing TRELLIS): {msg[:160]}", key)
            stats["errors"] += 1
            return "pause"
        st["attempts"]["trellis"] += 1
        st["history"].append({"t": time.time(), "trellis_error": str(e)[:300]})
        log(f"TRELLIS error: {e}", key)
        if st["attempts"]["trellis"] >= MAX_TRELLIS_ATTEMPTS:
            st["failed"] = "trellis: " + str(e)[:200]
        else:
            st["seed"] = seed + 1
        save_state(key, st)
        stats["errors"] += 1
        return "error"
    st["raw"] = str(raw)
    st["resolution"] = resolution
    st["stage"] = "raw"
    st["history"].append({"t": time.time(), "trellis_seed": seed, "seconds": meta.get("generation_seconds")})
    save_state(key, st)
    stats["models"] += 1
    log(f"TRELLIS seed {seed}: {meta.get('generation_seconds')} s, {meta['bytes'] // 1024} KB", key)
    return "ok"


def run_blender(script, args, logfile, timeout=900):
    cmd = [str(BLENDER), "-b", "--factory-startup", "-P", str(PIPE / script), "--"] + [str(a) for a in args]
    with open(logfile, "w", encoding="utf-8") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode


def model_problems(stats_json, fit):
    probs = []
    if stats_json["tris"] < 300:
        probs.append(f"too few triangles {stats_json['tris']}")
    sx, sy, sz = stats_json["size_xyz_godot"]
    if sy < 0.05:
        probs.append("flat model")
    if min(sx, sz) < 0.12 * max(sx, sz):
        probs.append(f"card-like model {sx:.2f}x{sz:.2f}")
    return probs


def do_fit(row, st):
    key = row["model_key"]
    fit = row_fit(row)
    defs = row_defs(row)
    footprints = sorted({fp for _, fp in defs} | {(int(fit["w"]), int(fit["h"]))})
    st["fits"] = {}
    for fp in footprints:
        p = dict(fit, w=fp[0], h=fp[1], concept=st.get("concept"))
        p["turn"] = float(p.get("turn", 0)) + float(st.get("turn_extra", 0))
        if st.get("front") is not None and st.get("front_raw") == st.get("raw"):
            p["front_fixed"] = st["front"]  # same raw model: keep the facade found on its first fit
        tag = f"{fp[0]}x{fp[1]}"
        out = WORK / key / f"fit_{tag}"
        pj = WORK / key / f"fit_{tag}.json"
        pj.write_text(json.dumps(p), encoding="utf-8")
        primary = fp == (int(fit["w"]), int(fit["h"]))
        p["render"] = primary
        pj.write_text(json.dumps(p), encoding="utf-8")
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        rc = run_blender("fit_asset.py", [st["raw"], out, pj], WORK / key / f"fit_{tag}.log")
        sj = out / "stats.json"
        if rc != 0 or not sj.exists():
            st["attempts"]["fit"] += 1
            log(f"fit {tag} failed rc={rc}, see fit_{tag}.log", key)
            if st["attempts"]["fit"] >= 2:
                st["failed"] = "blender fit failed"
            save_state(key, st)
            return "error"
        s = json.loads(sj.read_text(encoding="utf-8"))
        st["fits"][tag] = {"dir": str(out), "tris": s["tris"], "size": s["size_xyz_godot"], "scale": s["scale"],
                           "limited_by": s["limited_by"]}
        if primary:
            if s.get("orient") and s["orient"].get("front") is not None:
                st["front"], st["front_raw"] = s["orient"]["front"], st.get("raw")
            st["fit_dir"] = str(out)
            st["tris"] = s["tris"]
            probs = model_problems(s, p)
            st["auto_model"] = {"ok": not probs, "problems": probs}
            if probs:
                log(f"model auto-check: {probs}", key)
    if st["auto_model"]["ok"] or st["attempts"]["trellis"] >= MAX_TRELLIS_ATTEMPTS:
        st["stage"] = "fitted"
    else:
        st["attempts"]["trellis"] += 1
        st["seed"] = st.get("seed", 0) + 1
        st["stage"] = "concept_ok"
    save_state(key, st)
    return "ok"


def glb_name(key, tag, primary_tag):
    return f"{key}.glb" if tag == primary_tag else f"{key}__{tag}.glb"


def do_publish(row, st):
    key = row["model_key"]
    fit = row_fit(row)
    primary_tag = f"{int(fit['w'])}x{int(fit['h'])}"
    MODELS.mkdir(parents=True, exist_ok=True)
    for tag, f in st["fits"].items():
        shutil.copyfile(Path(f["dir"]) / "model.glb", MODELS / glb_name(key, tag, primary_tag))
    st["stage"] = "published"
    st["published_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(key, st)
    stats["published"] += 1
    log(f"published ({', '.join(st['fits'])}), {st.get('tris')} tris", key)
    try:
        cleanup.compact(key)  # concept -> JPEG, drop duplicates and intermediate renders (C: is small)
    except Exception as e:  # noqa: BLE001
        log(f"cleanup failed: {e}", key)
    return "ok"


# ---------------------------------------------------------------- clumps (forests from single tree models)
def do_clumps(rows):
    """kind=clump rows: one GLB per DEF, member tree models placed on the DEF's blocked cells"""
    done_any = False
    by_key = {r["model_key"]: r for r in rows}
    for row in rows:
        if row.get("kind") != "clump":
            continue
        key = row["model_key"]
        members = [m for m in (row.get("members") or "").split(",") if m]
        if not members or any(load_state(m).get("stage") != "published" for m in members if m in by_key):
            continue
        st = load_state(key)
        sig = CLUMP_VERSION + ":" + ",".join(f"{m}@{load_state(m).get('published_at')}" for m in members)
        if st.get("stage") == "published" and st.get("members_sig") == sig:
            continue
        defs = row_defs(row)
        masks = json.loads((PIPE / "sources" / "masks.json").read_text(encoding="utf-8")) \
            if (PIPE / "sources" / "masks.json").exists() else {}
        job = {"members": [str(MODELS / f"{m}.glb") for m in members], "out_dir": str(MODELS),
               "items": [{"def": d, "w": fp[0], "h": fp[1], "block": masks.get(d, {}).get("block"),
                          "out": f"{key}__{d}.glb"} for d, fp in defs],
               "density": float(dict(x.split("=") for x in (row.get("fit") or "density=1").split(";") if "=" in x)
                                .get("density", 1))}
        jf = work_dir(key) / "clump_job.json"
        jf.write_text(json.dumps(job, indent=1), encoding="utf-8")
        rc = run_blender("assemble_clump.py", [jf], WORK / key / "clump.log", timeout=1800)
        if rc != 0:
            log(f"clump assembly failed rc={rc}", key)
            st["failed"] = "clump failed"
            save_state(key, st)
            continue
        st.update(stage="published", members_sig=sig, published_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_state(key, st)
        log(f"clumps assembled for {len(defs)} defs", key)
        done_any = True
    return done_any


# ---------------------------------------------------------------- manifest
def write_manifest(rows):
    models, variants = {}, {}
    for row in rows:
        key = row["model_key"]
        st = load_state(key) if (WORK / key / "state.json").exists() else {}
        if st.get("stage") != "published":
            continue
        if row.get("kind") == "clump":
            for d, fp in row_defs(row):
                if (MODELS / f"{key}__{d}.glb").exists():
                    models[f"def:{d}"] = {"glb": f"res://assets/models/{key}__{d}.glb", "footprint": list(fp)}
            continue
        fit = row_fit(row)
        primary = (int(fit["w"]), int(fit["h"]))
        primary_tag = f"{primary[0]}x{primary[1]}"
        for d, fp in row_defs(row):
            tag = f"{fp[0]}x{fp[1]}"
            name = glb_name(key, tag, primary_tag)
            if (MODELS / name).exists():
                models[f"def:{d}"] = {"glb": f"res://assets/models/{name}", "footprint": list(fp)}
        for mk in (row.get("manifest_keys") or "").split():
            if mk.startswith("def:"):
                continue
            entry = {"glb": f"res://assets/models/{key}.glb", "footprint": list(primary)}
            if mk in models and mk.startswith("obj:"):
                variants.setdefault(mk, [models[mk]["glb"]])
                if entry["glb"] not in variants[mk]:
                    variants[mk].append(entry["glb"])
                continue
            models[mk] = entry
    data = {"version": 1, "models": dict(sorted(models.items())),
            "variants": {k: v for k, v in sorted(variants.items()) if len(v) > 1}}
    MODELS.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, MANIFEST)
    return len(models)


# ---------------------------------------------------------------- catalog refresh
def refresh_catalog():
    """rebuild catalog.tsv when model descriptions, sources or the bridge's test-map data are newer"""
    import build_catalog
    inputs = list((PIPE / "models").glob("*.tsv")) \
        + [p for p in (PIPE / "sources").glob("*.tsv") if p.name != "unassigned.tsv"] \
        + list(build_catalog.BRIDGE_TESTDATA.glob("*newcomers*"))
    cat = PIPE.parent / "catalog.tsv"
    newest = max((p.stat().st_mtime for p in inputs), default=0)
    if cat.exists() and newest <= cat.stat().st_mtime:
        return
    try:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            build_catalog.main()
        log("catalog rebuilt: " + " / ".join(buf.getvalue().splitlines()[:2]))
    except BaseException as e:  # noqa: BLE001  (build_catalog may sys.exit on duplicate keys)
        log(f"catalog rebuild failed: {e}")


# ---------------------------------------------------------------- workers
def rows_now(args):
    rows = [r for r in load_catalog() if r.get("kind") in ("gen", "clump")]
    rows = [r for r in rows if int(r.get("priority") or 9) <= args.max_priority]
    if args.keys:
        rows = [r for r in rows if r["model_key"] in args.keys]
    rows.sort(key=lambda r: int(r.get("priority") or 9))  # stable: catalog order inside a priority
    return rows


MIN_FREE_GB = float(os.environ.get("A3D_MIN_FREE_GB", "2"))
_disk_logged = [0.0]


def disk_low():
    """True (and a log line every 10 min) while drive C: has less than MIN_FREE_GB free: every stage pauses"""
    free = cleanup.free_gb()
    if free >= MIN_FREE_GB:
        return False
    if time.time() - _disk_logged[0] > 600:
        _disk_logged[0] = time.time()
        log(f"DISK GUARD: only {free:.2f} GB free on C: (< {MIN_FREE_GB} GB) - pipeline paused")
    return True


UPGRADE_EVERY = float(os.environ.get("A3D_UPGRADE_EVERY", "3600"))  # s between high-res attempts (OOM hurts all)
_last_upgrade = [0.0]


def try_upgrade(args):
    """High-detail pass for rows with fit "upgrade=1024" (towns): once their normal model is published, try
    TRELLIS at that resolution (decimation "upgrade_faces", default 60000) at most once per UPGRADE_EVERY and only
    when the GPU is idle. On success the new raw model replaces the old one (refit + publish); on OOM the
    published 512 model stays and the attempt is repeated later."""
    if time.time() - _last_upgrade[0] < UPGRADE_EVERY:
        return False
    for row in rows_now(args):
        fit = row_fit(row)
        if row.get("kind") != "gen" or not fit.get("upgrade"):
            continue
        key = row["model_key"]
        st = load_state(key)
        if st.get("stage") != "published" or st.get("upgraded") or st.get("upgrade_next", 0) > time.time():
            continue
        with lock:
            if key in claimed:
                continue
            claimed.add(key)
        try:
            try:
                if trellis.health().get("busy"):
                    return False
            except Exception:  # noqa: BLE001
                return False
            _last_upgrade[0] = time.time()
            res = str(int(fit["upgrade"]))
            raw = WORK / key / f"raw_r{res}_s{st.get('seed', 0)}.glb"
            log(f"high-detail attempt: TRELLIS {res}", key)
            try:
                meta = trellis.generate(st["concept"], raw, faces=int(fit.get("upgrade_faces", 60000)),
                                        seed=st.get("seed", 0), resolution=res, texture_size="2048")
            except Exception as e:  # noqa: BLE001
                st = load_state(key)
                st["upgrade_tries"] = st.get("upgrade_tries", 0) + 1
                st["upgrade_next"] = time.time() + UPGRADE_EVERY
                st["history"].append({"t": time.time(), "upgrade_error": str(e)[:200]})
                save_state(key, st)
                log(f"high-detail attempt failed (keeping the published model): {str(e)[:120]}", key)
                return True
            st = load_state(key)
            st.update(raw=str(raw), stage="raw", upgraded=res, resolution=res)
            st.pop("front", None)
            st["history"].append({"t": time.time(), "upgrade": res, "seconds": meta.get("generation_seconds")})
            save_state(key, st)
            log(f"high-detail TRELLIS {res}: {meta.get('generation_seconds')} s -> refit", key)
            _last_upgrade[0] = 0.0  # the GPU has memory right now: go on with the next town straight away
            return True
        finally:
            release(key)
    return False


def worker(name, args, stage, func, gate=None):
    pause_until = 0
    while not stop.is_set():
        if time.time() < pause_until or disk_low() or (gate and not gate()):
            time.sleep(10)
            continue
        if name == "trellis" and try_upgrade(args):
            continue
        rows = [r for r in rows_now(args) if r.get("kind") == "gen"]
        row, st = claim(rows, stage)
        if row is None:
            time.sleep(15)
            continue
        key = row["model_key"]
        try:
            res = func(row, st)
            if res == "pause":
                pause_until = time.time() + (300 if name == "trellis" else 60)
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"{name} crashed: {e}\n{traceback.format_exc()}", key)
            st = load_state(key)
            st.setdefault("crashes", 0)
            st["crashes"] += 1
            if st["crashes"] >= 3:
                st["failed"] = f"{name} crashed: {e}"[:200]
            save_state(key, st)
            time.sleep(5)
        finally:
            release(key)


def concept_gate(args):
    def gate():
        if time.time() < codex_pause_until:
            return False
        waiting = len(pending(rows_now(args), "concept_ok"))
        return waiting < args.lookahead
    return gate


def blender_loop(args):
    last_sheet = 0
    published_at_sheet = -1
    while not stop.is_set():
        if disk_low():
            time.sleep(60)
            continue
        rows = rows_now(args)
        busy = False
        for stage, func in (("raw", do_fit), ("fitted", do_publish)):
            row, st = claim([r for r in rows if r.get("kind") == "gen"], stage)
            if row is not None:
                busy = True
                try:
                    func(row, st)
                except Exception as e:  # noqa: BLE001
                    stats["errors"] += 1
                    log(f"blender stage crashed: {e}\n{traceback.format_exc()}", row["model_key"])
                    st = load_state(row["model_key"])
                    st["crashes"] = st.get("crashes", 0) + 1
                    if st["crashes"] >= 3:
                        st["failed"] = f"{stage} crashed: {e}"[:200]
                    save_state(row["model_key"], st)
                finally:
                    release(row["model_key"])
                break
        if not busy:
            try:
                busy = do_clumps(rows)
            except Exception as e:  # noqa: BLE001
                log(f"clumps crashed: {e}\n{traceback.format_exc()}")
        all_rows = [r for r in load_catalog() if r.get("kind") in ("gen", "clump")]
        n = write_manifest(all_rows)
        if (stats["published"] != published_at_sheet and time.time() - last_sheet > 300) or \
                (not busy and stats["published"] != published_at_sheet):
            try:
                import sheets
                sheets.build()
                published_at_sheet = stats["published"]
                last_sheet = time.time()
                log(f"sheets updated; manifest {n} keys; stats {stats}")
            except Exception as e:  # noqa: BLE001
                log(f"sheets failed: {e}")
        if not busy:
            time.sleep(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-priority", type=int, default=9)
    ap.add_argument("--keys", type=lambda s: set(s.split(",")))
    ap.add_argument("--codex-workers", type=int, default=3)
    ap.add_argument("--lookahead", type=int, default=24)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-codex", action="store_true")
    ap.add_argument("--no-trellis", action="store_true")
    ap.add_argument("--no-blender", action="store_true")
    args = ap.parse_args()

    lockf = WORK / "runner.lock"
    WORK.mkdir(parents=True, exist_ok=True)
    if lockf.exists():
        try:
            pid = int(lockf.read_text().strip())
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            if str(pid) in out and "python" in out.lower():
                sys.exit(f"another runner is active (pid {pid}); delete {lockf} if that is wrong")
        except ValueError:
            pass
    lockf.write_text(str(os.getpid()))
    log(f"runner start pid {os.getpid()} args {vars(args)}")
    try:
        apply_review(load_catalog())
        threads = []
        if not args.no_codex:
            for i in range(args.codex_workers):
                threads.append(threading.Thread(target=worker, daemon=True,
                                                args=(f"codex{i}", args, "new", do_concept, concept_gate(args))))
        if not args.no_trellis:
            threads.append(threading.Thread(target=worker, daemon=True,
                                            args=("trellis", args, "concept_ok", do_trellis)))
        if not args.no_blender:
            threads.append(threading.Thread(target=blender_loop, daemon=True, args=(args,)))
        for t in threads:
            t.start()
            time.sleep(2)
        idle = 0
        while True:
            time.sleep(30)
            refresh_catalog()
            apply_review(load_catalog())
            rows = rows_now(args)
            states = [load_state(r["model_key"]) for r in rows]
            open_items = [s for s in states if s.get("stage") != "published" and not s.get("failed")]
            if args.once:
                idle = idle + 1 if not open_items or not claimed else 0
                if not open_items and idle >= 2:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log(f"runner stop; stats {stats}")
        try:
            if lockf.read_text().strip() == str(os.getpid()):
                lockf.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
