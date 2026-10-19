"""Queue status: counts by priority and stage, failed items, last log lines.

usage: status.py [--failed] [--tail 10]
"""
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LOG, MANIFEST, MODELS, WORK, load_catalog, load_state  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--failed", action="store_true")
    ap.add_argument("--tail", type=int, default=8)
    a = ap.parse_args()
    rows = load_catalog()
    kinds = collections.Counter(r["kind"] for r in rows)
    print("catalog:", len(rows), "models", dict(kinds))
    table = collections.defaultdict(collections.Counter)
    failed = []
    for r in rows:
        if r["kind"] not in ("gen", "clump"):
            continue
        st = load_state(r["model_key"]) if (WORK / r["model_key"] / "state.json").exists() else {"stage": "new"}
        stage = "failed" if st.get("failed") else st.get("stage", "new")
        table[r["priority"]][stage] += 1
        if st.get("failed"):
            failed.append((r["model_key"], st["failed"]))
    stages = ["new", "concept_ok", "raw", "fitted", "published", "failed"]
    print("priority " + " ".join(f"{s:>10}" for s in stages) + "      total")
    for p in sorted(table):
        c = table[p]
        print(f"{p:>8} " + " ".join(f"{c[s]:>10}" for s in stages) + f" {sum(c.values()):>10}")
    glbs = list(MODELS.glob("*.glb"))
    print(f"models dir: {len(glbs)} glb, {sum(g.stat().st_size for g in glbs) / 1e6:.1f} MB; manifest:",
          MANIFEST.exists() and len(__import__('json').loads(MANIFEST.read_text())['models']), "keys")
    if a.failed or failed:
        for k, why in failed:
            print("FAILED", k, why)
    if LOG.exists() and a.tail:
        print("--- log")
        print("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-a.tail:]))


if __name__ == "__main__":
    main()
