"""Finds from which side TRELLIS.2 saw the concept, to turn the model's front (facade) towards the game camera.

usage: orient.py <raw.glb> <concept.png> [--el 35]     (prints JSON)
TRELLIS returns objects in a canonical pose (buildings square with the axes) and infers the camera itself, so the
concept's facade can end up on any side. Here the model's silhouette is rendered (orthographic, numpy + PIL) from
azimuths 0..350 at the concept's elevation and compared (IoU, both normalised to their bounding boxes) with the
concept's silhouette. Azimuth convention as in fit_asset.py: 0 = camera in front (Blender -Y), 90 = from +X.
The concepts are drawn from the front-left, so the facade is the face on the image's right: front F = camera + 45,
snapped to a multiple of 90 for objects squared up with the axes. fit_asset.py then turns the model by -F.
"""
import json
import math
import sys

import numpy as np
from PIL import Image, ImageDraw

N = 112  # silhouette resolution


def load_mesh(path):
    import trimesh
    scene = trimesh.load(path, force="scene")
    mesh = scene.dump(concatenate=True) if hasattr(scene, "dump") else scene
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    # glTF (Y up, front +Z) -> Blender (Z up, front -Y): (x, y, z)_b = (x, -z, y)_g
    vb = np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)
    return vb, f


def norm_mask(mask):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return np.zeros((N, N), bool)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    k = (N - 4) / max(h, w)
    im = Image.fromarray((crop * 255).astype(np.uint8)).resize((max(1, round(w * k)), max(1, round(h * k))),
                                                                 Image.BILINEAR)
    out = Image.new("L", (N, N), 0)
    out.paste(im, ((N - im.width) // 2, (N - im.height) // 2))
    return np.asarray(out) > 127


def concept_mask(path):
    im = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    H, W, _ = im.shape
    band = max(4, int(min(W, H) * 0.02))
    border = np.concatenate([im[:band].reshape(-1, 3), im[-band:].reshape(-1, 3),
                             im[:, :band].reshape(-1, 3), im[:, -band:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    spread = float(np.percentile(np.abs(border - bg).max(axis=1), 90))
    fg = np.abs(im - bg).max(axis=2) > max(24.0, spread * 1.6)
    small = np.asarray(Image.fromarray((fg * 255).astype(np.uint8)).resize((256, 256), Image.BOX)) > 100
    return norm_mask(small)


def render_mask(v, f, az, el, faces_max=12000):
    a, e = math.radians(az), math.radians(el)
    d = np.array([math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)])  # target -> camera
    fwd = -d
    right = np.cross(fwd, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    u, w = v @ right, -(v @ up)
    u0, u1, w0, w1 = u.min(), u.max(), w.min(), w.max()
    s = 250 / max(u1 - u0, w1 - w0, 1e-9)
    pu, pw = (u - u0) * s + 3, (w - w0) * s + 3
    img = Image.new("L", (256, 256), 0)
    dr = ImageDraw.Draw(img)
    ff = f if len(f) <= faces_max else f[np.random.default_rng(0).choice(len(f), faces_max, replace=False)]
    for tri in ff:
        dr.polygon([(pu[i], pw[i]) for i in tri], fill=255)
    return norm_mask(np.asarray(img) > 127)


def estimate(glb, concept, el=35.0, step=10):
    v, f = load_mesh(glb)
    cm = concept_mask(concept)
    scores = []
    for az in range(0, 360, step):
        rm = render_mask(v, f, az, el)
        inter = np.logical_and(cm, rm).sum()
        union = np.logical_or(cm, rm).sum()
        scores.append((inter / max(union, 1), az))
    scores.sort(reverse=True)
    best_iou, cam = scores[0]
    front = (cam + 45) % 360
    front90 = int(round(front / 90.0)) % 4 * 90
    second = next((s for s in scores[1:] if min(abs(s[1] - cam), 360 - abs(s[1] - cam)) > 60), None)
    return {"camera_az": cam, "iou": round(float(best_iou), 3), "front": front, "front90": front90,
            "runner_up": [second[1], round(float(second[0]), 3)] if second else None,
            "top": [[a, round(float(s), 3)] for s, a in scores[:5]]}


if __name__ == "__main__":
    args = sys.argv[1:]
    el = float(args[args.index("--el") + 1]) if "--el" in args else 35.0
    print(json.dumps(estimate(args[0], args[1], el)))
