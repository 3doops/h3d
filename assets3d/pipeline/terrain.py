"""Seamless terrain albedo textures via Codex image_gen (text only + one style reference).

usage: terrain.py [--only grass,snow] [--workers 3] [--force] [--postprocess-only]
Writes godot/client/assets/terrain/<terrain>_albedo.jpg (1024 px, tileable), work/_terrain/<terrain>_raw_N.png and
sheets/terrain_2x2.jpg (every texture repeated 2x2 to check the seams).
Codex pictures are not exactly tileable, so every texture is made seamless here: the picture is cross-faded with
its half-offset copy (noise-shaped mask), which removes the wrap seams without a visible cross.
"""
import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import A3D, SHEETS, TERRAIN, WORK, log  # noqa: E402
from concept import run_codex  # noqa: E402

# descriptions written from the original terrain tiles (assets/h3-original/adventure_map/terrain/*TL.def interior
# frames; HotA highlands/wasteland from the hota mod terrain DEFs)
TERRAINS = {
    "dirt": "dry brown earth: packed ochre-brown soil with fine grit, tiny pebbles, a few dry grass blades and "
            "faint cracks; warm medium-brown overall.",
    "sand": "pale desert sand: fine light peach-beige sand with soft wind ripples and scattered tiny darker grains.",
    "grass": "lush meadow grass seen from above: dense short green grass, slightly varied greens with a few "
             "darker clumps and tiny specks of clover; no flowers in big patches.",
    "snow": "fresh snow cover: clean white snow with soft blue-grey shading in shallow wind-blown drifts, a few "
            "small icy crystals; very bright overall.",
    "swamp": "marshland: dark green boggy moss and reeds interlaced with irregular small pools of murky blue-teal "
             "water; patches about 30/70 water to vegetation.",
    "rough": "rough stony ground: light grey-brown dry earth covered with many small flat rocks, gravel and "
             "cracked clay plates.",
    "subterranean": "cave floor: smooth light tan-brown packed earth and sandstone dust, soft undulations, a few "
                    "small pebbles; uniform and calm.",
    "lava": "volcanic ground: black and dark grey cooled basalt rubble and ash with a few thin glowing orange "
            "cracks of lava between the rocks.",
    "water": "deep sea surface seen from straight above: dark saturated blue water with gentle small wave "
             "patterns and subtle lighter blue highlights, no foam, no shore.",
    "rock": "solid underground rock mass: very dark charcoal-grey rough stone surface with faint brown veins "
            "and deep crevices; dark overall.",
    "highlands": "highland moor: short olive-green and yellowish grass mixed with brown earth patches, small grey "
                 "stones and heather tufts; rugged upland look.",
    "wasteland": "barren wasteland: cracked dry reddish-brown and ochre earth with scattered grey rubble and "
                 "dead dry grass; desolate.",
}

PROMPT = """Use the built-in image generation tool (image_gen) to create exactly ONE image, then stop. Do not write code, do not run shell commands, do not try to copy files. Reply with one short line naming the generated image.

Use case: texture
Asset type: seamless tileable ground texture (albedo/colour map) for the terrain of a 3D fantasy strategy-game world map seen from high above.
Style reference: the attached image only shows the target realism and colour mood (realistic hand-crafted fantasy diorama); do not copy its content.
Surface: {desc}
View: straight top-down orthographic view of flat ground, filling the whole image edge to edge; no perspective, no horizon, no sky.
Scale: the image covers about 4 x 4 metres of ground; details are small and evenly distributed, no single dominant feature, no large shapes.
Lighting: flat and even, as an albedo map: no directional shadows, no highlights, no vignette, uniform brightness across the whole image.
Tiling: the texture must repeat seamlessly; left/right and top/bottom edges continue each other.
Constraints: no objects (no trees, buildings, creatures, footprints, paths or roads), no text, labels, watermark, border or frame.
Output: square 1:1 image.
"""


def make_seamless(img, seed=0):
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    h, w, _ = a.shape
    shifted = np.roll(a, (h // 2, w // 2), axis=(0, 1))
    yy, xx = np.mgrid[0:h, 0:w]
    # distance to the wrap seams of the original (its borders), normalized 0 (edge) .. 1 (centre)
    dx = np.minimum(xx, w - 1 - xx) / (w / 2)
    dy = np.minimum(yy, h - 1 - yy) / (h / 2)
    rng = np.random.default_rng(seed)
    noise = rng.random((h // 32 + 2, w // 32 + 2)).astype(np.float32)
    noise = np.asarray(Image.fromarray((noise * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)) / 255.0
    m = np.clip((np.minimum(dx, dy) - 0.12 + (noise - 0.5) * 0.25) / 0.3, 0, 1)
    m = m * m * (3 - 2 * m)
    out = a * m[..., None] + shifted * (1 - m[..., None])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def seam_score(img):
    """mean abs difference across the wrap seams relative to inside neighbours (1.0 = as smooth as inside)"""
    a = np.asarray(img.convert("L")).astype(np.float32)
    seam = np.abs(a[:, 0] - a[:, -1]).mean() + np.abs(a[0] - a[-1]).mean()
    inside = np.abs(a[:, 1] - a[:, 0]).mean() + np.abs(a[1] - a[0]).mean()
    return round(float(seam / max(inside, 1e-3)), 2)


def generate(name, force=False):
    wd = WORK / "_terrain"
    wd.mkdir(parents=True, exist_ok=True)
    raw = wd / f"{name}_raw.png"
    if raw.exists() and not force:
        return raw
    prompt = PROMPT.format(desc=TERRAINS[name])
    (wd / f"{name}_prompt.txt").write_text(prompt, encoding="utf-8")
    images, info = run_codex(prompt, wd, name, refs=[A3D / "style_refs" / "style_map_closeup.jpg"])
    if not images:
        log(f"terrain {name}: no image {info}")
        return None
    shutil.copyfile(images[-1], raw)
    log(f"terrain {name}: raw in {info['seconds']} s")
    return raw


def postprocess(name, raw, size=1024):
    img = Image.open(raw).convert("RGB")
    s = min(img.size)
    img = img.crop(((img.width - s) // 2, (img.height - s) // 2, (img.width - s) // 2 + s, (img.height - s) // 2 + s))
    before = seam_score(img)
    img = img.resize((size, size), Image.LANCZOS)
    img = make_seamless(img, seed=len(name))
    after = seam_score(img)
    TERRAIN.mkdir(parents=True, exist_ok=True)
    out = TERRAIN / f"{name}_albedo.jpg"
    img.save(out, quality=90)
    return out, before, after


def sheet(names):
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 16)
    T = 256
    cols = 4
    rows = (len(names) + cols - 1) // cols
    sh = Image.new("RGB", (cols * (2 * T + 12) + 12, rows * (2 * T + 34) + 12), (40, 40, 40))
    d = ImageDraw.Draw(sh)
    for i, n in enumerate(names):
        p = TERRAIN / f"{n}_albedo.jpg"
        if not p.exists():
            continue
        t = Image.open(p).resize((T, T), Image.LANCZOS)
        x, y = 12 + (i % cols) * (2 * T + 12), 12 + (i // cols) * (2 * T + 34)
        for k in range(4):
            sh.paste(t, (x + (k % 2) * T, y + 22 + (k // 2) * T))
        d.text((x, y), f"{n} (2x2 repeat)", font=font, fill=(255, 255, 255))
    SHEETS.mkdir(parents=True, exist_ok=True)
    sh.save(SHEETS / "terrain_2x2.jpg", quality=88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--postprocess-only", action="store_true")
    args = ap.parse_args()
    names = args.only.split(",") if args.only else list(TERRAINS)
    if not args.postprocess_only:
        with ThreadPoolExecutor(args.workers) as ex:
            list(ex.map(lambda n: generate(n, args.force), names))
    for n in names:
        raw = WORK / "_terrain" / f"{n}_raw.png"
        if raw.exists():
            out, b, a = postprocess(n, raw)
            log(f"terrain {n}: {out.name} seam score {b} -> {a}")
    sheet(list(TERRAINS))


if __name__ == "__main__":
    main()
