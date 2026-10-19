"""Contact sheets for visual review.

usage: sheets.py [--keys k1,k2] [--per-page 30]
Writes godot/assets3d/sheets/concepts_NN.png (latest concept of every model that has one, cut out on grey) and
models_NN.png (fitted model: game camera view + front view) in catalog order, plus sheets/index.tsv (sheet -> keys).
Labels: key, stage, and 'REJ' / 'auto!' marks for rejected or auto-failed items.
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SHEETS, WORK, load_catalog, load_state, write_tsv  # noqa: E402

CELL = 220
try:
    FONT = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 13)
    TITLE = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 18)
except OSError:
    FONT = TITLE = ImageFont.load_default()


def thumb(path, bg, size=CELL):
    cell = Image.new("RGBA", (size, size), bg)
    if path and Path(path).exists():
        im = Image.open(path).convert("RGBA")
        bb = im.getbbox()
        if bb:
            im = im.crop(bb)
        k = min((size - 8) / im.width, (size - 8) / im.height)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
        cell.alpha_composite(im, ((size - im.width) // 2, (size - im.height) // 2))
    return cell


def latest_concept(key, st):
    c = st.get("concept")
    if c:
        cut = Path(c).with_name(Path(c).stem + "_cut.png")
        return cut if cut.exists() else Path(c)
    return None


def page(items, title, out, cols=6, cells_per_item=1):
    rows = (len(items) + cols - 1) // cols
    LABEL = 34
    W = 10 + cols * (CELL * cells_per_item + 10)
    H = 40 + rows * (CELL + LABEL + 10)
    sheet = Image.new("RGBA", (W, H), (36, 37, 40, 255))
    d = ImageDraw.Draw(sheet)
    d.text((10, 10), title, font=TITLE, fill=(240, 240, 240, 255))
    for i, (label, sub, cells) in enumerate(items):
        x = 10 + (i % cols) * (CELL * cells_per_item + 10)
        y = 40 + (i // cols) * (CELL + LABEL + 10)
        for j, c in enumerate(cells):
            sheet.alpha_composite(c, (x + j * CELL, y))
        d.text((x + 2, y + CELL + 2), label, font=FONT, fill=(255, 255, 255, 255))
        d.text((x + 2, y + CELL + 17), sub, font=FONT, fill=(200, 200, 120, 255))
    sheet.convert("RGB").save(out, quality=88)


def build(keys=None, per_page=30):
    SHEETS.mkdir(parents=True, exist_ok=True)
    cat = [r for r in load_catalog() if r.get("kind") == "gen"]
    if keys:
        cat = [r for r in cat if r["model_key"] in keys]
    concepts, models = [], []
    for r in cat:
        key = r["model_key"]
        if not (WORK / key / "state.json").exists():
            continue
        st = load_state(key)
        mark = []
        if st.get("auto_concept", {}).get("ok") is False:
            mark.append("auto!")
        if st.get("rejected"):
            mark.append("REJ")
        sub = f"p{r.get('priority')} {st.get('stage')} " + " ".join(mark)
        c = latest_concept(key, st)
        if c:
            concepts.append((key, sub, [thumb(c, (150, 150, 150, 255))], key))
        fit = st.get("fit_dir")
        if fit and (Path(fit) / "views" / "game.png").exists():
            models.append((key, sub + f" {st.get('tris', '')}t",
                           [thumb(Path(fit) / "views" / "game.png", (92, 104, 84, 255)),
                            thumb(Path(fit) / "views" / "front.png", (70, 72, 78, 255))], key))
    index = []
    for kind, items, cols, cpi in (("concepts", concepts, 6, 1), ("models", models, 4, 2)):
        for old in SHEETS.glob(f"{kind}_*.jpg"):
            old.unlink()
        for p in range(0, len(items), per_page):
            chunk = items[p:p + per_page]
            out = SHEETS / f"{kind}_{p // per_page + 1:02d}.jpg"
            page([(a, b, c) for a, b, c, _ in chunk], f"{kind} {p + 1}-{p + len(chunk)} of {len(items)}", out,
                 cols, cpi)
            index += [{"sheet": out.name, "key": k} for *_, k in chunk]
    write_tsv(SHEETS / "index.tsv", index, ["sheet", "key"])
    return len(concepts), len(models)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys")
    ap.add_argument("--per-page", type=int, default=30)
    a = ap.parse_args()
    print(build(set(a.keys.split(",")) if a.keys else None, a.per_page))
