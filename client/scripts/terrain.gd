extends Node3D
## One map level: heightfield mesh from the tile arrays, terrain shader (blending, roads, rivers, fog of war),
## water plane, fog-of-war clouds. Also answers height queries and ray picks for this level.

const Defs = preload("res://scripts/defs.gd")
const S := 4            # vertices per cell
const B := 10           # border cells around the map
const WATER_Y := -0.2
const CLOUD_Y := 0.9
const PR := 8           # path field texels per cell
const PMAX := 0.7       # path field range (cells)
const DIRS := [Vector2i(1, 0), Vector2i(1, 1), Vector2i(0, 1), Vector2i(-1, 1), Vector2i(-1, 0), Vector2i(-1, -1),
	Vector2i(0, -1), Vector2i(1, -1)]

var n := 48
var level := 0
var terrain := PackedInt32Array()
var road := PackedInt32Array()
var river := PackedInt32Array()
var fog := PackedByteArray()
var heights := PackedFloat32Array()
var road_f := PackedFloat32Array()
var river_f := PackedFloat32Array()
var path_img: Image
var _warp: FastNoiseLite
var gw := 0
var mat: ShaderMaterial
var water_mat: ShaderMaterial
var cloud_mat: ShaderMaterial
var fog_img: Image
var fog_tex: ImageTexture
var mesh_inst: MeshInstance3D


func setup(map: Dictionary, fog_rows: Array, z: int, objects: Array, shared: Dictionary) -> void:
	n = int(map.size)
	level = z
	terrain = PackedInt32Array(map.terrain[z])
	road = PackedInt32Array(map.road[z]) if map.has("road") else PackedInt32Array()
	river = PackedInt32Array(map.river[z]) if map.has("river") else PackedInt32Array()
	if road.size() != n * n:
		road.resize(n * n)
	if river.size() != n * n:
		river.resize(n * n)
	fog = PackedByteArray()
	fog.resize(n * n)
	var fz: Array = fog_rows[z] if z < fog_rows.size() else []
	for i in n * n:
		fog[i] = 1 if i < fz.size() and int(fz[i]) != 0 else 0
	var t0 := Time.get_ticks_msec()
	_build_heights(objects)
	var t1 := Time.get_ticks_msec()
	_build_mesh(shared)
	_build_water(shared)
	_build_clouds(shared)
	print("terrain level %d: heights %d ms, mesh %d ms" % [z, t1 - t0, Time.get_ticks_msec() - t1])


func tidx(x: int, y: int) -> int:
	return clampi(y, 0, n - 1) * n + clampi(x, 0, n - 1)


func ttype(x: int, y: int) -> int:
	return terrain[tidx(x, y)]


func conn_bits(layer: PackedInt32Array, x: int, y: int, to_water: bool) -> int:
	if layer[tidx(x, y)] == 0:
		return 0
	var bits := 0
	var has := func(cx: int, cy: int) -> bool:
		if cx < 0 or cy < 0 or cx >= n or cy >= n:
			return false
		return layer[cy * n + cx] != 0
	for k in 8:
		var d: Vector2i = DIRS[k]
		var cx := x + d.x
		var cy := y + d.y
		if k % 2 == 0:
			if has.call(cx, cy):
				bits |= 1 << k
			elif to_water and cx >= 0 and cy >= 0 and cx < n and cy < n and terrain[cy * n + cx] == Defs.WATER:
				bits |= 1 << k
		else:
			if has.call(cx, cy) and not has.call(x + d.x, y) and not has.call(x, y + d.y):
				bits |= 1 << k
	return bits


# ------------------------------------------------------------------------------------------- heights

func _build_heights(objects: Array) -> void:
	var nn := n * n
	var base := PackedFloat32Array()
	var amp := PackedFloat32Array()
	var rock := PackedFloat32Array()
	base.resize(nn)
	amp.resize(nn)
	rock.resize(nn)
	for i in nn:
		var t := terrain[i]
		var b := 0.0
		var a := 0.25
		match t:
			Defs.WATER: b = -0.85; a = 0.1
			Defs.SNOW: a = 0.34
			Defs.GRASS: a = 0.26
			Defs.DIRT: a = 0.2
			Defs.SAND: b = -0.03; a = 0.12
			Defs.SWAMP: b = -0.07; a = 0.06
			Defs.ROUGH: b = 0.03; a = 0.36
			Defs.SUBTER: a = 0.2
			Defs.LAVA: b = -0.04; a = 0.16
			Defs.ROCK: a = 0.3; rock[i] = 1.0
			Defs.HIGHLANDS: b = 0.1; a = 0.5
			Defs.WASTELAND: a = 0.28
		base[i] = b
		amp[i] = a
	# buildings stand on flattened ground
	for o in objects:
		if int(o.z) != level:
			continue
		var cat := Defs.category(o)
		if cat in Defs.BUILDING_CATS or cat in ["chest", "resource", "artifact", "campfire", "monster"]:
			var f := 0.12 if cat in Defs.BUILDING_CATS else 0.4
			for c in Defs.obj_cells(o):
				if c.x >= 0 and c.y >= 0 and c.x < n and c.y < n:
					amp[c.y * n + c.x] = minf(amp[c.y * n + c.x], f * amp[c.y * n + c.x])
		elif cat == "mountain":
			for c in Defs.obj_cells(o):
				if c.x >= 0 and c.y >= 0 and c.x < n and c.y < n:
					base[c.y * n + c.x] += 0.12
	gw = (n + 2 * B) * S + 1
	var g := gw * gw
	heights.resize(g)
	_build_path_field()
	var noise := FastNoiseLite.new()
	noise.seed = 1234 + level
	noise.noise_type = FastNoiseLite.TYPE_SIMPLEX_SMOOTH
	noise.frequency = 0.11
	noise.fractal_octaves = 4
	var ridge := FastNoiseLite.new()
	ridge.seed = 77 + level
	ridge.frequency = 0.35
	ridge.fractal_type = FastNoiseLite.FRACTAL_RIDGED
	ridge.fractal_octaves = 3
	var dw := FastNoiseLite.new()
	dw.seed = 31 + level
	dw.frequency = 0.16
	dw.fractal_octaves = 3
	var inv := 1.0 / S
	for gy in gw:
		var wy := -B + gy * inv
		for gx in gw:
			var wx := -B + gx * inv
			var u := wx - 0.5 + dw.get_noise_2d(wx, wy) * 0.42
			var v := wy - 0.5 + dw.get_noise_2d(wx + 53.1, wy - 11.7) * 0.42
			var y0 := floori(v)
			var fy := v - y0
			var sy := fy * fy * (3.0 - 2.0 * fy)
			var ry0 := clampi(y0, 0, n - 1) * n
			var ry1 := clampi(y0 + 1, 0, n - 1) * n
			var x0 := floori(u)
			var fx := u - x0
			var sx := fx * fx * (3.0 - 2.0 * fx)
			var cx0 := clampi(x0, 0, n - 1)
			var cx1 := clampi(x0 + 1, 0, n - 1)
			var i00 := ry0 + cx0
			var i10 := ry0 + cx1
			var i01 := ry1 + cx0
			var i11 := ry1 + cx1
			var bv := lerpf(lerpf(base[i00], base[i10], sx), lerpf(base[i01], base[i11], sx), sy)
			var av := lerpf(lerpf(amp[i00], amp[i10], sx), lerpf(amp[i01], amp[i11], sx), sy)
			var rv := lerpf(lerpf(rock[i00], rock[i10], fx), lerpf(rock[i01], rock[i11], fx), fy)
			var gi := gy * gw + gx
			var rd := field_at(river_f, wx, wy)
			var od := field_at(road_f, wx, wy)
			av *= clampf((rd - 0.15) * 2.0, 0.15, 1.0) * clampf(0.45 + od * 1.5, 0.45, 1.0)
			var h := bv + noise.get_noise_2d(wx, wy) * av
			if rv > 0.0:
				var rs := smoothstep(0.32, 0.68, rv)
				h += rs * (1.15 + 0.55 * ridge.get_noise_2d(wx, wy) + 0.25 * noise.get_noise_2d(wx * 3.0, wy * 3.0))
			if rd < 0.45:
				h -= 0.13 * smoothstep(0.42, 0.1, rd)
			# outer skirt: sink the very edge of the border so it never shows a cut
			var edge := minf(minf(wx + B, n + B - wx), minf(wy + B, n + B - wy))
			if edge < 2.0:
				h = lerpf(-1.4, h, edge / 2.0)
			heights[gi] = h


## Fine distance field for roads and rivers (PR texels per cell): rounded curves through the cells.
## path_img: R road distance, G river distance (0..1 = 0..PMAX cells), B road type, A river type.
func _build_path_field() -> void:
	var w := n * PR
	road_f = PackedFloat32Array()
	river_f = PackedFloat32Array()
	road_f.resize(w * w)
	river_f.resize(w * w)
	road_f.fill(PMAX)
	river_f.fill(PMAX)
	var road_t := PackedByteArray()
	var river_t := PackedByteArray()
	road_t.resize(w * w)
	river_t.resize(w * w)
	_stamp_layer(road, false, road_f, road_t)
	_stamp_layer(river, true, river_f, river_t)
	path_img = Image.create(w, w, false, Image.FORMAT_RGBA8)
	var data := PackedByteArray()
	data.resize(w * w * 4)
	for i in w * w:
		data[i * 4] = int(clampf(road_f[i] / PMAX, 0.0, 1.0) * 255.0)
		data[i * 4 + 1] = int(clampf(river_f[i] / PMAX, 0.0, 1.0) * 255.0)
		data[i * 4 + 2] = road_t[i]
		data[i * 4 + 3] = river_t[i]
	path_img.set_data(w, w, false, Image.FORMAT_RGBA8, data)


func _cell_curves(layer: PackedInt32Array, x: int, y: int, to_water: bool) -> Array:
	var bits := conn_bits(layer, x, y, to_water)
	var c := Vector2(x + 0.5, y + 0.5)
	var ends: Array[Vector2] = []
	for k in 8:
		if bits & (1 << k):
			ends.append(c + Vector2(DIRS[k]) * 0.5)
	var amp := 0.2 if to_water else 0.12
	if ends.size() == 2:
		var a: Vector2 = ends[0]
		var b: Vector2 = ends[1]
		var pts := PackedVector2Array()
		for i in 13:
			var t := i / 12.0
			pts.append(_meander(a.lerp(c, t).lerp(c.lerp(b, t), t), amp))
		return [pts]
	var out := []
	if ends.is_empty():
		out.append(PackedVector2Array([_meander(c, amp), _meander(c, amp) + Vector2(0.01, 0)]))
	for e in ends:
		var pts := PackedVector2Array()
		for i in 7:
			pts.append(_meander(c.lerp(e, i / 6.0), amp))
		out.append(pts)
	return out


## Smooth displacement field so straight road/river runs meander (same at shared cell edges).
func _meander(p: Vector2, amp: float) -> Vector2:
	if _warp == null:
		_warp = FastNoiseLite.new()
		_warp.seed = 99 + level
		_warp.frequency = 0.22
		_warp.fractal_octaves = 2
	return p + Vector2(_warp.get_noise_2d(p.x, p.y), _warp.get_noise_2d(p.x + 71.3, p.y - 17.9)) * amp * 2.0


func _stamp_layer(layer: PackedInt32Array, to_water: bool, dist: PackedFloat32Array, types: PackedByteArray) -> void:
	var w := n * PR
	var r := PMAX
	for y in n:
		for x in n:
			var tv := layer[y * n + x]
			if tv == 0:
				continue
			for pts in _cell_curves(layer, x, y, to_water):
				for i in pts.size() - 1:
					var a: Vector2 = pts[i] * PR
					var b: Vector2 = pts[i + 1] * PR
					var rr := r * PR
					var tx0 := clampi(int(minf(a.x, b.x) - rr), 0, w - 1)
					var tx1 := clampi(int(maxf(a.x, b.x) + rr) + 1, 0, w - 1)
					var ty0 := clampi(int(minf(a.y, b.y) - rr), 0, w - 1)
					var ty1 := clampi(int(maxf(a.y, b.y) + rr) + 1, 0, w - 1)
					var ab := b - a
					var l2 := maxf(ab.length_squared(), 1e-6)
					for ty in range(ty0, ty1 + 1):
						for tx in range(tx0, tx1 + 1):
							var p := Vector2(tx + 0.5, ty + 0.5)
							var tt := clampf((p - a).dot(ab) / l2, 0.0, 1.0)
							var dd := p.distance_to(a + ab * tt) / PR
							var ti := ty * w + tx
							if dd < dist[ti]:
								dist[ti] = dd
								types[ti] = tv


## Distance from world point to the nearest road/river line (cells), PMAX if none nearby.
func field_at(f: PackedFloat32Array, wx: float, wy: float) -> float:
	var w := n * PR
	var tx := int(wx * PR)
	var ty := int(wy * PR)
	if tx < 0 or ty < 0 or tx >= w or ty >= w:
		return PMAX
	return f[ty * w + tx]


## Terrain height at world (x, z) (cell units, cell (cx,cy) spans [cx,cx+1]).
func height_at(wx: float, wz: float) -> float:
	var gx := clampf((wx + B) * S, 0.0, gw - 1.001)
	var gy := clampf((wz + B) * S, 0.0, gw - 1.001)
	var x0 := int(gx)
	var y0 := int(gy)
	var fx := gx - x0
	var fy := gy - y0
	var i := y0 * gw + x0
	return lerpf(lerpf(heights[i], heights[i + 1], fx), lerpf(heights[i + gw], heights[i + gw + 1], fx), fy)


## Ground height for placing things (never below the water surface on land cells).
func ground_at(wx: float, wz: float) -> float:
	return height_at(wx, wz)


func ray_pick(from: Vector3, dir: Vector3) -> Variant:
	var t := 0.0
	var prev := from
	var step := 0.25
	for i in 1200:
		t += step
		var p := from + dir * t
		var h := maxf(height_at(p.x, p.z), WATER_Y)
		if p.y <= h:
			var lo := t - step
			var hi := t
			for k in 10:
				var mid := (lo + hi) * 0.5
				var q := from + dir * mid
				if q.y <= maxf(height_at(q.x, q.z), WATER_Y):
					hi = mid
				else:
					lo = mid
			return from + dir * hi
		prev = p
		if p.y < -5.0:
			break
	return null


# ------------------------------------------------------------------------------------------- mesh

func _build_mesh(shared: Dictionary) -> void:
	var verts := PackedVector3Array()
	var norms := PackedVector3Array()
	verts.resize(gw * gw)
	norms.resize(gw * gw)
	var inv := 1.0 / S
	for gy in gw:
		for gx in gw:
			var gi := gy * gw + gx
			verts[gi] = Vector3(-B + gx * inv, heights[gi], -B + gy * inv)
			var hl := heights[gy * gw + maxi(gx - 1, 0)]
			var hr := heights[gy * gw + mini(gx + 1, gw - 1)]
			var hu := heights[maxi(gy - 1, 0) * gw + gx]
			var hd := heights[mini(gy + 1, gw - 1) * gw + gx]
			norms[gi] = Vector3(hl - hr, 2.0 * inv, hu - hd).normalized()
	var idx := PackedInt32Array()
	idx.resize((gw - 1) * (gw - 1) * 6)
	var k := 0
	for gy in gw - 1:
		for gx in gw - 1:
			var a := gy * gw + gx
			var b := a + 1
			var c := a + gw
			var d := c + 1
			idx[k] = a; idx[k + 1] = b; idx[k + 2] = c
			idx[k + 3] = b; idx[k + 4] = d; idx[k + 5] = c
			k += 6
	var arrays := []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = verts
	arrays[Mesh.ARRAY_NORMAL] = norms
	arrays[Mesh.ARRAY_INDEX] = idx
	var mesh := ArrayMesh.new()
	mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)

	# data textures
	var cell := Image.create(n, n, false, Image.FORMAT_RGBA8)
	for y in n:
		for x in n:
			var i := y * n + x
			cell.set_pixel(x, y, Color8(terrain[i], road[i], river[i], 0))
	fog_img = Image.create(n + 2 * B, n + 2 * B, false, Image.FORMAT_L8)
	_fill_fog_image()
	fog_tex = ImageTexture.create_from_image(fog_img)

	mat = ShaderMaterial.new()
	mat.shader = shared.terrain_shader
	mat.set_shader_parameter("cell_tex", ImageTexture.create_from_image(cell))
	mat.set_shader_parameter("path_tex", ImageTexture.create_from_image(path_img))
	mat.set_shader_parameter("fog_tex", fog_tex)
	mat.set_shader_parameter("noise_tex", shared.noise_tex)
	mat.set_shader_parameter("noise_nrm", shared.noise_nrm)
	mat.set_shader_parameter("crack_tex", shared.crack_tex)
	mat.set_shader_parameter("map_size", float(n))
	mat.set_shader_parameter("border", float(B))
	mat.set_shader_parameter("level", level)
	if shared.has("tex_array"):
		mat.set_shader_parameter("tex_array", shared.tex_array)
		mat.set_shader_parameter("tex_mask", shared.tex_mask)
	mesh_inst = MeshInstance3D.new()
	mesh_inst.name = "Ground"
	mesh_inst.mesh = mesh
	mesh_inst.material_override = mat
	add_child(mesh_inst)


func _fill_fog_image() -> void:
	var w := n + 2 * B
	for y in w:
		for x in w:
			var cx := x - B
			var cy := y - B
			var v := 0
			if cx >= 0 and cy >= 0 and cx < n and cy < n and fog[cy * n + cx] != 0:
				v = 255
			fog_img.set_pixel(x, y, Color8(v, v, v))


func open_fog(cells: Array) -> void:
	var changed := false
	for c in cells:
		if int(c[2]) != level:
			continue
		var x := int(c[0])
		var y := int(c[1])
		if x < 0 or y < 0 or x >= n or y >= n:
			continue
		if fog[y * n + x] == 0:
			fog[y * n + x] = 1
			fog_img.set_pixel(x + B, y + B, Color8(255, 255, 255))
			changed = true
	if changed:
		fog_tex.update(fog_img)


func set_fog_all(rows: Array) -> void:
	var fz: Array = rows[level] if level < rows.size() else []
	for i in n * n:
		fog[i] = 1 if i < fz.size() and int(fz[i]) != 0 else 0
	_fill_fog_image()
	fog_tex.update(fog_img)


func is_open(x: int, y: int) -> bool:
	if x < 0 or y < 0 or x >= n or y >= n:
		return false
	return fog[y * n + x] != 0


func set_hover(cell: Vector2i) -> void:
	mat.set_shader_parameter("hover_cell", Vector2(cell))


func set_grid(on: bool) -> void:
	mat.set_shader_parameter("show_grid", on)


func _build_water(shared: Dictionary) -> void:
	var has_water := false
	for t in terrain:
		if t == Defs.WATER:
			has_water = true
			break
	if not has_water and level > 0:
		return
	var pm := PlaneMesh.new()
	var ext := float(n + 2 * B) + (160.0 if level == 0 else 0.0)
	pm.size = Vector2(ext, ext)
	pm.subdivide_width = 0
	pm.subdivide_depth = 0
	var mi := MeshInstance3D.new()
	mi.name = "Water"
	mi.mesh = pm
	mi.position = Vector3(n * 0.5, WATER_Y, n * 0.5)
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	water_mat = ShaderMaterial.new()
	water_mat.shader = shared.water_shader
	water_mat.set_shader_parameter("noise_nrm", shared.noise_nrm)
	water_mat.set_shader_parameter("noise_tex", shared.noise_tex)
	water_mat.set_shader_parameter("fog_tex", fog_tex)
	water_mat.set_shader_parameter("map_size", float(n))
	water_mat.set_shader_parameter("border", float(B))
	water_mat.set_shader_parameter("underground", level > 0)
	mi.material_override = water_mat
	add_child(mi)


func _build_clouds(shared: Dictionary) -> void:
	var pm := PlaneMesh.new()
	pm.size = Vector2(n + 2 * B + 60, n + 2 * B + 60)
	var mi := MeshInstance3D.new()
	mi.name = "FogClouds"
	mi.mesh = pm
	mi.position = Vector3(n * 0.5, CLOUD_Y, n * 0.5)
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	cloud_mat = ShaderMaterial.new()
	cloud_mat.shader = shared.cloud_shader
	cloud_mat.set_shader_parameter("noise_tex", shared.noise_tex)
	cloud_mat.set_shader_parameter("fog_tex", fog_tex)
	cloud_mat.set_shader_parameter("map_size", float(n))
	cloud_mat.set_shader_parameter("border", float(B))
	cloud_mat.set_shader_parameter("underground", level > 0)
	mi.material_override = cloud_mat
	add_child(mi)
