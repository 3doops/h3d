"""Assembles forest / plant clumps for multi-tile decoration DEFs from finished single-plant models (Blender).

usage: blender -b --factory-startup -P assemble_clump.py -- <job.json>
job.json: {"members": [glb, ...], "out_dir": dir, "density": 1.0,
           "items": [{"def": name, "w": W, "h": H, "block": "8chars/8chars/... (6 rows, '1' = blocked)", "out": file}]}
Every blocked cell of the DEF mask gets one or two plants (random member, jitter, turn and scale; deterministic per
DEF name). Member meshes are shared between instances, so the GLB stores each member once. Frame as in MANIFEST.md:
origin = bottom centre of the w x h cell rectangle, front = +Z glTF (-Y Blender).
"""
import json
import math
import random
import sys
import zlib
from pathlib import Path

import bpy

job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

members = []
for glb in job["members"]:
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=glb)
    new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    for o in list(bpy.data.objects):
        if o not in before and o.type != "MESH":
            bpy.data.objects.remove(o)
    bpy.ops.object.select_all(action="DESELECT")
    for o in new:
        o.select_set(True)
    bpy.context.view_layer.objects.active = new[0]
    if len(new) > 1:
        bpy.ops.object.join()
    m = bpy.context.view_layer.objects.active
    m.parent = None
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    members.append(m.data)
    bpy.data.objects.remove(m)

density = float(job.get("density", 1.0))
for item in job["items"]:
    rng = random.Random(zlib.crc32(item["def"].encode()))
    rows = (item.get("block") or "").split("/")
    cells = [(c, r) for r, line in enumerate(rows) for c, ch in enumerate(line) if ch == "1"]
    w, h = item["w"], item["h"]
    if cells:
        c0 = min(c for c, _ in cells)
        r1 = max(r for _, r in cells)
        c1, r0 = c0 + w - 1, r1 - h + 1
    else:  # no mask: fill the whole rectangle
        cells = [(c, r) for c in range(w) for r in range(h)]
        c0, r0 = 0, 0
    objs = []
    for (c, r) in cells:
        # dense like the map pictures: 2-3 plants per blocked cell (fewer for density < 1), slight overlap
        n = max(1, round(density * (2 + (rng.random() < 0.5))))
        for k in range(n):
            x = (c - c0 + 0.5) - w / 2 + rng.uniform(-0.34, 0.34)
            zg = (r - r0 + 0.5) - h / 2 + rng.uniform(-0.34, 0.34)
            o = bpy.data.objects.new(f"{item['def']}_{len(objs)}", rng.choice(members))
            o.location = (x, -zg, 0)
            o.rotation_euler = (0, 0, rng.uniform(0, 2 * math.pi))
            s = rng.uniform(0.9, 1.35)
            o.scale = (s, s, s * rng.uniform(0.9, 1.1))
            bpy.context.scene.collection.objects.link(o)
            objs.append(o)
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    out = Path(job["out_dir"]) / item["out"]
    formats = [i.identifier for i in
               bpy.ops.export_scene.gltf.get_rna_type().properties["export_image_format"].enum_items]
    bpy.ops.export_scene.gltf(filepath=str(out), export_format="GLB", use_selection=True,
                              export_image_format="JPEG" if "JPEG" in formats else "AUTO")
    for o in objs:
        bpy.data.objects.remove(o)
    print("CLUMP", item["def"], len(objs), out)
print("DONE")
