"""Builds godot/assets3d/catalog.tsv from the model definitions (pipeline/models/*.tsv, hand-written descriptions)
and the extracted sources (pipeline/sources/*.tsv, extract_sources.py).

usage: build_catalog.py            (prints a summary and the DEFs no model claims)

Model definition columns: model_key, category, kind (gen|clump|skip|decal), priority, name, match, footprint,
flag, fit, members, description.  match = space-separated rules:
  def:NAME        this DEF (lowercase, no extension)
  re:REGEX        every DEF whose name matches (Python regex, case-insensitive, full match)
  obj:C:S / obj:C every DEF of that class[:subtype]; also a manifest key obj:C:S / obj:C
  creature:ID     manifest key creature:ID + that creature's map DEF
  heroclass:ID    manifest key hero:class:ID + the class' hero DEFs;  hero  -> generic manifest key "hero"
  town:FID        manifest keys obj:98:FID + every town DEF of the faction (village/fort/citadel/castle/capitol)
  key:K           any other manifest key verbatim
A DEF goes to the most specific rule (def > re > town/creature/heroclass > obj:C:S > obj:C); ties: first file/line.
Files are read in name order. Output columns: see FIELDS; defs = "def:WxH,..." (per-DEF footprint),
manifest_keys = every key the runner writes to manifest.json for this model.
Also writes pipeline/sources/masks.json (def -> block/visit masks) for clump assembly and
pipeline/sources/unassigned.tsv.
"""
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CATALOG, PIPE, read_tsv, write_tsv  # noqa: E402

SRC = PIPE / "sources"
FIELDS = ["model_key", "category", "kind", "priority", "name", "classes", "footprint", "flag", "n_defs", "defs",
          "manifest_keys", "members", "fit", "description"]
RANK = {"def": 0, "re": 1, "town": 2, "creature": 2, "heroclass": 2, "objcs": 3, "objc": 4}

# Test map of the bridge ("The Newcomers", human = red Necropolis): its objects go first (priority 0).
# Sources, first found wins for each kind: the bridge's object list / live snapshot, else our own .h3m scan
# (sources/testmap_newcomers.tsv, def templates of the map via godot/bridge/tools/h3m_scan.py).
BRIDGE_TESTDATA = PIPE.parents[1] / "bridge" / "testdata"
TESTMAP_FIRST = "town_necropolis"
TESTMAP_EXTRA = {"town_necropolis", "hero_generic", "hero_necromancer", "hero_death_knight"}


def testmap():
    defs, keys = set(), set()
    rows = []
    for p in (BRIDGE_TESTDATA / "objects_newcomers.tsv", SRC / "testmap_newcomers.tsv"):
        if p.exists():
            rows = read_tsv(p)
            break
    snap = BRIDGE_TESTDATA / "snapshot_newcomers.json"
    if snap.exists():
        try:
            sd = json.loads(snap.read_text(encoding="utf-8"))
            rows += sd.get("objects", [])
            for h in sd.get("heroes", []):  # heroes and towns of the live game (random ones resolved)
                keys.add(f"hero:class:{h.get('class')}")
                for slot in h.get("army", []):
                    if isinstance(slot, list) and slot:
                        keys.add(f"creature:{slot[0]}")
            for t in sd.get("towns", []):
                keys.add(f"obj:98:{t.get('faction')}")
        except (json.JSONDecodeError, AttributeError):
            pass
    for r in rows:
        d = str(r.get("def") or "").lower().replace(".def", "")
        if d:
            defs.add(d)
        t, s = str(r.get("type", "")), str(r.get("subtype", ""))
        if t:
            keys.add(f"obj:{t}:{s}")
            if t == "54":
                keys.add(f"creature:{s}")
            if t == "34":  # hero on the map: subtype = hero class
                keys.add(f"hero:class:{s}")
    return defs, keys


def main():
    defs = {r["def"].lower(): r for r in read_tsv(SRC / "defs.tsv")}
    creatures = {r["id"]: r for r in read_tsv(SRC / "creatures.tsv")}
    heroclasses = {r["id"]: r for r in read_tsv(SRC / "heroclasses.tsv")}
    factions = {r["id"]: r for r in read_tsv(SRC / "factions.tsv") if r["id"]}
    models = []
    for f in sorted((PIPE / "models").glob("*.tsv")):
        for i, row in enumerate(read_tsv(f)):
            if not row.get("model_key") or row["model_key"].startswith("#"):
                continue
            row["_order"] = (f.name, i)
            models.append(row)
    seen = collections.Counter(m["model_key"] for m in models)
    dups = [k for k, n in seen.items() if n > 1]
    if dups:
        sys.exit(f"duplicate model keys: {dups}")

    claims = {}  # def -> (rank, order, model_key)
    mkeys = collections.defaultdict(list)
    classes = collections.defaultdict(set)

    def claim(d, rank, m):
        d = d.lower()
        cur = claims.get(d)
        cand = (rank, m["_order"], m["model_key"])
        if cur is None or cand[:2] < cur[:2]:
            claims[d] = cand

    for m in models:
        for tok in (m.get("match") or "").split():
            kind, _, val = tok.partition(":")
            if kind == "def":
                claim(val, RANK["def"], m)
            elif kind == "re":
                rx = re.compile(val, re.I)
                for d in defs:
                    if rx.fullmatch(d):
                        claim(d, RANK["re"], m)
            elif kind == "obj":
                parts = val.split(":")
                c = parts[0]
                s = parts[1] if len(parts) > 1 else None
                for d, r in defs.items():
                    if r["class"] == c and (s is None or r["subtype"] == s):
                        claim(d, RANK["objcs" if s is not None else "objc"], m)
                mkeys[m["model_key"]].append(f"obj:{val}")
                classes[m["model_key"]].add(val)
            elif kind == "creature":
                cr = creatures.get(val)
                if cr and cr.get("map_def"):
                    claim(cr["map_def"], RANK["creature"], m)
                mkeys[m["model_key"]].append(f"creature:{val}")
                classes[m["model_key"]].add(f"54:{val}")
            elif kind == "heroclass":
                hc = heroclasses.get(val)
                if hc:
                    for d in (hc.get("map_defs") or "").split(";"):
                        if d:
                            claim(d, RANK["heroclass"], m)
                mkeys[m["model_key"]].append(f"hero:class:{val}")
                classes[m["model_key"]].add(f"hero class {val}")
            elif kind == "hero":
                mkeys[m["model_key"]].append("hero")
            elif kind == "town":
                fr = factions.get(val)
                if fr:
                    for col in ("village_def", "fort_def", "citadel_def", "castle_def", "capitol_def"):
                        if fr.get(col):
                            claim(fr[col], RANK["town"], m)
                for d, r in defs.items():
                    if r["class"] == "98" and r["subtype"] == val:
                        claim(d, RANK["town"], m)
                mkeys[m["model_key"]].append(f"obj:98:{val}")
                classes[m["model_key"]].add(f"98:{val}")
            elif kind == "key":
                mkeys[m["model_key"]].append(val)
            else:
                print(f"warning: {m['model_key']}: unknown rule {tok}")

    by_model = collections.defaultdict(list)
    for d, (_, _, k) in claims.items():
        by_model[k].append(d)

    out = []
    for m in models:
        k = m["model_key"]
        ds = sorted(by_model.get(k, []))
        fps = collections.Counter()
        items = []
        for d in ds:
            r = defs.get(d)
            ok = r and r.get("w") not in (None, "", "0") and r.get("h") not in (None, "", "0")
            fp = f"{r['w']}x{r['h']}" if ok else (m.get("footprint") or "1x1")  # no mask in the sources
            fps[fp] += 1
            items.append(f"{d}:{fp}")
            if r:
                classes[k].add(f"{r['class']}:{r['subtype']}")
        footprint = m.get("footprint") or (fps.most_common(1)[0][0] if fps else "1x1")
        mk = [f"def:{d}" for d in ds] + mkeys.get(k, [])
        out.append({**m, "footprint": footprint, "classes": " ".join(sorted(classes[k])), "n_defs": len(ds),
                    "defs": ",".join(items), "manifest_keys": " ".join(dict.fromkeys(mk)),
                    "flag": m.get("flag") or "0", "priority": m.get("priority") or "3"})
    # test map first (priority 0): every model used by the bridge test map, the player's town first
    tm_defs, tm_keys = testmap()
    if tm_defs or tm_keys:
        for r in out:
            if r["kind"] not in ("gen", "clump"):
                continue
            ds = {x.split(":")[0] for x in r["defs"].split(",") if x}
            mk = set(r["manifest_keys"].split())
            if ds & tm_defs or mk & tm_keys or r["model_key"] in TESTMAP_EXTRA:
                r["priority"] = "0"
        out.sort(key=lambda r: (0 if r["model_key"] == TESTMAP_FIRST else 1))
    write_tsv(CATALOG, out, FIELDS)
    if tm_defs or tm_keys:
        print(f"test map: {len(tm_defs)} defs / {len(tm_keys)} keys -> "
              f"{sum(r['priority'] == '0' for r in out)} models at priority 0")

    masks = {d: {"block": r.get("block"), "visit": r.get("visit"), "w": r.get("w"), "h": r.get("h")}
             for d, r in defs.items()}
    (SRC / "masks.json").write_text(json.dumps(masks), encoding="utf-8")
    unassigned = [r for d, r in defs.items() if d not in claims]
    write_tsv(SRC / "unassigned.tsv", unassigned, list(next(iter(defs.values())).keys()) if defs else ["def"])

    kinds = collections.Counter(r["kind"] for r in out)
    pri = collections.Counter((r["priority"], r["kind"]) for r in out if r["kind"] in ("gen", "clump"))
    print(f"catalog: {len(out)} models {dict(kinds)}; gen/clump by priority {sorted(pri.items())}")
    print(f"defs: {len(defs)} total, {len(claims)} assigned, {len(unassigned)} unassigned")
    cnt = collections.Counter(f"{r['class']} {r['class_name']}" for r in unassigned)
    for c, n in sorted(cnt.items(), key=lambda x: -x[1])[:60]:
        print(f"  unassigned {n:4d}  {c}")
    crs = set(creatures) - {m.split(":")[1] for r in out for m in r["manifest_keys"].split()
                            if m.startswith("creature:")}
    print(f"creatures without model: {len(crs)}")
    hcs = set(heroclasses) - {m.split(":")[2] for r in out for m in r["manifest_keys"].split()
                              if m.startswith("hero:class:")}
    print(f"hero classes without model: {sorted(hcs, key=int)}")


if __name__ == "__main__":
    main()
