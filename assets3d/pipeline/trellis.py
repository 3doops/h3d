"""Image -> textured mesh via the TRELLIS.2 service on the gpu host (one job at a time on one GPU).

usage: trellis.py <image> <out.glb> [--faces 20000] [--seed 0] [--resolution 1024] [--texture-size 2048]
The API key is read from keys/trellis in the repo root and never printed; URL from TRELLIS_URL
(default http://10.10.0.205:8000). Writes <out.glb> and <out>.json (parameters, timing).
Based on necrocastle/ai3d/trellis_gen.py.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

URL = os.environ.get("TRELLIS_URL", "http://10.10.0.205:8000")


class TrellisError(Exception):
    pass


def health():
    return requests.get(f"{URL}/health", timeout=15).json()


def generate(image, out, faces=20000, seed=0, resolution="1024", texture_size="2048", preprocess=True):
    key = (ROOT / "keys" / "trellis").read_text(encoding="utf-8").strip()
    if health().get("status") != "ok":
        raise ConnectionError("TRELLIS health not ok")
    data = {"resolution": str(resolution), "texture_size": str(texture_size),
            "decimation_target": str(max(10000, int(faces))), "seed": str(seed),
            "preprocess": "true" if preprocess else "false"}
    start = time.time()
    with Path(image).open("rb") as f:
        r = requests.post(f"{URL}/generate", headers={"Authorization": f"Bearer {key}"},
                          files={"image": (Path(image).name, f, "image/jpeg" if Path(image).suffix.lower() in
                                           (".jpg", ".jpeg") else "image/png")}, data=data, timeout=2400)
    wall = time.time() - start
    if r.status_code != 200:
        raise TrellisError(f"{r.status_code}: {r.text[:300]}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    meta = {"image": str(image), "params": data, "generation_seconds": r.headers.get("X-Generation-Seconds"),
            "wall_seconds": round(wall, 1), "bytes": len(r.content)}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--faces", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resolution", default="1024", choices=["512", "1024", "1536"])
    ap.add_argument("--texture-size", default="2048", choices=["1024", "2048", "4096"])
    args = ap.parse_args()
    print("health:", health(), file=sys.stderr)
    print(json.dumps(generate(args.image, args.out, args.faces, args.seed, args.resolution, args.texture_size)))


if __name__ == "__main__":
    main()
