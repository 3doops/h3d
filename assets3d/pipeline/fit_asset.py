"""Fits a raw TRELLIS.2 GLB into the map frame of the Godot client and renders check views (Blender, headless).

usage: blender -b --factory-startup -P fit_asset.py -- <in.glb> <out_dir> <params.json>
params.json: {"turn": deg, "mode": "footprint"|"size", "w": tiles, "h": tiles, "max_xz": tiles, "max_h": tiles,
              "margin": 0.96, "tris": 20000, "tex": 1024, "flag": false, "gain": 1.0, "render": true, "size": 384}
Writes <out_dir>/model.glb, stats.json, views/{front,right,back,left,game}.png.

Frame (godot/assets3d/MANIFEST.md): 1 unit = 1 tile, Y up, the front (entrance) faces +Z glTF (= -Y in Blender, the
south, towards the game camera), origin = bottom centre of the object's tile rectangle. Blender -> glTF export maps
(x, y, z) to (x, z, -y), so here the front must face -Y and the bottom sits on z = 0.
--turn rotates the raw model around the vertical axis first (TRELLIS puts the concept's viewing side at a fixed
direction; the runner passes the calibrated constant).
Materials keep only the base colour (metallic 0, constant roughness): TRELLIS metallic maps darken the models in
plain lighting. Textures are downscaled to `tex` and exported as JPEG (TRELLIS emits WebP).
"""
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index("--") + 1:]
src, out = Path(argv[0]).resolve(), Path(argv[1]).resolve()
P = {"turn": 0.0, "mode": "size", "w": 1, "h": 1, "max_xz": 0.9, "max_h": 1.2, "margin": 0.96, "tris": 20000,
     "tex": 1024, "flag": False, "gain": 1.0, "render": True, "size": 384, "samples": 16, "roughness": 0.85, "pbr": 0}
P.update(json.loads(Path(argv[2]).read_text(encoding="utf-8")))

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
bpy.ops.import_scene.gltf(filepath=str(src))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not meshes:
    sys.exit("no meshes in " + str(src))
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
model = bpy.context.view_layer.objects.active
model.parent = None
model.matrix_world = model.matrix_world.copy()
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
for o in list(bpy.context.scene.objects):
    if o is not model:
        bpy.data.objects.remove(o)
model.name = out.name
mesh = model.data

# drop tiny disconnected islands (TRELLIS floaters) below 0.5% of the faces
bm = bmesh.new()
bm.from_mesh(mesh)
bm.faces.ensure_lookup_table()
seen, islands = set(), []
for f in bm.faces:
    if f.index in seen:
        continue
    stack, isl = [f], []
    seen.add(f.index)
    while stack:
        g = stack.pop()
        isl.append(g)
        for e in g.edges:
            for h in e.link_faces:
                if h.index not in seen:
                    seen.add(h.index)
                    stack.append(h)
    islands.append(isl)
removed = 0
if len(islands) > 1:
    limit = max(8, int(0.005 * len(bm.faces)))
    zs = [v.co.z for v in bm.verts]
    z_lo, z_hi = min(zs), max(zs)

    def floating(isl):
        """objects on a base: a chunk hanging in the air (TRELLIS lost what held it) goes too"""
        return P.get("align") and len(isl) < 0.25 * len(bm.faces) and \
            min(v.co.z for f in isl for v in f.verts) > z_lo + 0.12 * (z_hi - z_lo)
    kill = [f for isl in islands if len(isl) < limit or floating(isl) for f in isl]
    removed = len(kill)
    if kill and removed < len(bm.faces) * 0.2:
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    else:
        removed = 0
bm.to_mesh(mesh)
bm.free()

def orient_from_concept(concept_path, el=35.0, azimuths=(45, 135, 225, 315), res=112):
    """Camera azimuth from which TRELLIS saw the concept. Calibration (two-colour test house, 2026-10-17):
    TRELLIS.2 returns the object squared up with the axes and puts the concept's camera on a DIAGONAL - which of
    the four varies - with the concept's image-left face and image-right face on the two adjacent sides. So the
    raw model is rendered from the four diagonals with flat light and each view is compared with the concept
    (bounding-box normalised mask IoU + correlation of standardised colours on an 8x8 grid)."""
    import numpy as np
    scene = bpy.context.scene
    try:
        scene.render.engine = "CYCLES"
    except TypeError:
        pass
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.render.film_transparent = True
    scene.render.resolution_x = scene.render.resolution_y = res
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("Flat")
    world.use_nodes = True
    bgn = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bgn.inputs["Color"].default_value = (1, 1, 1, 1)
    bgn.inputs["Strength"].default_value = 1.0
    scene.world = world
    cd = bpy.data.cameras.new("OrientCam")
    cd.type = "ORTHO"
    cam_o = bpy.data.objects.new("OrientCam", cd)
    scene.collection.objects.link(cam_o)
    scene.camera = cam_o
    co = np.array([v.co[:] for v in mesh.vertices])
    center = Vector(((co.min(0) + co.max(0)) / 2).tolist())
    cd.ortho_scale = float(np.linalg.norm(co.max(0) - co.min(0))) * 1.05
    N = 8

    def norm(rgb, mask):
        """crop to the mask, box-average into an N x N grid (aspect kept): mean colour and coverage per cell"""
        ys, xs = np.nonzero(mask)
        if len(xs) < 10:
            return None
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        rgb, mask = rgb[y0:y1, x0:x1], mask[y0:y1, x0:x1].astype(np.float64)
        h, w = mask.shape
        side = max(h, w)
        py, px = (side - h) // 2, (side - w) // 2
        cov = np.zeros((N, N))
        col = np.zeros((N, N, 3))
        yy = ((np.arange(h) + py) * N // side)
        xx = ((np.arange(w) + px) * N // side)
        np.add.at(cov, (yy[:, None], xx[None, :]), mask)
        for c in range(3):
            np.add.at(col[..., c], (yy[:, None], xx[None, :]), rgb[..., c] * mask)
        cnt = np.zeros((N, N))
        np.add.at(cnt, (yy[:, None], xx[None, :]), 1)
        col = col / np.maximum(cov[..., None], 1e-6)
        return cov / np.maximum(cnt, 1) > 0.5, col

    img = bpy.data.images.load(str(concept_path))
    W, H = img.size
    px = np.empty(W * H * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(H, W, 4)[::-1, :, :3]
    band = max(4, int(min(W, H) * 0.02))
    border = np.concatenate([px[:band].reshape(-1, 3), px[-band:].reshape(-1, 3),
                             px[:, :band].reshape(-1, 3), px[:, -band:].reshape(-1, 3)])
    bgc = np.median(border, axis=0)
    spread = float(np.percentile(np.abs(border - bgc).max(axis=1), 90))
    cmask = np.abs(px - bgc).max(axis=2) > max(24 / 255, spread * 1.6)
    ref = norm(px, cmask)
    if ref is None:
        return None
    results = []
    tmp = out / "_orient"
    tmp.mkdir(parents=True, exist_ok=True)
    for az in azimuths:
        a, e = math.radians(az), math.radians(el)
        d = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
        cam_o.location = center + d * 60
        cam_o.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        f = tmp / f"az{az:03d}.png"
        scene.render.filepath = str(f)
        bpy.ops.render.render(write_still=True)
        r = bpy.data.images.load(str(f))
        rp = np.empty(res * res * 4, dtype=np.float32)
        r.pixels.foreach_get(rp)
        rp = rp.reshape(res, res, 4)[::-1]
        bpy.data.images.remove(r)
        got = norm(rp[..., :3], rp[..., 3] > 0.5)
        if got is None:
            continue
        m, col = got
        inter, union = np.logical_and(m, ref[0]), np.logical_or(m, ref[0])
        iou = inter.sum() / max(union.sum(), 1)
        corr = 0.0
        if inter.sum() > 6:
            a1, a2 = col[inter], ref[1][inter]
            a1 = (a1 - a1.mean(0)) / (a1.std(0) + 1e-4)
            a2 = (a2 - a2.mean(0)) / (a2.std(0) + 1e-4)
            corr = float((a1 * a2).mean())
        results.append((iou + corr, az, round(float(iou), 3), round(corr, 3)))
    bpy.data.objects.remove(cam_o)
    bpy.data.images.remove(img)
    results.sort(reverse=True)
    return results


orient = None
front = 0.0
if P.get("front_fixed") is not None:  # decided on the first fit; refits keep it so that review turns stay valid
    front = float(P["front_fixed"])
    orient = {"front": front, "fixed": True}
elif P.get("concept") and P.get("orient", 1):
    res = orient_from_concept(P["concept"])
    if res:
        cam_az = res[0][1]
        front = (cam_az + P.get("view_offset", 45.0)) % 360  # facade = the concept's image-right face
        if P.get("align"):
            front = (round(front / 90.0) % 4) * 90.0
        orient = {"camera_az": cam_az, "front": front, "top": [list(r) for r in res[:4]]}
# turn the facade (azimuth `front`) to the camera (azimuth 0), plus the manual extra turn
mesh.transform(Matrix.Rotation(math.radians(P["turn"] - front), 4, "Z"))

laid = False
if P.get("lay"):
    # small items (swords, rings, amulets) come back standing upright as in the concept; on the map they lie on
    # the ground: tip a tall thin item over onto its widest side
    import numpy as np
    pts = np.array([v.co[:] for v in mesh.vertices])
    ext = pts.max(0) - pts.min(0)
    if ext[2] > 1.6 * max(ext[0], ext[1]):
        axis = "X" if ext[1] <= ext[0] else "Y"
        mesh.transform(Matrix.Rotation(math.radians(90), 4, axis))
        laid = True

align_deg = 0.0
if P.get("align"):
    # objects standing on a base slab / plateau: the concept is drawn in 3/4 view, so TRELLIS returns the base
    # turned by the view angle. Turn it back by the smallest angle (-45..45) that makes the base's bounding
    # rectangle minimal, i.e. square it up with the axes; the facade stays on the side nearest to the front.
    import numpy as np
    pts = np.array([v.co[:] for v in mesh.vertices])
    z0, z1 = pts[:, 2].min(), pts[:, 2].max()
    base = pts[pts[:, 2] <= z0 + 0.15 * (z1 - z0)][:, :2]
    if len(base) < 50:
        base = pts[:, :2]
    best = None
    for a in np.arange(-45.0, 45.0, 1.0):
        r = np.radians(a)
        c, s_ = np.cos(r), np.sin(r)
        x = base[:, 0] * c - base[:, 1] * s_
        y = base[:, 0] * s_ + base[:, 1] * c
        area = (np.percentile(x, 99.5) - np.percentile(x, 0.5)) * (np.percentile(y, 99.5) - np.percentile(y, 0.5))
        if best is None or area < best[0] - 1e-9:
            best = (area, a)
    align_deg = float(best[1])
    mesh.transform(Matrix.Rotation(math.radians(align_deg), 4, "Z"))

mesh.calc_loop_triangles()
tris_in = len(mesh.loop_triangles)
if tris_in > P["tris"]:
    mod = model.modifiers.new("Decimate", "DECIMATE")
    mod.ratio = P["tris"] / tris_in
    bpy.ops.object.modifier_apply(modifier=mod.name)
    mesh = model.data
mesh.calc_loop_triangles()
tris = len(mesh.loop_triangles)

co = [v.co.copy() for v in mesh.vertices]
xmin, xmax = min(c.x for c in co), max(c.x for c in co)
ymin, ymax = min(c.y for c in co), max(c.y for c in co)
zmin, zmax = min(c.z for c in co), max(c.z for c in co)
xw, yd, zh = xmax - xmin, ymax - ymin, zmax - zmin
sx = sy = None
if P["mode"] == "footprint":
    # the box: footprint width x (depth + "back" tiles behind it: many H3 objects block only their front row while
    # the picture rises behind it); the front of the model sits on the front edge of the footprint.
    # x and y may be scaled differently by up to "aniso" to follow the cell rectangle.
    W, D = P["w"] * P["margin"], P["h"] * P["margin"] + P.get("back", 0.0)
    s_w, s_d = W / xw, D / yd
    s = min(s_w, s_d)
    sx, sy = min(s_w, s * P.get("aniso", 1.25)), min(s_d, s * P.get("aniso", 1.25))
    sz = min((sx + sy) / 2, P["max_h"] / zh)
    if sz < (sx + sy) / 2:  # height-limited: shrink everything
        k = sz / ((sx + sy) / 2)
        sx, sy = sx * k, sy * k
    scales = {"width": s_w, "depth": s_d, "height": P["max_h"] / zh}
    limit = min(scales, key=scales.get)
    s = sz
    y_front = -P["h"] / 2 * P["margin"]
    mesh.transform(Matrix.Translation((0, y_front, 0)) @ Matrix.Diagonal((sx, sy, sz, 1))
                   @ Matrix.Translation((-(xmin + xmax) / 2, -ymin, -zmin)))
else:
    scales = {"across": P["max_xz"] * P["margin"] / max(xw, yd), "height": P["max_h"] / zh}
    limit = min(scales, key=scales.get)
    s = scales[limit]
    mesh.transform(Matrix.Scale(s, 4) @ Matrix.Translation((-(xmin + xmax) / 2, -(ymin + ymax) / 2, -zmin)))
mesh.update()
co = [v.co for v in mesh.vertices]
bbox = [[round(min(c[i] for c in co), 3) for i in range(3)], [round(max(c[i] for c in co), 3) for i in range(3)]]

# materials: base colour only
base_images = set()
for mat in mesh.materials:
    if not mat or not mat.node_tree:
        continue
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        continue
    for name, value in (("Metallic", 0.0), ("Roughness", P["roughness"])):
        if P.get("pbr"):  # keep the metallic/roughness maps (gold, gems, polished metal)
            break
        sock = bsdf.inputs[name]
        for link in list(sock.links):
            mat.node_tree.links.remove(link)
        sock.default_value = value
    for name in ("Normal",):
        for link in list(bsdf.inputs[name].links):
            mat.node_tree.links.remove(link)
    for link in bsdf.inputs["Base Color"].links:
        if link.from_node.type == "TEX_IMAGE" and link.from_node.image:
            base_images.add(link.from_node.image)
    for node in list(mat.node_tree.nodes):  # unused texture nodes would still be exported
        if node.type == "TEX_IMAGE" and node.image not in base_images and not node.outputs[0].links:
            mat.node_tree.nodes.remove(node)
for img in base_images:
    if img.size[0] > P["tex"] or img.size[1] > P["tex"]:
        img.scale(min(img.size[0], P["tex"]), min(img.size[1], P["tex"]))
    if P["gain"] != 1.0:
        import numpy as np
        px = np.empty(len(img.pixels), dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(-1, 4)
        px[:, :3] = np.clip(px[:, :3] * P["gain"], 0, 1)
        img.pixels.foreach_set(px.ravel())
        img.update()


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.use_backface_culling = False
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Roughness"].default_value = 0.8
    return mat


def add_flag():
    """Pole with a pennant in owner colour (material PlayerColor) at the front-right of the object, standing on
    whatever surface is there; the client recolours PlayerColor* materials per owner."""
    tree = BVHTree.FromObject(model, bpy.context.evaluated_depsgraph_get())
    x = bbox[1][0] * 0.62
    y = bbox[0][1] * 0.55
    hit = tree.ray_cast(Vector((x, y, 50)), Vector((0, 0, -1)))
    z = hit[0].z if hit[0] else 0.0
    k = max(0.6, min(1.4, (P["w"] + P["h"]) / 4)) if P["mode"] == "footprint" else 0.7
    height, length, drop = 0.55 * k, 0.32 * k, 0.18 * k
    bm = bmesh.new()
    pole = bmesh.ops.create_cone(bm, cap_ends=True, segments=8, radius1=0.02 * k, radius2=0.015 * k,
                                 depth=height + 0.05)
    for v in pole["verts"]:
        v.co += Vector((x, y, z + height / 2 - 0.03))
    upper, lower = [], []
    top, bottom = z + height - 0.02, z + height - 0.02 - drop
    for i in range(9):
        t = i / 8
        px = x + 0.02 * k + t * length
        py = y + 0.03 * k * math.sin(t * 5.5) * t
        half, mid = (top - bottom) / 2 * (1 - t * 0.85), (top + bottom) / 2 - 0.03 * t * k
        upper.append(bm.verts.new((px, py, mid + half)))
        lower.append(bm.verts.new((px, py, mid - half)))
    for i in range(8):
        f = bm.faces.new((lower[i], lower[i + 1], upper[i + 1], upper[i]))
        f.material_index = 1
    flag_mesh = bpy.data.meshes.new("Flag")
    bm.to_mesh(flag_mesh)
    bm.free()
    flag_mesh.materials.append(material("FlagPole", (0.13, 0.11, 0.09, 1)))
    flag_mesh.materials.append(material("PlayerColor", (0.8, 0.05, 0.05, 1)))
    obj = bpy.data.objects.new("Flag", flag_mesh)
    bpy.context.scene.collection.objects.link(obj)
    return [round(x, 3), round(z, 3), round(-y, 3)]


flag_at = add_flag() if P["flag"] else None

out.mkdir(parents=True, exist_ok=True)
formats = [i.identifier for i in bpy.ops.export_scene.gltf.get_rna_type().properties["export_image_format"].enum_items]
bpy.ops.export_scene.gltf(filepath=str(out / "model.glb"), export_format="GLB",
                          export_image_format="JPEG" if "JPEG" in formats else "AUTO", use_selection=False,
                          export_yup=True)
stats = {"source": str(src), "params": P, "tris_in": tris_in, "tris": tris, "islands_removed_faces": removed,
         "orient": orient, "laid": laid, "align_deg": align_deg, "scale": round(s, 4), "scale_xy": [round(sx, 4), round(sy, 4)] if sx else None,
         "limited_by": limit, "bbox_blender": bbox,
         "size_xyz_godot": [round(bbox[1][0] - bbox[0][0], 3), round(bbox[1][2] - bbox[0][2], 3),
                            round(bbox[1][1] - bbox[0][1], 3)],
         "flag_godot": flag_at, "images": [[img.name, list(img.size)] for img in bpy.data.images if img.size[0]]}
(out / "stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
print("STATS", json.dumps(stats))
if not P["render"]:
    print("DONE")
    sys.exit(0)

scene = bpy.context.scene
try:
    scene.render.engine = "CYCLES"
except TypeError as e:
    print("no cycles:", e)
scene.cycles.device = "CPU"
scene.cycles.samples = P["samples"]
scene.cycles.use_denoising = True
scene.render.film_transparent = True
scene.render.resolution_x = scene.render.resolution_y = P["size"]
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "Standard"
world = bpy.data.worlds.new("Sky")
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs["Color"].default_value = (0.85, 0.87, 0.9, 1)
bg.inputs["Strength"].default_value = 0.75
scene.world = world
sun_data = bpy.data.lights.new("Sun", "SUN")
sun_data.energy = 2.8
sun_data.angle = math.radians(4)
sun_data.color = (1.0, 0.93, 0.82)
sun = bpy.data.objects.new("Sun", sun_data)
scene.collection.objects.link(sun)
cam_data = bpy.data.cameras.new("Camera")
cam_data.type = "ORTHO"
cam_data.clip_start, cam_data.clip_end = 0.1, 200
cam = bpy.data.objects.new("Camera", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

# footprint outline on the ground for the views
if P["mode"] == "footprint":
    bm = bmesh.new()
    w2, h2 = P["w"] / 2, P["h"] / 2
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1)
    for v in bm.verts:
        v.co = Vector((math.copysign(w2, v.co.x), math.copysign(h2, v.co.y), -0.004))
    grid_mesh = bpy.data.meshes.new("Footprint")
    bm.to_mesh(grid_mesh)
    bm.free()
    grid_mesh.materials.append(material("FootprintMat", (0.35, 0.45, 0.3, 1)))
    gobj = bpy.data.objects.new("Footprint", grid_mesh)
    scene.collection.objects.link(gobj)

pts = [Vector((x, y, z)) for x in (bbox[0][0], bbox[1][0]) for y in (bbox[0][1], bbox[1][1])
       for z in (bbox[0][2], bbox[1][2])]
center = sum(pts, Vector()) / 8
extent = max((p - center).length for p in pts) * 2


def direction(azimuth, elevation):
    """Unit vector from the target to the camera; azimuth 0 = from the front (south, -Y), 90 = from the east"""
    a, e = math.radians(azimuth), math.radians(elevation)
    return Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))


views = {"front": (0, 15), "right": (90, 15), "back": (180, 15), "left": (270, 15), "game": (0, 50)}
(out / "views").mkdir(exist_ok=True)
for name, (azimuth, elevation) in views.items():
    d = direction(azimuth, elevation)
    cam_data.ortho_scale = extent * 1.02
    cam.location = center + d * 60
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    light = direction(-45, 40)
    sun.rotation_euler = (-light).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(out / "views" / f"{name}.png")
    bpy.ops.render.render(write_still=True)
print("DONE")
