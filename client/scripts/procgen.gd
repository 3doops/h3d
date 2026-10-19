extends RefCounted
## Procedural fallback models for every object category (used when manifest.json has no GLB).
## All meshes: 1 unit = 1 cell, Y up, origin at the bottom centre, front (entrance) towards +Z.

const Defs = preload("res://scripts/defs.gd")

enum { MATTE, FOLIAGE, SHINY, METAL, GLOW, FLAG }

static var _mats := {}
static var _cache := {}


static func materials() -> Dictionary:
	if not _mats.is_empty():
		return _mats
	var matte := ShaderMaterial.new()
	matte.shader = load("res://shaders/props.gdshader")
	var fol := ShaderMaterial.new()
	fol.shader = load("res://shaders/foliage.gdshader")
	var shiny := StandardMaterial3D.new()
	shiny.vertex_color_use_as_albedo = true
	shiny.roughness = 0.18
	shiny.emission_enabled = true
	shiny.emission = Color(0.06, 0.02, 0.02)
	var metal := StandardMaterial3D.new()
	metal.vertex_color_use_as_albedo = true
	metal.metallic = 0.85
	metal.roughness = 0.32
	var glow := ShaderMaterial.new()
	glow.shader = load("res://shaders/glow.gdshader")
	var flag := StandardMaterial3D.new()
	flag.albedo_color = Defs.NEUTRAL
	flag.roughness = 0.7
	flag.cull_mode = BaseMaterial3D.CULL_DISABLED
	_mats = {MATTE: matte, FOLIAGE: fol, SHINY: shiny, METAL: metal, GLOW: glow, FLAG: flag}
	return _mats


## Detail textures for the props shader (call once the shared noise textures exist).
static func set_detail(noise: Texture2D, crack: Texture2D) -> void:
	var m: ShaderMaterial = materials()[MATTE]
	m.set_shader_parameter("noise_tex", noise)
	m.set_shader_parameter("crack_tex", crack)


static func _b(k) -> Array:
	return k.build_with(materials())


static func flag_material(c: Color) -> Material:
	var key := "flag_%s" % c.to_html()
	if _cache.has(key):
		return _cache[key]
	var m := StandardMaterial3D.new()
	m.albedo_color = c
	m.roughness = 0.65
	m.cull_mode = BaseMaterial3D.CULL_DISABLED
	_cache[key] = m
	return m


# ============================================================================================= Kit

class Kit:
	var data := {}
	var xf := Transform3D.IDENTITY
	var rng := RandomNumberGenerator.new()
	var snow := 0.0      # 0..1: upward faces turn white
	var ao := true

	func _init(seed_value: int = 1) -> void:
		rng.seed = seed_value

	func _a(slot: int) -> Array:
		if not data.has(slot):
			data[slot] = [PackedVector3Array(), PackedVector3Array(), PackedColorArray()]
		return data[slot]

	func _snowed(c: Color, nrm: Vector3) -> Color:
		if snow <= 0.0:
			return c
		var k := clampf((nrm.y - 0.35) * 2.2, 0.0, 1.0) * snow
		return c.lerp(Color(0.9, 0.93, 0.97), k)

	## triangle with explicit (local) normals; winding is fixed automatically from the normals
	func tri(slot: int, a: Vector3, b: Vector3, c: Vector3, na: Vector3, nb: Vector3, nc: Vector3, ca: Color, cb: Color, cc: Color) -> void:
		var A := xf * a
		var Bv := xf * b
		var C := xf * c
		var bas := xf.basis
		var NA := (bas * na).normalized()
		var NB := (bas * nb).normalized()
		var NC := (bas * nc).normalized()
		var fn := (C - A).cross(Bv - A)
		if fn.dot(NA + NB + NC) < 0.0:
			var t := Bv; Bv = C; C = t
			var tn := NB; NB = NC; NC = tn
			var tc := cb; cb = cc; cc = tc
		var arr := _a(slot)
		arr[0].append(A); arr[0].append(Bv); arr[0].append(C)
		arr[1].append(NA); arr[1].append(NB); arr[1].append(NC)
		arr[2].append(_snowed(ca, NA)); arr[2].append(_snowed(cb, NB)); arr[2].append(_snowed(cc, NC))

	func ftri(slot: int, a: Vector3, b: Vector3, c: Vector3, col: Color, outward_from: Vector3) -> void:
		var n := (b - a).cross(c - a).normalized()
		if n.dot((a + b + c) / 3.0 - outward_from) < 0.0:
			n = -n
		tri(slot, a, b, c, n, n, n, col, col, col)

	func fquad(slot: int, a: Vector3, b: Vector3, c: Vector3, d: Vector3, col: Color, outward_from: Vector3) -> void:
		ftri(slot, a, b, c, col, outward_from)
		ftri(slot, a, c, d, col, outward_from)

	func box(slot: int, center: Vector3, size: Vector3, col: Color, rot_y := 0.0, top_col = null) -> void:
		var h := size * 0.5
		var bas := Basis(Vector3.UP, rot_y)
		var p := []
		for i in 8:
			var v := Vector3(h.x if i & 1 else -h.x, h.y if i & 2 else -h.y, h.z if i & 4 else -h.z)
			p.append(center + bas * v)
		var faces := [[0, 1, 3, 2], [4, 5, 7, 6], [0, 1, 5, 4], [2, 3, 7, 6], [0, 2, 6, 4], [1, 3, 7, 5]]
		for fi in faces.size():
			var f: Array = faces[fi]
			var c: Color = col
			if fi == 3 and top_col != null:
				c = top_col
			elif ao and fi != 3:
				c = col.darkened(0.08 if fi != 2 else 0.25)
			fquad(slot, p[f[0]], p[f[1]], p[f[2]], p[f[3]], c, center)

	## cylinder / cone frustum along +Y
	func cyl(slot: int, base: Vector3, r0: float, r1: float, h: float, segs: int, c0: Color, c1: Color, cap := true, tilt := Vector3.ZERO) -> void:
		var top := base + Vector3(0, h, 0) + tilt
		var slope := (r0 - r1) / maxf(h, 0.001)
		for i in segs:
			var a0 := TAU * i / segs
			var a1 := TAU * (i + 1) / segs
			var d0 := Vector3(cos(a0), 0, sin(a0))
			var d1 := Vector3(cos(a1), 0, sin(a1))
			var n0 := (d0 + Vector3(0, slope, 0)).normalized()
			var n1 := (d1 + Vector3(0, slope, 0)).normalized()
			var b0 := base + d0 * r0
			var b1 := base + d1 * r0
			var t0 := top + d0 * r1
			var t1 := top + d1 * r1
			tri(slot, b0, b1, t1, n0, n1, n1, c0, c0, c1)
			if r1 > 0.0005:
				tri(slot, b0, t1, t0, n0, n1, n0, c0, c1, c1)
				if cap:
					tri(slot, t0, t1, top, Vector3.UP, Vector3.UP, Vector3.UP, c1, c1, c1)

	func sphere(slot: int, center: Vector3, radii: Vector3, col: Color, rings := 5, segs := 8, noise_amp := 0.0, top_col = null, bottom_dark := 0.25) -> void:
		var noise := FastNoiseLite.new()
		noise.seed = rng.randi()
		noise.frequency = 1.6
		var grid := []
		for r in rings + 1:
			var phi := PI * r / rings
			var row := []
			for s in segs:
				var th := TAU * s / segs
				var d := Vector3(sin(phi) * cos(th), cos(phi), sin(phi) * sin(th))
				var k := 1.0 + noise_amp * noise.get_noise_3dv(d * 1.3)
				row.append([center + d * radii * k, d])
			grid.append(row)
		for r in rings:
			for s in segs:
				var s1 := (s + 1) % segs
				var q := [grid[r][s], grid[r][s1], grid[r + 1][s1], grid[r + 1][s]]
				var cs := []
				for v in q:
					var dd: Vector3 = v[1]
					var c: Color = col
					if top_col != null:
						c = col.lerp(top_col, clampf(dd.y * 1.4, 0.0, 1.0))
					if ao:
						c = c.darkened(clampf(-dd.y, 0.0, 1.0) * bottom_dark)
					cs.append(c)
				if r > 0:
					tri(slot, q[0][0], q[1][0], q[2][0], q[0][1], q[1][1], q[2][1], cs[0], cs[1], cs[2])
				if r < rings - 1:
					tri(slot, q[0][0], q[2][0], q[3][0], q[0][1], q[2][1], q[3][1], cs[0], cs[2], cs[3])

	## crystal: hexagonal prism with a pointed tip, tilted
	func crystal(slot: int, base: Vector3, r: float, h: float, col: Color, tilt: Vector3) -> void:
		var axis := (Vector3.UP + tilt).normalized()
		var bas := Basis(Quaternion(Vector3.UP, axis))
		var save := xf
		xf = xf * Transform3D(bas, base)
		cyl(slot, Vector3.ZERO, r, r * 0.9, h * 0.7, 6, col.darkened(0.25), col, false)
		cyl(slot, Vector3(0, h * 0.7, 0), r * 0.9, 0.0, h * 0.3, 6, col, col.lightened(0.3), false)
		xf = save

	func flag(base: Vector3, h: float, w: float, pole_col := Color(0.3, 0.22, 0.14)) -> void:
		cyl(MATTE, base, 0.025, 0.02, h, 5, pole_col, pole_col)
		var t := base + Vector3(0, h - 0.02, 0)
		var a := t
		var b := t + Vector3(w, -0.03, 0.02)
		var c := t + Vector3(w * 0.95, -w * 0.62, 0.03)
		var d := t + Vector3(0, -w * 0.6, 0)
		var n := Vector3(0, 0, 1)
		var fc := Color(1, 1, 1)
		tri(FLAG, a, b, c, n, n, n, fc, fc, fc)
		tri(FLAG, a, c, d, n, n, n, fc, fc, fc)

	func build_with(mats: Dictionary) -> Array:
		var mesh := ArrayMesh.new()
		var flag_idx := -1
		for slot in [MATTE, FOLIAGE, SHINY, METAL, GLOW, FLAG]:
			if not data.has(slot):
				continue
			var arr: Array = data[slot]
			if arr[0].size() == 0:
				continue
			var arrays := []
			arrays.resize(Mesh.ARRAY_MAX)
			arrays[Mesh.ARRAY_VERTEX] = arr[0]
			arrays[Mesh.ARRAY_NORMAL] = arr[1]
			arrays[Mesh.ARRAY_COLOR] = arr[2]
			mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
			var si := mesh.get_surface_count() - 1
			mesh.surface_set_material(si, mats[slot])
			if slot == FLAG:
				flag_idx = si
		return [mesh, flag_idx]


# ============================================================================================= vegetation

static func tree_mesh(kind: String, variant: int, snowy: bool) -> ArrayMesh:
	var key := "tree_%s_%d_%s" % [kind, variant, snowy]
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(hash(key))
	var rng := k.rng
	var trunk := Color(0.28, 0.19, 0.12)
	match kind:
		"pine":
			k.cyl(MATTE, Vector3.ZERO, 0.055, 0.035, 0.3, 6, trunk.darkened(0.4), trunk)
			var tiers := 6 + variant % 2
			var hgt := 1.05 + 0.18 * (variant % 3)
			var green := Color(0.025, 0.07, 0.035).lerp(Color(0.05, 0.11, 0.05), rng.randf())
			var snow_c := Color(0.86, 0.9, 0.96)
			for i in tiers:
				var f := float(i) / tiers
				var r := lerpf(0.36, 0.1, f) * (0.9 + 0.2 * rng.randf())
				var y := 0.18 + f * hgt * 0.8
				var th := hgt * 0.3 * (1.0 - f * 0.3)
				var top := green.lightened(0.12)
				var rim := green.darkened(0.25)
				if snowy:
					top = green.lerp(snow_c, 0.5 + 0.2 * rng.randf())
					rim = green.lerp(snow_c, 0.08)
				_jag_cone(k, FOLIAGE, y, r, th, 11, rim, top, rng)
			if snowy:
				k.cyl(FOLIAGE, Vector3(0, 0.18 + hgt * 0.8 + 0.05, 0), 0.05, 0.0, 0.12, 5, snow_c, snow_c, false)
		"oak", "autumn":
			k.cyl(MATTE, Vector3.ZERO, 0.08, 0.05, 0.5, 6, trunk.darkened(0.3), trunk)
			var base_c: Color = Color(0.2, 0.36, 0.12) if kind == "oak" else [Color(0.75, 0.38, 0.08), Color(0.62, 0.22, 0.06), Color(0.8, 0.6, 0.12)][variant % 3]
			k.snow = 0.8 if snowy else 0.0
			for i in 4:
				var off := Vector3(rng.randf_range(-0.18, 0.18), 0.65 + rng.randf_range(0, 0.3), rng.randf_range(-0.18, 0.18))
				var r := rng.randf_range(0.26, 0.36)
				k.sphere(FOLIAGE, off, Vector3(r, r * 0.85, r), base_c.lerp(Color(0.1, 0.1, 0.05), rng.randf() * 0.3), 4, 7, 0.25, base_c.lightened(0.25))
			k.snow = 0.0
		"willow":
			k.cyl(MATTE, Vector3.ZERO, 0.08, 0.06, 0.55, 6, trunk.darkened(0.4), trunk)
			var wc := Color(0.24, 0.32, 0.14)
			k.sphere(FOLIAGE, Vector3(0, 0.75, 0), Vector3(0.45, 0.35, 0.45), wc, 4, 8, 0.3, wc.lightened(0.2))
			for i in 7:
				var a := TAU * i / 7.0
				var p := Vector3(cos(a) * 0.36, 0.7, sin(a) * 0.36)
				k.cyl(FOLIAGE, p - Vector3(0, 0.45, 0), 0.03, 0.09, 0.45, 4, wc.darkened(0.3), wc)
		"dead":
			var bark := Color(0.22, 0.18, 0.15)
			k.snow = 0.3 if snowy else 0.0
			_branch(k, Vector3.ZERO, Vector3.UP, 0.85 + 0.15 * variant, 0.06, 3, bark)
			k.snow = 0.0
		"stump":
			k.cyl(MATTE, Vector3.ZERO, 0.14, 0.12, 0.18, 8, trunk.darkened(0.3), Color(0.55, 0.42, 0.28))
		"reeds":
			for i in 14:
				var p := Vector3(rng.randf_range(-0.3, 0.3), 0, rng.randf_range(-0.3, 0.3))
				var hh := rng.randf_range(0.18, 0.4)
				var rc := Color(0.35, 0.38, 0.16).lerp(Color(0.5, 0.45, 0.22), rng.randf())
				k.cyl(FOLIAGE, p, 0.012, 0.004, hh, 3, rc.darkened(0.3), rc, false, Vector3(rng.randf_range(-0.05, 0.05), 0, rng.randf_range(-0.05, 0.05)))
				if i % 4 == 0:
					k.cyl(MATTE, p + Vector3(0, hh * 0.75, 0), 0.02, 0.02, 0.07, 4, Color(0.3, 0.2, 0.1), Color(0.3, 0.2, 0.1))
		"bush":
			var bc := Color(0.22, 0.33, 0.13)
			k.snow = 0.8 if snowy else 0.0
			for i in 3:
				var r := rng.randf_range(0.15, 0.22)
				k.sphere(FOLIAGE, Vector3(rng.randf_range(-0.15, 0.15), r * 0.7, rng.randf_range(-0.15, 0.15)), Vector3(r, r * 0.8, r), bc, 3, 6, 0.3, bc.lightened(0.2))
			k.snow = 0.0
		"mushroom":
			for i in 3:
				var p := Vector3(rng.randf_range(-0.25, 0.25), 0, rng.randf_range(-0.25, 0.25))
				var h := rng.randf_range(0.2, 0.45)
				k.cyl(MATTE, p, 0.05, 0.04, h, 6, Color(0.75, 0.72, 0.62), Color(0.85, 0.82, 0.7))
				var cc: Color = [Color(0.7, 0.2, 0.15), Color(0.55, 0.4, 0.6), Color(0.6, 0.55, 0.3)][i % 3]
				k.sphere(MATTE, p + Vector3(0, h, 0), Vector3(0.17, 0.08, 0.17), cc, 3, 8)
		"flowers":
			for i in 10:
				var p := Vector3(rng.randf_range(-0.4, 0.4), 0.05, rng.randf_range(-0.4, 0.4))
				var fc: Color = [Color(0.9, 0.85, 0.3), Color(0.85, 0.3, 0.4), Color(0.6, 0.5, 0.9), Color(0.95, 0.95, 0.9)][i % 4]
				k.sphere(FOLIAGE, p, Vector3(0.05, 0.04, 0.05), fc, 2, 4)
				k.cyl(FOLIAGE, p - Vector3(0, 0.05, 0), 0.012, 0.01, 0.06, 3, Color(0.2, 0.35, 0.1), Color(0.2, 0.35, 0.1))
	var res: ArrayMesh = _b(k)[0]
	_cache[key] = res
	return res


static func _branch(k: Kit, p: Vector3, dir: Vector3, length: float, r: float, depth: int, col: Color) -> void:
	var end := p + dir * length
	var save := k.xf
	var bas := Basis(Quaternion(Vector3.UP, dir.normalized()))
	k.xf = save * Transform3D(bas, p)
	k.cyl(MATTE, Vector3.ZERO, r, r * 0.65, length, 5, col.darkened(0.2), col)
	k.xf = save
	if depth <= 0:
		return
	var n := 2 + k.rng.randi() % 2
	for i in n:
		var a := k.rng.randf() * TAU
		var nd := (dir + Vector3(cos(a), 0.2, sin(a)) * k.rng.randf_range(0.5, 0.9)).normalized()
		_branch(k, p + dir * length * k.rng.randf_range(0.55, 1.0), nd, length * 0.6, r * 0.6, depth - 1, col)


## Cone with a ragged, drooping rim (conifer tier). Colours: rim -> apex.
static func _jag_cone(k: Kit, slot: int, y: float, r: float, h: float, segs: int, rim_c: Color, top_c: Color, rng: RandomNumberGenerator) -> void:
	var apex := Vector3(0, y + h, 0)
	var rim := []
	for i in segs:
		var a := TAU * i / segs + rng.randf_range(-0.12, 0.12)
		var rr := r * rng.randf_range(0.78, 1.12)
		rim.append(Vector3(cos(a) * rr, y - rng.randf_range(0.0, 0.06), sin(a) * rr))
	var mid_c := rim_c.lerp(top_c, 0.3)
	for i in segs:
		var a: Vector3 = rim[i]
		var b: Vector3 = rim[(i + 1) % segs]
		var m := (a + b) * 0.5 + Vector3(0, h * 0.25, 0) * 0.0
		var na := Vector3(a.x, r * 0.9, a.z).normalized()
		var nb := Vector3(b.x, r * 0.9, b.z).normalized()
		# split each facet: lower band (rim colour) and upper part (towards apex)
		var a2 := a.lerp(apex, 0.35)
		var b2 := b.lerp(apex, 0.35)
		k.tri(slot, a, b, b2, na, nb, nb, rim_c, rim_c, mid_c)
		k.tri(slot, a, b2, a2, na, nb, na, rim_c, mid_c, mid_c)
		k.tri(slot, a2, b2, apex, na, nb, Vector3.UP, mid_c, mid_c, top_c)
		# underside so the tier does not look hollow from low angles
		k.tri(slot, a, Vector3(0, y + h * 0.1, 0), b, Vector3.DOWN, Vector3.DOWN, Vector3.DOWN, rim_c.darkened(0.5), rim_c.darkened(0.6), rim_c.darkened(0.5))


# ============================================================================================= relief

## Mountain range following the object's block cells (cells relative to the origin = bottom centre).
static func mountain_mesh(cells: Array, seed_value: int, snowy: bool, green: bool, scale_h := 1.0) -> ArrayMesh:
	var k := Kit.new(seed_value)
	var rng := k.rng
	var minx := 1e9; var maxx := -1e9; var minz := 1e9; var maxz := -1e9
	var peaks := []
	for c in cells:
		var cc: Vector2 = c
		minx = minf(minx, cc.x - 0.5); maxx = maxf(maxx, cc.x + 0.5)
		minz = minf(minz, cc.y - 0.5); maxz = maxf(maxz, cc.y + 0.5)
		peaks.append([cc + Vector2(rng.randf_range(-0.25, 0.25), rng.randf_range(-0.25, 0.25)), rng.randf_range(1.0, 2.3) * scale_h])
	var res := 7
	var w := int(ceil((maxx - minx + 0.6) * res))
	var d := int(ceil((maxz - minz + 0.6) * res))
	var x0 := minx - 0.3
	var z0 := minz - 0.3
	var noise := FastNoiseLite.new()
	noise.seed = seed_value
	noise.frequency = 1.4
	noise.fractal_type = FastNoiseLite.FRACTAL_RIDGED
	noise.fractal_octaves = 3
	var hs := PackedFloat32Array()
	hs.resize((w + 1) * (d + 1))
	for iz in d + 1:
		for ix in w + 1:
			var p := Vector2(x0 + ix / float(res), z0 + iz / float(res))
			var h := -0.25
			for pk in peaks:
				var dist: float = p.distance_to(pk[0])
				var ph: float = pk[1]
				h = maxf(h, ph * pow(1.0 - smoothstep(0.0, 1.0, dist), 1.3) - 0.12)
			if h > 0.0:
				h += noise.get_noise_2d(p.x, p.y) * 0.38 * minf(h + 0.2, 1.0)
			var edge := minf(minf(ix, w - ix), minf(iz, d - iz))
			if edge == 0:
				h = -0.3
			hs[iz * (w + 1) + ix] = h
	var rock_a := Color(0.24, 0.23, 0.22)
	var rock_b := Color(0.46, 0.43, 0.39)
	var grass := Color(0.24, 0.33, 0.14)
	var snow_c := Color(0.9, 0.93, 0.97)
	var getp := func(ix: int, iz: int) -> Vector3:
		return Vector3(x0 + ix / float(res), hs[iz * (w + 1) + ix], z0 + iz / float(res))
	var getn := func(ix: int, iz: int) -> Vector3:
		var hl := hs[iz * (w + 1) + maxi(ix - 1, 0)]
		var hr := hs[iz * (w + 1) + mini(ix + 1, w)]
		var hu := hs[maxi(iz - 1, 0) * (w + 1) + ix]
		var hd := hs[mini(iz + 1, d) * (w + 1) + ix]
		return Vector3(hl - hr, 2.0 / res, hu - hd).normalized()
	var colf := func(p: Vector3, n: Vector3) -> Color:
		var c := rock_a.lerp(rock_b, clampf(noise.get_noise_2d(p.x * 3.0, p.y * 6.0) + 0.5, 0.0, 1.0))
		if green and p.y < 0.55:
			c = c.lerp(grass, clampf((0.55 - p.y) * 3.0, 0.0, 1.0) * clampf(n.y * 1.5 - 0.3, 0.0, 1.0))
		if snowy:
			var sn := noise.get_noise_2d(p.x * 5.0, p.z * 5.0) * 0.25
			var s := clampf((n.y - 0.62 + sn) * 4.0, 0.0, 1.0) * clampf((p.y - 0.15) * 3.0, 0.0, 1.0)
			s = maxf(s, clampf((p.y - 1.6) * 2.0, 0.0, 1.0) * clampf(n.y * 2.0 - 0.6, 0.0, 1.0))
			c = c.lerp(snow_c, s)
		return c.darkened(clampf(0.3 - p.y, 0.0, 0.3))
	for iz in d:
		for ix in w:
			var pa: Vector3 = getp.call(ix, iz)
			var pb: Vector3 = getp.call(ix + 1, iz)
			var pc: Vector3 = getp.call(ix, iz + 1)
			var pd: Vector3 = getp.call(ix + 1, iz + 1)
			if pa.y < -0.2 and pb.y < -0.2 and pc.y < -0.2 and pd.y < -0.2:
				continue
			var na: Vector3 = getn.call(ix, iz)
			var nb: Vector3 = getn.call(ix + 1, iz)
			var nc: Vector3 = getn.call(ix, iz + 1)
			var nd: Vector3 = getn.call(ix + 1, iz + 1)
			k.tri(MATTE, pa, pb, pc, na, nb, nc, colf.call(pa, na), colf.call(pb, nb), colf.call(pc, nc))
			k.tri(MATTE, pb, pd, pc, nb, nd, nc, colf.call(pb, nb), colf.call(pd, nd), colf.call(pc, nc))
	return _b(k)[0]


static func rock_mesh(size: Vector2, seed_value: int, snowy: bool, dark := false) -> ArrayMesh:
	var key := "rock_%d_%d_%d_%s_%s" % [int(size.x * 10), int(size.y * 10), seed_value % 5, snowy, dark]
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(seed_value)
	var c := Color(0.3, 0.29, 0.27) if not dark else Color(0.2, 0.17, 0.16)
	k.snow = 0.3 if snowy else 0.0
	var n := 2 + int(size.x * size.y * 0.8)
	for i in mini(n, 6):
		var r := k.rng.randf_range(0.18, 0.34) * minf(size.x, 1.6)
		var p := Vector3(k.rng.randf_range(-0.3, 0.3) * size.x, r * 0.25, k.rng.randf_range(-0.3, 0.3) * size.y)
		k.sphere(MATTE, p, Vector3(r * 1.15, r * k.rng.randf_range(0.6, 1.1), r * 0.95), c.lerp(Color(0.48, 0.45, 0.4), k.rng.randf() * 0.5), 3, 6, 0.5)
	var res: ArrayMesh = _b(k)[0]
	_cache[key] = res
	return res


static func stalagmite_mesh(seed_value: int) -> ArrayMesh:
	var k := Kit.new(seed_value)
	for i in 4:
		var p := Vector3(k.rng.randf_range(-0.35, 0.35), 0, k.rng.randf_range(-0.35, 0.35))
		var h := k.rng.randf_range(0.4, 1.1)
		k.cyl(MATTE, p, k.rng.randf_range(0.1, 0.18), 0.0, h, 7, Color(0.3, 0.25, 0.22), Color(0.5, 0.44, 0.38))
	return _b(k)[0]


static func flat_patch_mesh(size: Vector2, col: Color, glow := false, y := 0.03) -> ArrayMesh:
	var k := Kit.new(1)
	k.ao = false
	var segs := 20
	var slot := GLOW if glow else SHINY
	for i in segs:
		var a0 := TAU * i / segs
		var a1 := TAU * (i + 1) / segs
		var wob0 := 0.9 + 0.1 * sin(a0 * 3.0)
		var wob1 := 0.9 + 0.1 * sin(a1 * 3.0)
		var p0 := Vector3(cos(a0) * size.x * 0.5 * wob0, y, sin(a0) * size.y * 0.5 * wob0)
		var p1 := Vector3(cos(a1) * size.x * 0.5 * wob1, y, sin(a1) * size.y * 0.5 * wob1)
		k.tri(slot, Vector3(0, y, 0), p0, p1, Vector3.UP, Vector3.UP, Vector3.UP, col, col.darkened(0.2), col.darkened(0.2))
	return _b(k)[0]


# ============================================================================================= pickups

static func resource_mesh(sub: int) -> ArrayMesh:
	var key := "res_%d" % sub
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(sub + 11)
	var rng := k.rng
	match sub:
		0:  # wood: stacked logs
			var lc := Color(0.45, 0.3, 0.17)
			for i in 5:
				var row := 0 if i < 3 else 1
				var x := (i - 1) * 0.13 if row == 0 else (i - 3.5) * 0.13
				var save := k.xf
				k.xf = Transform3D(Basis(Vector3.RIGHT, PI * 0.5), Vector3(x, 0.07 + row * 0.12, 0.18))
				k.cyl(MATTE, Vector3.ZERO, 0.065, 0.065, 0.42, 7, lc, Color(0.72, 0.56, 0.36))
				k.xf = save
		1:  # mercury: flask with silver liquid
			k.cyl(SHINY, Vector3(0, 0, 0), 0.13, 0.15, 0.18, 10, Color(0.5, 0.55, 0.6), Color(0.6, 0.65, 0.7))
			k.cyl(SHINY, Vector3(0, 0.18, 0), 0.15, 0.05, 0.12, 10, Color(0.6, 0.65, 0.7), Color(0.6, 0.65, 0.7))
			k.cyl(MATTE, Vector3(0, 0.3, 0), 0.05, 0.05, 0.06, 8, Color(0.4, 0.3, 0.2), Color(0.4, 0.3, 0.2))
			for i in 3:
				k.sphere(METAL, Vector3(rng.randf_range(-0.25, 0.25), 0.03, rng.randf_range(0.1, 0.25)), Vector3(0.05, 0.03, 0.05), Color(0.85, 0.87, 0.9), 3, 6)
		2:  # ore: dark chunks
			for i in 6:
				var r := rng.randf_range(0.08, 0.14)
				k.sphere(MATTE, Vector3(rng.randf_range(-0.18, 0.18), r * 0.6 + (0.08 if i > 3 else 0.0), rng.randf_range(-0.15, 0.15)), Vector3(r, r * 0.8, r), Color(0.3, 0.3, 0.32).lerp(Color(0.45, 0.35, 0.3), rng.randf()), 3, 5, 0.4)
		3:  # sulfur: yellow lumps
			for i in 6:
				var r := rng.randf_range(0.07, 0.13)
				k.sphere(SHINY, Vector3(rng.randf_range(-0.18, 0.18), r * 0.6, rng.randf_range(-0.15, 0.15)), Vector3(r, r * 0.9, r), Color(0.85, 0.75, 0.15).lerp(Color(0.95, 0.9, 0.4), rng.randf()), 3, 5, 0.4)
		4:  # crystal: red cluster
			for i in 7:
				var tilt := Vector3(rng.randf_range(-0.5, 0.5), 0, rng.randf_range(-0.5, 0.5)) * (0.2 if i == 0 else 1.0)
				k.crystal(SHINY, Vector3(rng.randf_range(-0.1, 0.1), 0, rng.randf_range(-0.1, 0.1)), rng.randf_range(0.04, 0.07), rng.randf_range(0.22, 0.48) * (1.4 if i == 0 else 1.0), Color(0.8, 0.08, 0.1), tilt)
		5:  # gems: colourful pile
			var gcols := [Color(0.1, 0.7, 0.3), Color(0.15, 0.3, 0.9), Color(0.85, 0.1, 0.2), Color(0.9, 0.8, 0.2), Color(0.6, 0.2, 0.8)]
			for i in 14:
				var r := rng.randf_range(0.045, 0.07)
				var lay := 0 if i < 8 else 1
				k.sphere(SHINY, Vector3(rng.randf_range(-0.16, 0.16) * (1.0 - lay * 0.5), r + lay * 0.08, rng.randf_range(-0.14, 0.14) * (1.0 - lay * 0.5)), Vector3(r, r, r), gcols[i % gcols.size()], 3, 5)
		6:  # gold: coins + bars
			k.cyl(METAL, Vector3(0, 0, 0), 0.26, 0.04, 0.2, 12, Color(0.85, 0.62, 0.15), Color(1.0, 0.82, 0.3))
			for i in 3:
				k.box(METAL, Vector3(-0.12 + i * 0.12, 0.04, 0.22), Vector3(0.1, 0.07, 0.2), Color(0.95, 0.75, 0.25), 0.2)
		_:
			k.sphere(MATTE, Vector3(0, 0.15, 0), Vector3(0.2, 0.15, 0.2), Color(0.6, 0.6, 0.6))
	var res: ArrayMesh = _b(k)[0]
	_cache[key] = res
	return res


static func chest_mesh() -> ArrayMesh:
	if _cache.has("chest"):
		return _cache.chest
	var k := Kit.new(5)
	var wood := Color(0.42, 0.24, 0.12)
	var band := Color(0.85, 0.65, 0.2)
	k.box(MATTE, Vector3(0, 0.12, 0), Vector3(0.46, 0.24, 0.3), wood)
	var save := k.xf
	k.xf = Transform3D(Basis(Vector3.RIGHT, PI * 0.5).rotated(Vector3.UP, PI * 0.5), Vector3(-0.23, 0.24, 0))
	k.cyl(MATTE, Vector3.ZERO, 0.15, 0.15, 0.46, 10, wood.lightened(0.1), wood.lightened(0.1), true)
	k.xf = save
	for x in [-0.15, 0.15]:
		k.box(METAL, Vector3(x, 0.2, 0), Vector3(0.04, 0.42, 0.32), band)
	k.box(METAL, Vector3(0, 0.2, 0.16), Vector3(0.07, 0.09, 0.02), band)
	var res: ArrayMesh = _b(k)[0]
	_cache.chest = res
	return res


static func artifact_mesh(sub: int) -> ArrayMesh:
	var key := "art_%d" % (sub % 4)
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(sub)
	k.cyl(MATTE, Vector3.ZERO, 0.2, 0.17, 0.12, 8, Color(0.5, 0.48, 0.44), Color(0.62, 0.6, 0.55))
	var gc: Color = [Color(0.3, 0.6, 1.0), Color(1.0, 0.6, 0.2), Color(0.6, 1.0, 0.4), Color(0.9, 0.3, 1.0)][sub % 4]
	k.sphere(GLOW, Vector3(0, 0.27, 0), Vector3(0.1, 0.1, 0.1), gc, 4, 8)
	k.cyl(METAL, Vector3(0, 0.12, 0), 0.06, 0.1, 0.07, 8, Color(0.8, 0.6, 0.2), Color(0.9, 0.7, 0.3))
	var res: ArrayMesh = _b(k)[0]
	_cache[key] = res
	return res


static func campfire_mesh() -> ArrayMesh:
	if _cache.has("campfire"):
		return _cache.campfire
	var k := Kit.new(3)
	for i in 8:
		var a := TAU * i / 8.0
		k.sphere(MATTE, Vector3(cos(a) * 0.22, 0.04, sin(a) * 0.22), Vector3(0.07, 0.05, 0.07), Color(0.4, 0.38, 0.36), 3, 5, 0.3)
	for i in 3:
		var save := k.xf
		k.xf = Transform3D(Basis(Vector3.UP, i * 2.1) * Basis(Vector3.RIGHT, 1.2), Vector3(0, 0.05, 0))
		k.cyl(MATTE, Vector3(0, -0.18, 0), 0.035, 0.035, 0.36, 5, Color(0.3, 0.2, 0.1), Color(0.15, 0.1, 0.08))
		k.xf = save
	k.cyl(GLOW, Vector3(0, 0.05, 0), 0.12, 0.0, 0.32, 7, Color(1.0, 0.45, 0.08), Color(1.0, 0.85, 0.3), false)
	k.cyl(GLOW, Vector3(0.05, 0.05, 0.03), 0.06, 0.0, 0.22, 5, Color(1.0, 0.6, 0.1), Color(1.0, 0.9, 0.5), false)
	# a few supplies
	k.box(MATTE, Vector3(0.32, 0.08, -0.1), Vector3(0.14, 0.16, 0.14), Color(0.45, 0.3, 0.15))
	k.sphere(METAL, Vector3(-0.3, 0.06, 0.12), Vector3(0.1, 0.06, 0.1), Color(0.9, 0.7, 0.25), 3, 6)
	var res: ArrayMesh = _b(k)[0]
	_cache.campfire = res
	return res


# ============================================================================================= buildings

## Fortified town on a rocky plinth. size = footprint in cells (x, z), gate on the +Z side at gate_x.
static func town_mesh(size: Vector2, faction: int, gate_x: float, seed_value: int) -> Array:
	var k := Kit.new(seed_value)
	var rng := k.rng
	var pal: Array = Defs.FACTION_PALETTE[clampi(faction, 0, Defs.FACTION_PALETTE.size() - 1)]
	var stone: Color = pal[0]
	var roof: Color = pal[1]
	var accent: Color = pal[2]
	var glowc: Color = pal[3]
	var hw := size.x * 0.5 - 0.1
	var hd := size.y * 0.5 - 0.05
	var spiky := faction in [3, 4, 5]
	# rocky plinth with cliffs
	k.snow = 0.35
	for i in 9:
		var p := Vector3(rng.randf_range(-hw, hw), -0.15, rng.randf_range(-hd, hd))
		k.sphere(MATTE, p, Vector3(0.7, 0.45, 0.6), Color(0.34, 0.32, 0.29).lerp(Color(0.45, 0.42, 0.38), rng.randf()), 4, 8, 0.35)
	k.snow = 0.0
	var base_y := 0.32
	k.box(MATTE, Vector3(0, base_y * 0.5, 0), Vector3(hw * 2.0, base_y, hd * 2.0), stone.darkened(0.35))
	# curtain walls
	var wall_h := 0.8
	var wt := 0.16
	var wy := base_y + wall_h * 0.5
	k.box(MATTE, Vector3(0, wy, -hd), Vector3(hw * 2, wall_h, wt), stone.darkened(rng.randf() * 0.08))
	k.box(MATTE, Vector3(-hw, wy, 0), Vector3(wt, wall_h, hd * 2), stone.darkened(0.05))
	k.box(MATTE, Vector3(hw, wy, 0), Vector3(wt, wall_h, hd * 2), stone.darkened(0.12))
	var gx := gate_x
	var lw := (gx - 0.42) - (-hw)
	var rw := hw - (gx + 0.42)
	if lw > 0.05:
		k.box(MATTE, Vector3(-hw + lw * 0.5, wy, hd), Vector3(lw, wall_h, wt), stone)
	if rw > 0.05:
		k.box(MATTE, Vector3(gx + 0.42 + rw * 0.5, wy, hd), Vector3(rw, wall_h, wt), stone)
	# crenellations on all walls
	var top := base_y + wall_h
	var nx := int(hw * 2 / 0.2)
	for x in nx:
		var px := -hw + 0.1 + x * 0.2
		if absf(px - gx) > 0.45:
			k.box(MATTE, Vector3(px, top + 0.06, hd), Vector3(0.1, 0.12, wt + 0.03), stone.lightened(0.05))
		k.box(MATTE, Vector3(px, top + 0.06, -hd), Vector3(0.1, 0.12, wt + 0.03), stone.lightened(0.05))
	for z in int(hd * 2 / 0.2):
		var pz := -hd + 0.1 + z * 0.2
		k.box(MATTE, Vector3(-hw, top + 0.06, pz), Vector3(wt + 0.03, 0.12, 0.1), stone.lightened(0.05))
		k.box(MATTE, Vector3(hw, top + 0.06, pz), Vector3(wt + 0.03, 0.12, 0.1), stone.lightened(0.05))
	# gatehouse with two towers, dark arch and a ramp
	for sx in [-0.45, 0.45]:
		k.cyl(MATTE, Vector3(gx + sx, base_y, hd), 0.2, 0.18, 1.35, 12, stone.darkened(0.15), stone)
		k.cyl(MATTE, Vector3(gx + sx, base_y + 1.35, hd), 0.24, 0.0, 0.55 if not spiky else 0.9, 12, roof.darkened(0.25), roof)
	k.box(MATTE, Vector3(gx, base_y + 0.85, hd), Vector3(0.75, 0.45, 0.26), stone.darkened(0.05))
	k.box(MATTE, Vector3(gx, base_y + 0.3, hd + 0.03), Vector3(0.4, 0.6, 0.22), Color(0.06, 0.045, 0.04))
	k.box(MATTE, Vector3(gx, 0.12, hd + 0.45), Vector3(0.42, 0.06, 0.7), Color(0.38, 0.28, 0.18))
	k.box(FLAG, Vector3(gx, base_y + 0.9, hd + 0.14), Vector3(0.22, 0.32, 0.02), Color.WHITE)
	# corner and mid-wall towers
	var th := 1.8 if not spiky else 2.1
	var towers := [Vector2(-hw, -hd), Vector2(hw, -hd), Vector2(-hw, hd), Vector2(hw, hd)]
	if hw > 1.6:
		towers.append(Vector2(0, -hd))
	var wcol := glowc if glowc.v > 0.1 else Color(1.0, 0.72, 0.35)
	for t in towers:
		var tv: Vector2 = t
		var h := th * rng.randf_range(0.85, 1.12)
		var r := 0.3 if not spiky else 0.26
		k.cyl(MATTE, Vector3(tv.x, base_y, tv.y), r, r * 0.88, h, 14, stone.darkened(0.18), stone)
		k.cyl(MATTE, Vector3(tv.x, base_y + h, tv.y), r * 1.05, r * 1.05, 0.1, 14, stone.lightened(0.05), stone.lightened(0.05))
		k.cyl(MATTE, Vector3(tv.x, base_y + h + 0.1, tv.y), r * 1.1, 0.0, 0.75 if not spiky else 1.15, 14, roof.darkened(0.3), roof.lightened(0.12))
		k.box(GLOW, Vector3(tv.x, base_y + h * 0.62, tv.y + r * 0.93), Vector3(0.06, 0.16, 0.02), wcol * 0.8)
		k.flag(Vector3(tv.x, base_y + h + (0.7 if not spiky else 1.1), tv.y), 0.4, 0.3)
	# keep
	var kx := clampf(rng.randf_range(-0.4, 0.4), -hw + 0.7, hw - 0.7)
	var kz := -hd * 0.3
	var kh := 2.3 if not spiky else 2.6
	k.box(MATTE, Vector3(kx, base_y + kh * 0.5, kz), Vector3(1.1, kh, 0.85), stone)
	for i in 6:
		k.box(MATTE, Vector3(kx - 0.5 + i * 0.2, base_y + kh + 0.06, kz + 0.43), Vector3(0.1, 0.12, 0.06), stone.lightened(0.05))
	if spiky:
		for sx in [-0.38, 0.38]:
			k.cyl(MATTE, Vector3(kx + sx, base_y + kh, kz), 0.2, 0.16, 0.8, 8, stone, stone.lightened(0.1))
			k.cyl(MATTE, Vector3(kx + sx, base_y + kh + 0.8, kz), 0.22, 0.0, 1.1, 8, roof, roof.lightened(0.15))
		k.cyl(MATTE, Vector3(kx, base_y + kh, kz - 0.1), 0.28, 0.22, 1.4, 8, stone.darkened(0.05), stone)
		k.cyl(MATTE, Vector3(kx, base_y + kh + 1.4, kz - 0.1), 0.31, 0.0, 1.4, 8, roof, roof.lightened(0.15))
	else:
		var y0 := base_y + kh
		var a := Vector3(kx - 0.6, y0, kz - 0.48)
		var b := Vector3(kx + 0.6, y0, kz - 0.48)
		var c := Vector3(kx + 0.6, y0, kz + 0.48)
		var d := Vector3(kx - 0.6, y0, kz + 0.48)
		var r0 := Vector3(kx - 0.6, y0 + 0.75, kz)
		var r1 := Vector3(kx + 0.6, y0 + 0.75, kz)
		var ctr := Vector3(kx, y0 + 0.2, kz)
		k.fquad(MATTE, a, b, r1, r0, roof.darkened(0.1), ctr)
		k.fquad(MATTE, d, c, r1, r0, roof.lightened(0.05), ctr)
		k.ftri(MATTE, a, d, r0, stone, ctr)
		k.ftri(MATTE, b, c, r1, stone, ctr)
		k.cyl(MATTE, Vector3(kx + 0.62, base_y, kz + 0.25), 0.24, 0.21, kh + 0.75, 14, stone.darkened(0.12), stone)
		k.cyl(MATTE, Vector3(kx + 0.62, base_y + kh + 0.75, kz + 0.25), 0.28, 0.0, 0.95, 14, roof.darkened(0.25), roof)
		k.flag(Vector3(kx + 0.62, base_y + kh + 1.65, kz + 0.25), 0.45, 0.36)
	for i in 3:
		k.box(GLOW, Vector3(kx - 0.32 + i * 0.32, base_y + kh * 0.62, kz + 0.43), Vector3(0.09, 0.24, 0.02), wcol * 0.75)
		k.box(GLOW, Vector3(kx - 0.32 + i * 0.32, base_y + kh * 0.3, kz + 0.43), Vector3(0.09, 0.2, 0.02), wcol * 0.6)
	k.box(FLAG, Vector3(kx - 0.36, base_y + kh * 0.82, kz + 0.44), Vector3(0.16, 0.5, 0.02), Color.WHITE)
	k.box(FLAG, Vector3(kx + 0.36, base_y + kh * 0.82, kz + 0.44), Vector3(0.16, 0.5, 0.02), Color.WHITE)
	# houses in the bailey
	for i in 5:
		var p := Vector3(rng.randf_range(-hw + 0.35, hw - 0.35), base_y, rng.randf_range(0.0, hd - 0.3))
		if absf(p.x - gx) < 0.5 or (absf(p.x - kx) < 0.7 and p.z < kz + 0.6):
			continue
		var hh := rng.randf_range(0.25, 0.4)
		k.box(MATTE, p + Vector3(0, hh * 0.5, 0), Vector3(0.32, hh, 0.26), accent.darkened(0.35 + rng.randf() * 0.15))
		k.cyl(MATTE, p + Vector3(0, hh, 0), 0.25, 0.0, 0.25, 4, roof.darkened(0.2), roof)
	return _b(k)


static func mine_mesh(size: Vector2, sub: int, gate_x: float, snowy: bool, seed_value: int) -> Array:
	var k := Kit.new(seed_value)
	var rng := k.rng
	var hw := size.x * 0.5
	var hd := size.y * 0.5
	k.snow = 0.8 if snowy else 0.0
	for i in 5:
		var p := Vector3(rng.randf_range(-hw * 0.7, hw * 0.7), 0.0, rng.randf_range(-hd * 0.8, -hd * 0.1))
		var r := rng.randf_range(0.45, 0.7)
		k.sphere(MATTE, p, Vector3(r, r * 1.05, r * 0.9), Color(0.36, 0.33, 0.3), 5, 8, 0.35)
	k.snow = 0.0
	var timber := Color(0.38, 0.25, 0.13)
	var gx := gate_x
	var fz := hd * 0.25
	k.box(MATTE, Vector3(gx, 0.3, fz - 0.15), Vector3(0.5, 0.6, 0.3), Color(0.04, 0.03, 0.03))
	for sx in [-0.28, 0.28]:
		k.box(MATTE, Vector3(gx + sx, 0.33, fz), Vector3(0.09, 0.66, 0.09), timber)
	k.box(MATTE, Vector3(gx, 0.68, fz), Vector3(0.7, 0.1, 0.12), timber)
	k.box(MATTE, Vector3(gx, 0.76, fz - 0.08), Vector3(0.78, 0.06, 0.32), timber.darkened(0.2), 0.0)
	# rails and cart
	for sx in [-0.09, 0.09]:
		k.box(METAL, Vector3(gx + sx, 0.02, fz + 0.4), Vector3(0.025, 0.03, 0.9), Color(0.4, 0.4, 0.42))
	k.box(MATTE, Vector3(gx + 0.0, 0.14, fz + 0.55), Vector3(0.26, 0.16, 0.3), timber.darkened(0.15))
	var save := k.xf
	k.xf = Transform3D(Basis.IDENTITY.scaled(Vector3(0.6, 0.6, 0.6)), Vector3(gx, 0.2, fz + 0.55))
	var rm := resource_mesh(clampi(sub, 0, 6))
	k.xf = save
	# resource sample next to the entrance (copy the resource mesh surfaces into the kit)
	_append_mesh(k, rm, Transform3D(Basis.IDENTITY.scaled(Vector3(0.9, 0.9, 0.9)), Vector3(gx + 0.62, 0.0, fz + 0.35)))
	k.flag(Vector3(gx - 0.45, 0.0, fz + 0.15), 0.85, 0.3)
	# lantern
	k.sphere(GLOW, Vector3(gx + 0.32, 0.55, fz + 0.08), Vector3(0.04, 0.05, 0.04), Color(1.0, 0.7, 0.3), 3, 5)
	return _b(k)


static func _append_mesh(k: Kit, m: ArrayMesh, t: Transform3D) -> void:
	var save := k.xf
	k.xf = save * t
	for s in m.get_surface_count():
		var arr := m.surface_get_arrays(s)
		var v: PackedVector3Array = arr[Mesh.ARRAY_VERTEX]
		var nn: PackedVector3Array = arr[Mesh.ARRAY_NORMAL]
		var c: PackedColorArray = arr[Mesh.ARRAY_COLOR]
		var mat := m.surface_get_material(s)
		var slot := MATTE
		for key in _mats:
			if _mats[key] == mat:
				slot = key
		for i in range(0, v.size(), 3):
			k.tri(slot, v[i], v[i + 1], v[i + 2], nn[i], nn[i + 1], nn[i + 2], c[i], c[i + 1], c[i + 2])
	k.xf = save


static func house_mesh(size: Vector2, faction: int, gate_x: float, seed_value: int, tall := false) -> Array:
	var k := Kit.new(seed_value)
	var rng := k.rng
	var pal: Array = Defs.FACTION_PALETTE[clampi(faction, 0, Defs.FACTION_PALETTE.size() - 1)] if faction >= 0 else [Color(0.62, 0.58, 0.52), Color(0.3, 0.2, 0.14), Color(0.6, 0.5, 0.35), Color(0, 0, 0)]
	var stone: Color = pal[0]
	var roof: Color = pal[1]
	var timber := Color(0.24, 0.16, 0.1)
	var plaster := Color(0.78, 0.72, 0.6)
	var w := clampf(size.x * 0.62, 0.6, 1.5)
	var d := clampf(size.y * 0.55, 0.5, 0.95)
	var h := 0.42 if not tall else 0.85
	var cz := -0.12
	# stone footing, plaster walls with a timber frame
	k.box(MATTE, Vector3(0, 0.06, cz), Vector3(w + 0.12, 0.12, d + 0.12), stone.darkened(0.25))
	k.box(MATTE, Vector3(0, 0.12 + h * 0.5, cz), Vector3(w, h, d), plaster.darkened(rng.randf() * 0.15))
	for i in 4:
		var x := -w * 0.5 + w * i / 3.0
		k.box(MATTE, Vector3(x, 0.12 + h * 0.5, cz + d * 0.5 + 0.01), Vector3(0.05, h, 0.02), timber)
	k.box(MATTE, Vector3(0, 0.12 + h, cz + d * 0.5 + 0.01), Vector3(w, 0.05, 0.02), timber)
	k.box(MATTE, Vector3(0, 0.12 + h * 0.5, cz + d * 0.5 + 0.01), Vector3(w, 0.04, 0.02), timber)
	# steep roof with overhang
	var y0 := 0.12 + h
	var ov := 0.1
	var a := Vector3(-w * 0.5 - ov, y0 - 0.04, cz - d * 0.5 - ov)
	var b := Vector3(w * 0.5 + ov, y0 - 0.04, cz - d * 0.5 - ov)
	var c := Vector3(w * 0.5 + ov, y0 - 0.04, cz + d * 0.5 + ov)
	var dd := Vector3(-w * 0.5 - ov, y0 - 0.04, cz + d * 0.5 + ov)
	var rh := d * 0.75
	var r0 := Vector3(-w * 0.5 - ov, y0 + rh, cz)
	var r1 := Vector3(w * 0.5 + ov, y0 + rh, cz)
	var ctr := Vector3(0, y0 + rh * 0.3, cz)
	k.fquad(MATTE, a, b, r1, r0, roof.darkened(0.2), ctr)
	k.fquad(MATTE, dd, c, r1, r0, roof, ctr)
	var g0 := Vector3(-w * 0.5, y0, cz - d * 0.5)
	var g1 := Vector3(-w * 0.5, y0, cz + d * 0.5)
	var g2 := Vector3(-w * 0.5, y0 + rh * 0.9, cz)
	k.ftri(MATTE, g0, g1, g2, plaster.darkened(0.25), ctr)
	k.ftri(MATTE, g0 + Vector3(w, 0, 0), g1 + Vector3(w, 0, 0), g2 + Vector3(w, 0, 0), plaster.darkened(0.1), ctr)
	# chimney
	k.box(MATTE, Vector3(w * 0.25, y0 + rh * 0.75, cz - d * 0.2), Vector3(0.1, 0.35, 0.1), stone.darkened(0.2))
	# door and a lit window
	var gx := clampf(gate_x, -w * 0.35, w * 0.35)
	k.box(MATTE, Vector3(gx, 0.12 + 0.16, cz + d * 0.5 + 0.02), Vector3(0.16, 0.32, 0.03), Color(0.22, 0.13, 0.07))
	k.box(GLOW, Vector3(gx + (0.26 if gx < w * 0.2 else -0.26), 0.12 + h * 0.62, cz + d * 0.5 + 0.02), Vector3(0.1, 0.1, 0.02), Color(1.0, 0.72, 0.35) * 0.7)
	if tall:
		k.cyl(MATTE, Vector3(w * 0.45, 0.1, cz - d * 0.3), 0.18, 0.16, h + 0.6, 10, stone.darkened(0.1), stone)
		k.cyl(MATTE, Vector3(w * 0.45, 0.7 + h, cz - d * 0.3), 0.22, 0.0, 0.5, 10, roof.darkened(0.2), roof)
	k.flag(Vector3(-w * 0.5 - 0.1, 0.0, cz + d * 0.5), 1.0 + (0.5 if tall else 0.0), 0.3)
	# crates, a barrel and a little fence
	k.box(MATTE, Vector3(w * 0.5 + 0.13, 0.07, cz + d * 0.35), Vector3(0.13, 0.14, 0.13), Color(0.45, 0.3, 0.15))
	k.cyl(MATTE, Vector3(w * 0.5 + 0.14, 0.0, cz + d * 0.05), 0.07, 0.07, 0.17, 8, Color(0.35, 0.22, 0.12), Color(0.4, 0.27, 0.15))
	for i in 5:
		k.box(MATTE, Vector3(-w * 0.5 + i * w / 4.0, 0.08, cz + d * 0.5 + 0.32), Vector3(0.03, 0.16, 0.03), timber.lightened(0.1))
	return _b(k)


static func windmill_mesh(seed_value: int) -> Array:
	var k := Kit.new(seed_value)
	var stone := Color(0.6, 0.57, 0.52)
	k.cyl(MATTE, Vector3(0, 0, -0.1), 0.32, 0.22, 1.05, 12, stone.darkened(0.15), stone)
	k.cyl(MATTE, Vector3(0, 1.05, -0.1), 0.28, 0.0, 0.42, 12, Color(0.35, 0.22, 0.12), Color(0.45, 0.3, 0.15))
	k.box(MATTE, Vector3(0, 0.18, 0.2), Vector3(0.18, 0.34, 0.04), Color(0.25, 0.15, 0.08))
	var blades := Kit.new(seed_value + 1)
	var cloth := Color(0.85, 0.8, 0.68)
	for i in 4:
		blades.xf = Transform3D(Basis(Vector3.BACK, i * PI * 0.5), Vector3.ZERO)
		blades.box(MATTE, Vector3(0, 0.36, 0), Vector3(0.04, 0.72, 0.03), Color(0.35, 0.24, 0.13))
		blades.box(MATTE, Vector3(0.08, 0.42, 0.01), Vector3(0.14, 0.55, 0.01), cloth)
	return [_b(k)[0], _b(blades)[0]]


static func watermill_mesh(seed_value: int) -> Array:
	var r := house_mesh(Vector2(2, 1), -1, 0.0, seed_value)
	var wheel := Kit.new(seed_value + 3)
	var wood := Color(0.4, 0.27, 0.15)
	for i in 8:
		var a := TAU * i / 8.0
		wheel.xf = Transform3D(Basis(Vector3.RIGHT, a), Vector3.ZERO)
		wheel.box(MATTE, Vector3(0, 0, 0.2), Vector3(0.08, 0.03, 0.4), wood)
		wheel.box(MATTE, Vector3(0, 0, 0.4), Vector3(0.12, 0.12, 0.03), wood.darkened(0.2))
	return [r[0], _b(wheel)[0], r[1]]


static func obelisk_mesh(seed_value: int, col := Color(0.62, 0.3, 0.85)) -> ArrayMesh:
	var k := Kit.new(seed_value)
	k.cyl(MATTE, Vector3.ZERO, 0.3, 0.26, 0.12, 4, Color(0.45, 0.43, 0.4), Color(0.55, 0.52, 0.48))
	k.cyl(SHINY, Vector3(0, 0.12, 0), 0.14, 0.1, 0.95, 4, col.darkened(0.3), col)
	k.cyl(SHINY, Vector3(0, 1.07, 0), 0.1, 0.0, 0.2, 4, col, col.lightened(0.3))
	k.sphere(GLOW, Vector3(0, 0.6, 0.12), Vector3(0.04, 0.12, 0.02), col.lightened(0.4) * 0.6, 3, 4)
	return _b(k)[0]


static func shrine_mesh(seed_value: int, col := Color(0.75, 0.72, 0.65)) -> ArrayMesh:
	var k := Kit.new(seed_value)
	k.cyl(MATTE, Vector3.ZERO, 0.5, 0.48, 0.1, 10, col.darkened(0.3), col.darkened(0.15))
	for i in 6:
		var a := TAU * i / 6.0
		k.cyl(MATTE, Vector3(cos(a) * 0.36, 0.1, sin(a) * 0.36), 0.05, 0.045, 0.6, 6, col.darkened(0.1), col)
	k.cyl(MATTE, Vector3(0, 0.7, 0), 0.46, 0.0, 0.32, 10, Color(0.3, 0.35, 0.55), Color(0.4, 0.45, 0.65))
	k.sphere(GLOW, Vector3(0, 0.35, 0), Vector3(0.1, 0.1, 0.1), Color(0.4, 0.7, 1.0), 4, 6)
	return _b(k)[0]


static func tower_mesh(seed_value: int) -> Array:
	var k := Kit.new(seed_value)
	var stone := Color(0.55, 0.52, 0.48)
	k.cyl(MATTE, Vector3.ZERO, 0.32, 0.26, 1.5, 10, stone.darkened(0.2), stone)
	k.cyl(MATTE, Vector3(0, 1.5, 0), 0.34, 0.0, 0.55, 10, Color(0.25, 0.3, 0.45), Color(0.35, 0.4, 0.55))
	k.box(GLOW, Vector3(0, 1.1, 0.27), Vector3(0.08, 0.16, 0.02), Color(1.0, 0.75, 0.35) * 0.7)
	k.box(MATTE, Vector3(0, 0.16, 0.27), Vector3(0.16, 0.32, 0.04), Color(0.2, 0.12, 0.07))
	k.flag(Vector3(0, 2.0, 0), 0.4, 0.3)
	return _b(k)


static func tent_mesh(seed_value: int) -> Array:
	var k := Kit.new(seed_value)
	var cloth: Color = [Color(0.7, 0.6, 0.42), Color(0.55, 0.25, 0.2), Color(0.35, 0.4, 0.55)][seed_value % 3]
	k.cyl(MATTE, Vector3(0, 0, -0.05), 0.42, 0.0, 0.7, 8, cloth.darkened(0.25), cloth)
	k.box(MATTE, Vector3(0, 0.14, 0.33), Vector3(0.16, 0.28, 0.04), Color(0.12, 0.08, 0.05))
	k.flag(Vector3(0, 0.55, -0.05), 0.5, 0.25)
	k.box(MATTE, Vector3(0.4, 0.08, 0.2), Vector3(0.14, 0.16, 0.14), Color(0.45, 0.3, 0.15))
	return _b(k)


static func well_mesh(seed_value: int) -> ArrayMesh:
	var k := Kit.new(seed_value)
	k.cyl(MATTE, Vector3.ZERO, 0.28, 0.28, 0.3, 12, Color(0.45, 0.43, 0.4), Color(0.55, 0.52, 0.48))
	k.cyl(SHINY, Vector3(0, 0.26, 0), 0.22, 0.22, 0.01, 12, Color(0.15, 0.35, 0.6), Color(0.3, 0.55, 0.85))
	for sx in [-0.25, 0.25]:
		k.box(MATTE, Vector3(sx, 0.45, 0), Vector3(0.05, 0.6, 0.05), Color(0.35, 0.24, 0.13))
	k.cyl(MATTE, Vector3(0, 0.72, 0), 0.4, 0.0, 0.25, 4, Color(0.4, 0.22, 0.12), Color(0.5, 0.3, 0.15))
	return _b(k)[0]


static func gate_mesh(seed_value: int, size: Vector2) -> ArrayMesh:
	var k := Kit.new(seed_value)
	var hw := size.x * 0.5
	k.snow = 0.6
	for i in 6:
		var p := Vector3(k.rng.randf_range(-hw, hw), 0.0, k.rng.randf_range(-0.6, 0.0))
		k.sphere(MATTE, p, Vector3(0.55, 0.65, 0.5), Color(0.34, 0.31, 0.29), 4, 7, 0.35)
	k.snow = 0.0
	k.box(MATTE, Vector3(0, 0.3, 0.12), Vector3(0.5, 0.6, 0.3), Color(0.02, 0.02, 0.02))
	for sx in [-0.33, 0.33]:
		k.box(MATTE, Vector3(sx, 0.35, 0.22), Vector3(0.14, 0.7, 0.16), Color(0.45, 0.42, 0.38))
	k.box(MATTE, Vector3(0, 0.74, 0.22), Vector3(0.82, 0.14, 0.18), Color(0.45, 0.42, 0.38))
	return _b(k)[0]


static func portal_mesh(seed_value: int) -> ArrayMesh:
	var k := Kit.new(seed_value)
	k.cyl(MATTE, Vector3.ZERO, 0.4, 0.38, 0.08, 12, Color(0.4, 0.38, 0.36), Color(0.5, 0.48, 0.45))
	k.sphere(GLOW, Vector3(0, 0.5, 0), Vector3(0.25, 0.42, 0.08), Color(0.3, 0.6, 1.0), 5, 10)
	for sx in [-0.32, 0.32]:
		k.cyl(MATTE, Vector3(sx, 0.08, 0), 0.07, 0.06, 0.9, 6, Color(0.45, 0.42, 0.4), Color(0.55, 0.52, 0.48))
	return _b(k)[0]


static func boat_mesh(seed_value: int) -> ArrayMesh:
	var k := Kit.new(seed_value)
	var wood := Color(0.4, 0.26, 0.14)
	var pts := [Vector3(0, 0.25, 0.55), Vector3(0.22, 0.25, 0.2), Vector3(0.22, 0.25, -0.35), Vector3(0, 0.25, -0.5), Vector3(-0.22, 0.25, -0.35), Vector3(-0.22, 0.25, 0.2)]
	var ctr := Vector3(0, 0.15, 0)
	for i in pts.size():
		var a: Vector3 = pts[i]
		var b: Vector3 = pts[(i + 1) % pts.size()]
		var a2 := Vector3(a.x * 0.5, 0.0, a.z * 0.85)
		var b2 := Vector3(b.x * 0.5, 0.0, b.z * 0.85)
		k.fquad(MATTE, a, b, b2, a2, wood, ctr)
	for i in range(1, pts.size() - 1):
		k.ftri(MATTE, pts[0] - Vector3(0, 0.04, 0), pts[i] - Vector3(0, 0.04, 0), pts[i + 1] - Vector3(0, 0.04, 0), wood.lightened(0.15), Vector3(0, -1, 0))
	k.cyl(MATTE, Vector3(0, 0.2, 0), 0.03, 0.025, 1.1, 5, wood, wood)
	k.box(MATTE, Vector3(0, 0.75, 0.02), Vector3(0.6, 0.55, 0.02), Color(0.85, 0.82, 0.72))
	k.flag(Vector3(0, 1.25, 0), 0.2, 0.22)
	return _b(k)[0]


static func ruins_mesh(seed_value: int, size: Vector2) -> ArrayMesh:
	var k := Kit.new(seed_value)
	var stone := Color(0.5, 0.48, 0.44)
	var hw := size.x * 0.4
	for i in 7:
		var p := Vector3(k.rng.randf_range(-hw, hw), 0.0, k.rng.randf_range(-0.4, 0.2))
		var h := k.rng.randf_range(0.2, 0.9)
		k.cyl(MATTE, p, 0.08, 0.07, h, 8, stone.darkened(0.2), stone)
	k.box(MATTE, Vector3(0, 0.08, -0.1), Vector3(hw * 2.0, 0.16, 0.6), stone.darkened(0.3))
	k.box(MATTE, Vector3(0, 0.4, -0.3), Vector3(0.7, 0.8, 0.2), stone.darkened(0.1))
	k.box(MATTE, Vector3(0, 0.25, -0.18), Vector3(0.3, 0.5, 0.06), Color(0.05, 0.04, 0.04))
	k.sphere(GLOW, Vector3(0, 0.6, -0.15), Vector3(0.05, 0.05, 0.03), Color(0.3, 1.0, 0.5) * 0.6, 3, 5)
	return _b(k)[0]


static func sign_mesh(seed_value: int) -> ArrayMesh:
	var k := Kit.new(seed_value)
	var wood := Color(0.42, 0.28, 0.15)
	k.box(MATTE, Vector3(0, 0.3, 0), Vector3(0.06, 0.6, 0.06), wood)
	k.box(MATTE, Vector3(0, 0.5, 0.04), Vector3(0.42, 0.2, 0.04), wood.lightened(0.15), 0.1)
	return _b(k)[0]


static func pandora_mesh() -> ArrayMesh:
	var k := Kit.new(9)
	k.box(METAL, Vector3(0, 0.14, 0), Vector3(0.3, 0.28, 0.3), Color(0.8, 0.6, 0.2))
	k.box(SHINY, Vector3(0, 0.3, 0), Vector3(0.34, 0.06, 0.34), Color(0.5, 0.1, 0.5))
	k.sphere(GLOW, Vector3(0, 0.38, 0), Vector3(0.06, 0.06, 0.06), Color(1.0, 0.5, 1.0), 3, 6)
	return _b(k)[0]


static func wagon_mesh() -> ArrayMesh:
	var k := Kit.new(4)
	var wood := Color(0.4, 0.27, 0.15)
	k.box(MATTE, Vector3(0, 0.25, 0), Vector3(0.7, 0.2, 0.4), wood)
	for x in [-0.25, 0.25]:
		for z in [-0.22, 0.22]:
			var save := k.xf
			k.xf = Transform3D(Basis(Vector3.BACK, PI * 0.5), Vector3(x, 0.15, z))
			k.cyl(MATTE, Vector3(0, -0.02, 0), 0.15, 0.15, 0.04, 10, wood.darkened(0.3), wood.darkened(0.3))
			k.xf = save
	k.cyl(MATTE, Vector3(0, 0.35, 0), 0.25, 0.25, 0.04, 8, Color(0.85, 0.8, 0.7), Color(0.85, 0.8, 0.7))
	return _b(k)[0]


# ============================================================================================= creatures

## Generic creature statue: body, head, weapon; colours by faction.
static func creature_mesh(cid: int) -> ArrayMesh:
	var key := "cr_%d" % cid
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(cid * 7 + 1)
	var fac := Defs.creature_faction(cid)
	var body := Color(0.55, 0.5, 0.45)
	var cloth := Color(0.5, 0.45, 0.4)
	var big := (cid % 14) >= 10 if cid < 112 else cid >= 132
	match fac:
		0: body = Color(0.7, 0.72, 0.75); cloth = Color(0.2, 0.3, 0.7)
		1: body = Color(0.55, 0.45, 0.3); cloth = Color(0.25, 0.5, 0.2)
		2: body = Color(0.8, 0.78, 0.72); cloth = Color(0.35, 0.45, 0.8)
		3: body = Color(0.6, 0.2, 0.12); cloth = Color(0.25, 0.1, 0.08)
		4: body = Color(0.82, 0.8, 0.7); cloth = Color(0.2, 0.25, 0.22)
		5: body = Color(0.45, 0.35, 0.5); cloth = Color(0.35, 0.15, 0.4)
		6: body = Color(0.45, 0.55, 0.3); cloth = Color(0.5, 0.3, 0.15)
		7: body = Color(0.35, 0.45, 0.3); cloth = Color(0.3, 0.3, 0.2)
		8: body = Color(0.45, 0.65, 0.85); cloth = Color(0.8, 0.85, 0.95)
	var s := 1.35 if big else 1.0
	k.cyl(MATTE, Vector3.ZERO, 0.3 * s, 0.28 * s, 0.05, 12, Color(0.25, 0.22, 0.2), Color(0.3, 0.27, 0.24))
	# legs
	for sx in [-0.07, 0.07]:
		k.cyl(MATTE, Vector3(sx * s, 0.05, 0), 0.05 * s, 0.045 * s, 0.22 * s, 6, cloth.darkened(0.3), cloth)
	k.sphere(MATTE, Vector3(0, 0.38 * s, 0), Vector3(0.14, 0.18, 0.11) * s, body, 4, 8)
	k.sphere(MATTE, Vector3(0, 0.62 * s, 0.01), Vector3(0.08, 0.09, 0.08) * s, body.lightened(0.1), 4, 8)
	k.cyl(MATTE, Vector3(0, 0.2 * s, 0), 0.16 * s, 0.12 * s, 0.22 * s, 8, cloth, cloth.lightened(0.1))
	# weapon / shield
	k.box(METAL, Vector3(0.2 * s, 0.45 * s, 0.05), Vector3(0.03, 0.55, 0.03) * s, Color(0.7, 0.7, 0.72))
	k.sphere(MATTE, Vector3(-0.17 * s, 0.4 * s, 0.05), Vector3(0.03, 0.12, 0.1) * s, cloth.darkened(0.2), 3, 8)
	if big:
		# wings for the big ones
		for sx in [-1.0, 1.0]:
			var a := Vector3(0.08 * sx, 0.5, -0.05) * s
			var b := Vector3(0.5 * sx, 0.85, -0.15) * s
			var c := Vector3(0.45 * sx, 0.35, -0.12) * s
			k.ftri(MATTE, a, b, c, body.darkened(0.2), Vector3(0, 0.5, -1) * s)
			k.ftri(MATTE, a, c, b, body.darkened(0.3), Vector3(0, 0.5, 1) * s)
	var res: ArrayMesh = _b(k)[0]
	_cache[key] = res
	return res


## Hero: rider on a horse with a banner in the owner colour (FLAG surface).
static func hero_mesh(hero_class: int) -> Array:
	var key := "hero_%d" % hero_class
	if _cache.has(key):
		return _cache[key]
	var k := Kit.new(77 + hero_class)
	var horse: Color = [Color(0.35, 0.22, 0.12), Color(0.85, 0.83, 0.78), Color(0.15, 0.13, 0.12), Color(0.55, 0.4, 0.25)][hero_class % 4]
	var armor := Color(0.7, 0.72, 0.76) if hero_class % 2 == 0 else Color(0.35, 0.25, 0.4)
	# horse body along Z
	k.sphere(MATTE, Vector3(0, 0.42, 0), Vector3(0.13, 0.13, 0.3), horse, 5, 9)
	for p in [Vector3(-0.08, 0, 0.2), Vector3(0.08, 0, 0.2), Vector3(-0.08, 0, -0.2), Vector3(0.08, 0, -0.2)]:
		k.cyl(MATTE, p, 0.035, 0.03, 0.38, 5, horse.darkened(0.4), horse)
	k.cyl(MATTE, Vector3(0, 0.45, 0.24), 0.07, 0.05, 0.22, 6, horse, horse, true, Vector3(0, 0, 0.12))
	k.sphere(MATTE, Vector3(0, 0.66, 0.4), Vector3(0.05, 0.06, 0.12), horse, 4, 6)
	k.cyl(MATTE, Vector3(0, 0.5, -0.28), 0.03, 0.01, 0.25, 4, horse.darkened(0.5), horse.darkened(0.5), false, Vector3(0, -0.2, -0.1))
	# saddle cloth in owner colour
	k.box(FLAG, Vector3(0, 0.52, -0.02), Vector3(0.28, 0.1, 0.24), Color.WHITE)
	# rider
	k.cyl(MATTE, Vector3(0, 0.56, -0.02), 0.09, 0.07, 0.28, 8, armor.darkened(0.2), armor)
	k.sphere(METAL, Vector3(0, 0.92, -0.02), Vector3(0.065, 0.075, 0.065), armor.lightened(0.1), 4, 7)
	k.cyl(FLAG, Vector3(0, 0.6, -0.1), 0.1, 0.07, 0.25, 6, Color.WHITE, Color.WHITE, false)
	# lance with pennant
	k.cyl(MATTE, Vector3(0.12, 0.45, 0.0), 0.012, 0.012, 0.95, 4, Color(0.4, 0.3, 0.2), Color(0.5, 0.4, 0.3))
	var t := Vector3(0.12, 1.35, 0.0)
	var nrm := Vector3(1, 0, 0)
	k.tri(FLAG, t, t + Vector3(0, -0.14, 0), t + Vector3(0, -0.07, -0.25), nrm, nrm, nrm, Color.WHITE, Color.WHITE, Color.WHITE)
	var res: Array = _b(k)
	_cache[key] = res
	return res
