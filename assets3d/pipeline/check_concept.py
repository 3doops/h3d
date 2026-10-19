"""Automatic check of a concept image before it goes to TRELLIS.2.

usage: check_concept.py <concept.png> [--json]
Checks: square-ish and large enough; uniform light background along the border; one dominant foreground blob that
does not touch the border; foreground covers a sane share of the picture. Writes <concept>_cut.png (foreground on
transparent background, used by the contact sheets) and prints a JSON verdict {"ok": bool, "problems": [...], ...}.
"""
import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

GRID = 160  # analysis resolution


def components(mask):
    h, w = mask.shape
    lab = np.zeros(mask.shape, dtype=np.int32)
    sizes = []
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not lab[y, x]:
                n = len(sizes) + 1
                q = deque([(y, x)])
                lab[y, x] = n
                cnt = 0
                while q:
                    cy, cx = q.popleft()
                    cnt += 1
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not lab[ny, nx]:
                            lab[ny, nx] = n
                            q.append((ny, nx))
                sizes.append(cnt)
    return lab, sizes


def check(path):
    path = Path(path)
    im = Image.open(path).convert("RGB")
    problems = []
    W, H = im.size
    if min(W, H) < 512:
        problems.append(f"small image {W}x{H}")
    if max(W, H) / min(W, H) > 1.6:  # wide 3:2 pictures are fine for TRELLIS
        problems.append(f"not square {W}x{H}")
    a = np.asarray(im).astype(np.float32)
    band = max(4, int(min(W, H) * 0.02))
    border = np.concatenate([a[:band].reshape(-1, 3), a[-band:].reshape(-1, 3),
                             a[:, :band].reshape(-1, 3), a[:, -band:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    spread = float(np.percentile(np.abs(border - bg).max(axis=1), 90))
    if spread > 28:
        problems.append(f"background not uniform (p90 deviation {spread:.0f})")
    if bg.mean() < 120:
        problems.append(f"background dark ({bg.mean():.0f})")
    dist = np.abs(a - bg).max(axis=2)
    # local background estimate for gradients: threshold relative to border spread
    thr = max(24.0, spread * 1.6)
    fg = dist > thr
    small = np.asarray(Image.fromarray((fg * 255).astype(np.uint8)).resize((GRID, GRID), Image.BOX)) > 96
    lab, sizes = components(small)
    total = int(small.sum())
    info = {"bg": [round(float(v)) for v in bg], "bg_spread": round(spread, 1), "fg_share": round(total / GRID ** 2, 3)}
    if not sizes:
        problems.append("no foreground")
        return {"ok": False, "problems": problems, **info}
    main = int(np.argmax(sizes)) + 1
    big = [s for s in sizes if s >= 0.03 * total]
    info["blobs"] = len(big)
    info["main_share"] = round(max(sizes) / total, 3)
    ys, xs = np.nonzero(lab == main)
    x0, x1, y0, y1 = xs.min() / GRID, (xs.max() + 1) / GRID, ys.min() / GRID, (ys.max() + 1) / GRID
    info["bbox"] = [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]
    if x0 <= 0.006 or y0 <= 0.006 or x1 >= 0.994 or y1 >= 0.994:
        problems.append("subject touches the image border")
    if info["fg_share"] < 0.04:
        problems.append(f"subject too small ({info['fg_share']})")
    if info["fg_share"] > 0.80:
        problems.append(f"subject fills the picture ({info['fg_share']})")
    if len(big) > 3 or info["main_share"] < 0.7:
        problems.append(f"several separate subjects ({len(big)} blobs, main {info['main_share']})")
    # cutout for the sheets
    alpha = np.clip((dist - thr * 0.6) / (thr * 0.8), 0, 1)
    rgba = np.dstack([a, alpha * 255]).astype(np.uint8)
    cut = Image.fromarray(rgba, "RGBA")
    cut.thumbnail((384, 384), Image.LANCZOS)  # only for the contact sheets
    cut.save(path.with_name(path.stem + "_cut.png"), optimize=True)
    return {"ok": not problems, "problems": problems, **info}


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1])))
