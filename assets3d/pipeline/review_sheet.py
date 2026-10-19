"""Contact sheet of models published after a time and not yet in review.tsv (for the next review pass).

usage: review_sheet.py <since 'YYYY-MM-DD HH:MM:SS'> [--per-page 24]   -> sheets/review_NN.jpg
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SHEETS, WORK, load_catalog, load_review, load_state  # noqa: E402
import sheets  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("since")
ap.add_argument("--per-page", type=int, default=24)
a = ap.parse_args()
reviewed = load_review()
items = []
for r in load_catalog():
    k = r["model_key"]
    if r.get("kind") != "gen" or k in reviewed or not (WORK / k / "state.json").exists():
        continue
    st = load_state(k)
    if st.get("stage") != "published" or (st.get("published_at") or "") < a.since:
        continue
    v = Path(st["fit_dir"]) / "views"
    items.append((k, f"{r['category']} p{r['priority']}", [sheets.thumb(v / "game.png", (92, 104, 84, 255)),
                                                            sheets.thumb(v / "front.png", (70, 72, 78, 255))]))
for old in SHEETS.glob("review_*.jpg"):
    old.unlink()
for p in range(0, len(items), a.per_page):
    chunk = items[p:p + a.per_page]
    sheets.page(chunk, f"review: published since {a.since}, {p + 1}-{p + len(chunk)} of {len(items)}",
                SHEETS / f"review_{p // a.per_page + 1:02d}.jpg", 4, 2)
print(len(items), "models")
