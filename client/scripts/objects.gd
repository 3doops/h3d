extends Node3D
## Map objects of all levels: manifest GLBs or procedural fallbacks, tree/plant instancing, owner colours,
## fog-of-war visibility.

const Defs = preload("res://scripts/defs.gd")
const Procgen = preload("res://scripts/procgen.gd")
const Terrain = preload("res://scripts/terrain.gd")
const MANIFEST := "res://assets/models/manifest.json"

var manifest := {"models": {}, "variants": {}}
var levels: Array = []          # terrain level nodes
var players: Array = []
var towns: Array = []
var objs := {}                  # id -> {data, node, cells, level, flag_meshes}
var instanced := []             # per level: Array of [mesh, Transform3D, Vector2i cell, obj_id]
var mm_nodes := []              # per level: Node3D holding MultiMeshInstance3D
var level_roots := []
var animated: Array = []        # [node, axis, speed]
var _glb_cache := {}
var _fog_dirty := false
var stats := {"glb": 0, "fallback": 0, "instanced": 0}
var generate_lods := true
var keepalive: Callable      # called now and then during long builds (network pings)


func load_manifest() -> void:
	var path := MANIFEST
	var text := ""
	if FileAccess.file_exists(path):
		text = FileAccess.get_file_as_string(path)
	if text == "":
		print("objects: no manifest.json, procedural fallbacks only")
		return
	var m = JSON.parse_string(text)
	if m is Dictionary:
		manifest.models = m.get("models", {})
		manifest.variants = m.get("variants", {})
		print("objects: manifest with %d models, %d variant sets" % [manifest.models.size(), manifest.variants.size()])
	else:
		push_warning("objects: manifest.json is not valid JSON")


func build(snapshot: Dictionary, level_nodes: Array) -> void:
	for c in get_children():
		c.queue_free()
	objs.clear()
	animated.clear()
	levels = level_nodes
	players = snapshot.get("players", [])
	towns = snapshot.get("towns", [])
	instanced = []
	mm_nodes = []
	level_roots = []
	for z in levels.size():
		instanced.append([])
		var root := Node3D.new()
		root.name = "Level%d" % z
		add_child(root)
		level_roots.append(root)
		var mm := Node3D.new()
		mm.name = "Instanced"
		root.add_child(mm)
		mm_nodes.append(mm)
	var k := 0
	for o in snapshot.get("objects", []):
		add_object(o)
		k += 1
		if k % 40 == 0 and keepalive.is_valid():
			keepalive.call()
	var hero_cells := {}
	for h in snapshot.get("heroes", []):
		hero_cells[Vector3i(int(h.x), int(h.y), int(h.z))] = true
	for z in levels.size():
		_dress_level(z, hero_cells)
	_rebuild_instanced_all()
	update_fog()
	print("objects: %d (glb %d, fallback %d, instanced %d)" % [objs.size(), stats.glb, stats.fallback, stats.instanced])


func set_level_visible(z: int) -> void:
	for i in level_roots.size():
		level_roots[i].visible = i == z


# ------------------------------------------------------------------------------------------- lookup

func _model_entry(o: Dictionary) -> Variant:
	var t := int(o.type)
	var models: Dictionary = manifest.models
	var keys := []
	var d := String(o.get("def", "")).to_lower().get_basename()
	if d != "":
		keys.append("def:" + d)
	if t == 54 or (t >= 71 and t <= 75) or (t >= 162 and t <= 164):
		keys.append("creature:%d" % int(o.subtype))
	keys.append("obj:%d:%d" % [t, int(o.subtype)])
	keys.append("obj:%d" % t)
	for k in keys:
		if manifest.variants.has(k):
			var arr: Array = manifest.variants[k]
			if arr.size() > 0:
				var base: Dictionary = models.get(k, {})
				var e := base.duplicate()
				e.glb = arr[int(o.id) % arr.size()]
				return e
		if models.has(k):
			return models[k]
	return null


func load_glb(path: String) -> Node3D:
	if _glb_cache.has(path):
		var tpl = _glb_cache[path]
		return tpl.duplicate() if tpl else null
	var node: Node3D = null
	if ResourceLoader.exists(path):
		var res = load(path)
		if res is PackedScene:
			node = res.instantiate()
	if node == null:
		var file := ProjectSettings.globalize_path(path) if path.begins_with("res://") else path
		if FileAccess.file_exists(file):
			var doc := GLTFDocument.new()
			var st := GLTFState.new()
			if doc.append_from_file(file, st) == OK:
				# runtime GLB load has no editor import step: build LODs so hundreds of map objects stay cheap
				if generate_lods:
					for gm in st.get_meshes():
						var im: ImporterMesh = gm.mesh
						if im and im.get_surface_count() > 0:
							im.generate_lods(25.0, 60.0, [])
				node = doc.generate_scene(st)
	if node == null:
		push_warning("objects: cannot load %s" % path)
	_glb_cache[path] = node
	return node.duplicate() if node else null


# ------------------------------------------------------------------------------------------- placement

func _rect(cells: Array[Vector2i]) -> Rect2:
	var minx := 1 << 20; var miny := 1 << 20; var maxx := -(1 << 20); var maxy := -(1 << 20)
	for c in cells:
		minx = mini(minx, c.x); miny = mini(miny, c.y); maxx = maxi(maxx, c.x); maxy = maxi(maxy, c.y)
	return Rect2(minx, miny, maxx - minx + 1, maxy - miny + 1)


func _ground(level, cells: Array[Vector2i], center: Vector2) -> float:
	var hs := 0.0
	var mn := 1e9
	for c in cells:
		var h: float = level.height_at(c.x + 0.5, c.y + 0.5)
		hs += h
		mn = minf(mn, h)
	var avg := hs / maxf(cells.size(), 1)
	return maxf(lerpf(mn, avg, 0.5), Terrain.WATER_Y + 0.02)


func add_object(o: Dictionary) -> void:
	var z := int(o.get("z", 0))
	if z >= levels.size():
		return
	var cat := Defs.category(o)
	if cat == "none":
		return
	var level = levels[z]
	var cells := Defs.obj_cells(o)
	var block := Defs.mask_cells(String(o.get("block", "")), int(o.x), int(o.y))
	if block.is_empty():
		block = cells
	var rect := _rect(cells)
	var center := rect.get_center()
	var entry := {"data": o, "node": null, "cells": cells, "level": z, "cat": cat, "flags": []}
	objs[int(o.id)] = entry
	var m = _model_entry(o)
	var node: Node3D = null
	if m != null:
		node = load_glb(String(m.glb))
		if node:
			stats.glb += 1
			var sc := float(m.get("scale", 1.0))
			node.scale = Vector3(sc, sc, sc)
			var off: Array = m.get("offset", [0, 0, 0])
			node.position = Vector3(float(off[0]), float(off[1]), float(off[2]))
			node.rotation.y = deg_to_rad(float(m.get("rot_y", 0)))
			var holder := Node3D.new()
			holder.add_child(node)
			node = holder
			_collect_player_color(entry, node)
	if node == null:
		node = _fallback(o, cat, entry, block, rect, level)
		if node == null:
			return   # instanced (trees, plants)
		stats.fallback += 1
	node.position = Vector3(center.x, _ground(level, block, center), center.y)
	if cat == "boat":
		node.position.y = Terrain.WATER_Y - 0.05
	node.name = "obj_%d_%s" % [int(o.id), cat]
	level_roots[z].add_child(node)
	entry.node = node
	apply_owner(entry)


func _collect_player_color(entry: Dictionary, root: Node) -> void:
	for mi in root.find_children("*", "MeshInstance3D", true, false):
		var mesh: Mesh = mi.mesh
		if mesh == null:
			continue
		for s in mesh.get_surface_count():
			var mat := mesh.surface_get_material(s)
			if mat and mat.resource_name.begins_with("PlayerColor"):
				entry.flags.append([mi, s])


func apply_owner(entry: Dictionary) -> void:
	var col := Defs.owner_color(int(entry.data.get("owner", -1)), players)
	var mat := Procgen.flag_material(col)
	for f in entry.flags:
		var mi: MeshInstance3D = f[0]
		mi.set_surface_override_material(int(f[1]), mat)


func _mesh_node(mesh: Mesh, flag_idx := -1, entry = null) -> MeshInstance3D:
	var mi := MeshInstance3D.new()
	mi.mesh = mesh
	if flag_idx >= 0 and entry != null:
		entry.flags.append([mi, flag_idx])
	return mi


func _snowy(level, o: Dictionary) -> bool:
	var d := String(o.get("def", "")).to_lower()
	if "sn" in d.substr(3):
		return true
	var t: int = level.ttype(int(o.x), int(o.y))
	return t == Defs.SNOW


func _tree_kind(o: Dictionary, terr: int) -> String:
	var d := String(o.get("def", "")).to_lower()
	var t := int(o.type)
	if t in [119, 170]:
		return "dead"
	if t in [153, 197]:
		return "stump"
	if t in [137, 188] or "pntr" in d or "sntr" in d:
		return "pine"
	if "autr" in d:
		return "autumn"
	if "sptr" in d or t in [135, 186]:
		return "oak"
	if "swmp" in d or "wlw" in d or t in [159, 203]:
		return "willow"
	if "yuc" in d or "plm" in d:
		return "bush"
	match terr:
		Defs.SNOW, Defs.ROUGH:
			return "pine"
		Defs.SWAMP:
			return "willow"
		Defs.SAND, Defs.WASTELAND:
			return "bush"
		Defs.SUBTER:
			return "mushroom"
	return "pine" if (int(o.id) % 3) != 0 else "oak"


func _fallback(o: Dictionary, cat: String, entry: Dictionary, block: Array[Vector2i], rect: Rect2, level) -> Node3D:
	var oid := int(o.id)
	var z := int(o.z)
	var rng := RandomNumberGenerator.new()
	rng.seed = oid * 7919
	var center := rect.get_center()
	var size := rect.size
	var vis := Defs.mask_cells(String(o.get("visit", "")), int(o.x), int(o.y))
	var gate_x := 0.0
	if not vis.is_empty():
		gate_x = vis[0].x + 0.5 - center.x
	var snowy := _snowy(level, o)
	match cat:
		"tree_pine", "tree_oak", "tree_mixed", "tree_willow", "tree_dead", "stump", "plant", "flowers", "mushrooms", "log":
			var terr: int = level.ttype(int(o.x), int(o.y))
			var kind := _tree_kind(o, terr)
			if cat == "plant":
				kind = "bush" if terr != Defs.SUBTER else "mushroom"
			elif cat == "flowers":
				kind = "flowers"
			elif cat == "mushrooms":
				kind = "mushroom"
			elif cat == "log":
				kind = "stump"
			for c in block:
				var n := 1
				if kind in ["pine", "oak", "autumn"]:
					n = 2 + rng.randi() % 2
				elif kind in ["willow", "dead"]:
					n = 1 + rng.randi() % 2
				for i in n:
					var k2 := kind
					if kind == "oak" and terr == Defs.GRASS and rng.randf() < 0.3:
						k2 = "pine"
					if kind == "pine" and terr == Defs.GRASS and rng.randf() < 0.25:
						k2 = "autumn" if rng.randf() < 0.4 else "oak"
					var mesh := Procgen.tree_mesh(k2, rng.randi() % 3, snowy and k2 != "willow")
					var p := Vector2(c.x + rng.randf_range(0.15, 0.85), c.y + rng.randf_range(0.15, 0.85))
					var s := rng.randf_range(0.75, 1.25) * (1.15 if k2 == "pine" else 1.0)
					var tr := Transform3D(Basis(Vector3.UP, rng.randf() * TAU).scaled(Vector3(s, s * rng.randf_range(0.9, 1.15), s)),
						Vector3(p.x, level.height_at(p.x, p.y) - 0.03, p.y))
					instanced[z].append([mesh, tr, c, oid])
					stats.instanced += 1
			return null
		"mountain", "hill", "volcano":
			var rel := []
			for c in block:
				rel.append(Vector2(c.x + 0.5 - center.x, c.y + 0.5 - center.y))
			var d := String(o.get("def", "")).to_lower()
			var hscale := 1.0 if cat == "mountain" else 0.45
			var mesh := Procgen.mountain_mesh(rel, oid, snowy or "sn" in d, "gr" in d or level.ttype(int(o.x), int(o.y)) == Defs.GRASS, hscale)
			var mi := _mesh_node(mesh)
			if cat == "volcano":
				var glow := _mesh_node(Procgen.flat_patch_mesh(Vector2(0.5, 0.5), Color(1.0, 0.4, 0.05), true, 1.0))
				mi.add_child(glow)
			return _wrap(mi)
		"rock", "skull":
			if cat == "skull":
				return _wrap(_mesh_node(Procgen.rock_mesh(Vector2(0.6, 0.5), oid, false)))
			return _wrap(_mesh_node(Procgen.rock_mesh(size, oid, snowy, level.level > 0)))
		"stalagmite":
			return _wrap(_mesh_node(Procgen.stalagmite_mesh(oid)))
		"crater":
			return _wrap(_mesh_node(Procgen.flat_patch_mesh(size * 0.9, Color(0.18, 0.15, 0.12))))
		"tar":
			return _wrap(_mesh_node(Procgen.flat_patch_mesh(size * 0.9, Color(0.03, 0.03, 0.03))))
		"lake":
			return _wrap(_mesh_node(Procgen.flat_patch_mesh(size * 0.95, Color(0.06, 0.2, 0.3))))
		"frozen_lake":
			return _wrap(_mesh_node(Procgen.flat_patch_mesh(size * 0.95, Color(0.7, 0.82, 0.9))))
		"lava_lake":
			return _wrap(_mesh_node(Procgen.flat_patch_mesh(size * 0.95, Color(1.0, 0.35, 0.05), true)))
		"overlay":
			var oc := Color(0.22, 0.12, 0.26) if int(o.type) in [21, 223, 224] else Color(0.45, 0.5, 0.62)
			var pm := _mesh_node(Procgen.flat_patch_mesh(size * 0.9, oc, false, 0.03))
			pm.transparency = 0.55
			pm.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			return _wrap(pm)
		"resource":
			return _wrap(_mesh_node(Procgen.resource_mesh(int(o.subtype) if int(o.type) == 79 else rng.randi() % 7)))
		"chest":
			var cm := _mesh_node(Procgen.chest_mesh())
			cm.rotation.y = rng.randf_range(-0.4, 0.4)
			return _wrap(cm)
		"artifact":
			return _wrap(_mesh_node(Procgen.artifact_mesh(int(o.subtype))))
		"pandora":
			return _wrap(_mesh_node(Procgen.pandora_mesh()))
		"campfire":
			var n := _wrap(_mesh_node(Procgen.campfire_mesh()))
			var light := OmniLight3D.new()
			light.light_color = Color(1.0, 0.6, 0.25)
			light.light_energy = 1.2
			light.omni_range = 2.0
			light.position = Vector3(0, 0.4, 0)
			n.add_child(light)
			return n
		"wagon":
			return _wrap(_mesh_node(Procgen.wagon_mesh()))
		"town":
			var fac := int(o.subtype)
			for t in towns:
				# town x,y = entrance cell (anchor - 2 in x); objects may also carry "town" = town id
				var same: bool = int(o.get("town", -2)) == int(t.id) or (int(t.z) == z and int(t.y) == int(o.y) and (int(t.x) == int(o.x) - 2 or int(t.x) == int(o.x)))
				if same:
					fac = int(t.faction)
			var r := Procgen.town_mesh(size, fac, gate_x, oid)
			return _wrap(_mesh_node(r[0], r[1], entry))
		"mine":
			var r := Procgen.mine_mesh(size, int(o.subtype), gate_x, snowy, oid)
			return _wrap(_mesh_node(r[0], r[1], entry))
		"dwelling":
			var r := Procgen.house_mesh(size, -1, gate_x, oid, int(o.type) == 20)
			return _wrap(_mesh_node(r[0], r[1], entry))
		"windmill":
			var r := Procgen.windmill_mesh(oid)
			var root := _wrap(_mesh_node(r[0]))
			var blades := _mesh_node(r[1])
			blades.position = Vector3(0, 0.95, 0.2)
			root.add_child(blades)
			animated.append([blades, Vector3.BACK, 0.8])
			return root
		"watermill":
			var r := Procgen.watermill_mesh(oid)
			var root := _wrap(_mesh_node(r[0], r[2], entry))
			var wheel := _mesh_node(r[1])
			wheel.position = Vector3(size.x * 0.4, 0.3, 0.1)
			root.add_child(wheel)
			animated.append([wheel, Vector3.RIGHT, 0.6])
			return root
		"obelisk":
			return _wrap(_mesh_node(Procgen.obelisk_mesh(oid)))
		"shrine":
			return _wrap(_mesh_node(Procgen.shrine_mesh(oid)))
		"tower":
			var r := Procgen.tower_mesh(oid)
			return _wrap(_mesh_node(r[0], r[1], entry))
		"tent", "garden", "gatehouse", "garrison", "shipyard":
			if size.x >= 2:
				var r := Procgen.house_mesh(size, -1, gate_x, oid)
				return _wrap(_mesh_node(r[0], r[1], entry))
			var r2 := Procgen.tent_mesh(oid)
			return _wrap(_mesh_node(r2[0], r2[1], entry))
		"well":
			return _wrap(_mesh_node(Procgen.well_mesh(oid)))
		"gate":
			return _wrap(_mesh_node(Procgen.gate_mesh(oid, size)))
		"portal":
			return _wrap(_mesh_node(Procgen.portal_mesh(oid)))
		"boat":
			return _wrap(_mesh_node(Procgen.boat_mesh(oid)))
		"ruins", "pyramid":
			return _wrap(_mesh_node(Procgen.ruins_mesh(oid, size)))
		"sign":
			return _wrap(_mesh_node(Procgen.sign_mesh(oid)))
		"monster":
			var cid := int(o.subtype) if int(o.type) == 54 else -1
			var mn := _mesh_node(Procgen.creature_mesh(cid))
			mn.rotation.y = rng.randf_range(-0.5, 0.5)
			return _wrap(mn)
	return _wrap(_mesh_node(Procgen.rock_mesh(Vector2(0.8, 0.8), oid, snowy)))


## Purely cosmetic small vegetation and stones on free cells (not map objects, never block anything).
func _dress_level(z: int, hero_cells: Dictionary) -> void:
	var level = levels[z]
	var n: int = level.n
	var used := {}
	for id in objs:
		var e: Dictionary = objs[id]
		if e.level == z:
			for c in e.cells:
				used[c] = true
	var rng := RandomNumberGenerator.new()
	rng.seed = 4242 + z
	for y in n:
		for x in n:
			var c := Vector2i(x, y)
			if used.has(c) or hero_cells.has(Vector3i(x, y, z)):
				continue
			var t: int = level.ttype(x, y)
			var cx := x + 0.5
			var cy := y + 0.5
			if level.field_at(level.road_f, cx, cy) < 0.5 or level.field_at(level.river_f, cx, cy) < 0.55:
				continue
			var kinds := []
			match t:
				Defs.SNOW: kinds = [["pine_s", 0.22], ["rock_s", 0.1], ["twig_s", 0.1]]
				Defs.GRASS, Defs.HIGHLANDS: kinds = [["bush", 0.3], ["flowers", 0.12], ["pine", 0.1], ["rock", 0.06]]
				Defs.DIRT, Defs.ROUGH, Defs.WASTELAND: kinds = [["rock", 0.22], ["bush", 0.1], ["twig", 0.06]]
				Defs.SWAMP: kinds = [["reeds", 0.45], ["bush", 0.12]]
				Defs.SAND: kinds = [["rock", 0.1]]
				Defs.SUBTER: kinds = [["mushroom", 0.1], ["rock_d", 0.12]]
				Defs.LAVA: kinds = [["rock_d", 0.15]]
			for kd in kinds:
				if rng.randf() >= float(kd[1]):
					continue
				var mesh: Mesh
				var sc := 1.0
				match String(kd[0]):
					"pine_s": mesh = Procgen.tree_mesh("pine", rng.randi() % 3, true); sc = rng.randf_range(0.35, 0.6)
					"pine": mesh = Procgen.tree_mesh("pine", rng.randi() % 3, false); sc = rng.randf_range(0.35, 0.6)
					"rock_s": mesh = Procgen.rock_mesh(Vector2(0.5, 0.5), rng.randi() % 5, true); sc = rng.randf_range(0.35, 0.6)
					"rock": mesh = Procgen.rock_mesh(Vector2(0.5, 0.5), rng.randi() % 5, false); sc = rng.randf_range(0.3, 0.55)
					"rock_d": mesh = Procgen.rock_mesh(Vector2(0.5, 0.5), rng.randi() % 5, false, true); sc = rng.randf_range(0.3, 0.6)
					"twig_s": mesh = Procgen.tree_mesh("dead", rng.randi() % 3, true); sc = rng.randf_range(0.3, 0.45)
					"twig": mesh = Procgen.tree_mesh("dead", rng.randi() % 3, false); sc = rng.randf_range(0.3, 0.45)
					"bush": mesh = Procgen.tree_mesh("bush", rng.randi() % 3, false); sc = rng.randf_range(0.6, 1.0)
					"flowers": mesh = Procgen.tree_mesh("flowers", rng.randi() % 3, false); sc = rng.randf_range(0.7, 1.0)
					"reeds": mesh = Procgen.tree_mesh("reeds", rng.randi() % 3, false); sc = rng.randf_range(0.7, 1.1)
					"mushroom": mesh = Procgen.tree_mesh("mushroom", rng.randi() % 3, false); sc = rng.randf_range(0.5, 0.8)
				var p := Vector2(x + rng.randf_range(0.2, 0.8), y + rng.randf_range(0.2, 0.8))
				var tr := Transform3D(Basis(Vector3.UP, rng.randf() * TAU).scaled(Vector3(sc, sc, sc)),
					Vector3(p.x, level.height_at(p.x, p.y) - 0.02, p.y))
				instanced[z].append([mesh, tr, c, -1])


func _wrap(mi: Node3D) -> Node3D:
	var n := Node3D.new()
	n.add_child(mi)
	return n


# ------------------------------------------------------------------------------------------- instancing + fog

func _rebuild_instanced(z: int) -> void:
	var holder: Node3D = mm_nodes[z]
	for c in holder.get_children():
		c.queue_free()
	var buckets := {}
	var level = levels[z]
	for e in instanced[z]:
		var cell: Vector2i = e[2]
		if not level.is_open(cell.x, cell.y):
			continue
		var mesh: Mesh = e[0]
		if not buckets.has(mesh):
			buckets[mesh] = []
		buckets[mesh].append(e[1])
	for mesh in buckets:
		var arr: Array = buckets[mesh]
		var mm := MultiMesh.new()
		mm.transform_format = MultiMesh.TRANSFORM_3D
		mm.mesh = mesh
		mm.instance_count = arr.size()
		for i in arr.size():
			mm.set_instance_transform(i, arr[i])
		var mmi := MultiMeshInstance3D.new()
		mmi.multimesh = mm
		holder.add_child(mmi)


func _rebuild_instanced_all() -> void:
	for z in levels.size():
		_rebuild_instanced(z)


func update_fog() -> void:
	for id in objs:
		var e: Dictionary = objs[id]
		if e.node == null:
			continue
		var level = levels[e.level]
		var vis := false
		for c in e.cells:
			if level.is_open(c.x, c.y):
				vis = true
				break
		e.node.visible = vis


func fog_changed() -> void:
	if not _fog_dirty:
		_fog_dirty = true
		call_deferred("_apply_fog")


func _apply_fog() -> void:
	_fog_dirty = false
	_rebuild_instanced_all()
	update_fog()


# ------------------------------------------------------------------------------------------- state updates

func remove_object(id: int) -> void:
	if not objs.has(id):
		return
	var e: Dictionary = objs[id]
	if e.node:
		var n: Node3D = e.node
		var tw := create_tween()
		tw.tween_property(n, "scale", Vector3(0.01, 0.01, 0.01), 0.35).set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_IN)
		tw.tween_callback(n.queue_free)
	else:
		var z: int = e.level
		instanced[z] = instanced[z].filter(func(x): return int(x[3]) != id)
		_rebuild_instanced(z)
	objs.erase(id)


func change_object(o: Dictionary) -> void:
	var id := int(o.id)
	if objs.has(id):
		var e: Dictionary = objs[id]
		var same_pos := int(e.data.x) == int(o.x) and int(e.data.y) == int(o.y) and int(e.data.get("z", 0)) == int(o.get("z", 0)) and String(e.data.get("def", "")) == String(o.get("def", ""))
		if same_pos:
			e.data = o
			apply_owner(e)
			return
		remove_object(id)
	add_object(o)
	var e2 = objs.get(id)
	if e2 and e2.node == null:
		_rebuild_instanced(int(o.get("z", 0)))
	update_fog()


func set_players(p: Array) -> void:
	players = p
	for id in objs:
		apply_owner(objs[id])


func object_at(z: int, cell: Vector2i) -> Variant:
	for id in objs:
		var e: Dictionary = objs[id]
		if e.level == z and cell in e.cells:
			return e.data
	return null


func _process(delta: float) -> void:
	for a in animated:
		var n: Node3D = a[0]
		if is_instance_valid(n) and n.is_visible_in_tree():
			n.rotate_object_local(a[1], a[2] * delta)
