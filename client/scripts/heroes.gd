extends Node3D
## Heroes on the map: model (manifest or rider fallback), owner colours, selection ring, movement animation.

signal move_finished(hero_id: int)

const Defs = preload("res://scripts/defs.gd")
const Procgen = preload("res://scripts/procgen.gd")

var objects_layer      # objects.gd (manifest + glb loader)
var levels: Array = []
var players: Array = []
var heroes := {}       # id -> {data, node, model, moving}
var selected := -1
var ring: MeshInstance3D
var step_time := 0.22
var current_level := 0


func _ready() -> void:
	ring = MeshInstance3D.new()
	var tm := TorusMesh.new()
	tm.inner_radius = 0.36
	tm.outer_radius = 0.46
	tm.rings = 24
	tm.ring_segments = 6
	ring.mesh = tm
	ring.scale = Vector3(1, 0.15, 1)
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = Color(1.0, 0.85, 0.3)
	ring.material_override = m
	ring.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	ring.visible = false
	add_child(ring)


func clear() -> void:
	_park_ring()
	for id in heroes:
		heroes[id].node.queue_free()
	heroes.clear()
	selected = -1
	ring.visible = false


func _cell_pos(x: int, y: int, z: int) -> Vector3:
	var lv = levels[clampi(z, 0, levels.size() - 1)]
	return Vector3(x + 0.5, maxf(lv.height_at(x + 0.5, y + 0.5), -0.15), y + 0.5)


func _dir_yaw(d: int) -> float:
	# engine dir 0..7 = N, NE, E, SE, S, SW, W, NW; model faces +Z (south)
	var v: Vector2 = [Vector2(0, -1), Vector2(1, -1), Vector2(1, 0), Vector2(1, 1), Vector2(0, 1), Vector2(-1, 1), Vector2(-1, 0), Vector2(-1, -1)][clampi(d, 0, 7)]
	return atan2(v.x, v.y)


func _make_node(h: Dictionary) -> Node3D:
	var root := Node3D.new()
	root.name = "hero_%d" % int(h.id)
	var entry := {"flags": []}
	var model: Node3D = null
	if objects_layer:
		var models: Dictionary = objects_layer.manifest.models
		for key in ["hero:%d" % int(h.id), "hero:class:%d" % int(h.get("class", 0)), "hero"]:
			if models.has(key):
				model = objects_layer.load_glb(String(models[key].glb))
				if model:
					var sc := float(models[key].get("scale", 1.0))
					model.scale = Vector3(sc, sc, sc)
					model.rotation.y = deg_to_rad(float(models[key].get("rot_y", 0)))
					objects_layer._collect_player_color(entry, model)
					break
	if model == null:
		var r := Procgen.hero_mesh(int(h.get("class", 0)))
		var mi := MeshInstance3D.new()
		mi.mesh = r[0]
		if int(r[1]) >= 0:
			entry.flags.append([mi, int(r[1])])
		model = mi
		model.scale = Vector3(1.3, 1.3, 1.3)
	model.name = "model"
	root.add_child(model)
	root.set_meta("flags", entry.flags)
	return root


func _apply_color(id: int) -> void:
	var e: Dictionary = heroes[id]
	var col := Defs.owner_color(int(e.data.owner), players)
	var mat := Procgen.flag_material(col)
	for f in e.node.get_meta("flags", []):
		f[0].set_surface_override_material(int(f[1]), mat)


func sync(list: Array, fog_check: Callable) -> void:
	var seen := {}
	for h in list:
		var id := int(h.id)
		seen[id] = true
		if not heroes.has(id):
			var node := _make_node(h)
			add_child(node)
			heroes[id] = {"data": h, "node": node, "moving": false}
			node.position = _cell_pos(int(h.x), int(h.y), int(h.z))
			node.rotation.y = _dir_yaw(int(h.get("dir", 4)))
		var e: Dictionary = heroes[id]
		e.data = h
		if not e.moving:
			e.node.position = _cell_pos(int(h.x), int(h.y), int(h.z))
			e.node.rotation.y = _dir_yaw(int(h.get("dir", 4)))
		_apply_color(id)
	for id in heroes.keys():
		if not seen.has(id):
			if ring.get_parent() == heroes[id].node:
				_park_ring()
			heroes[id].node.queue_free()
			heroes.erase(id)
			if selected == id:
				selected = -1
	update_visibility(fog_check)


func update_visibility(fog_check: Callable) -> void:
	for id in heroes:
		var e: Dictionary = heroes[id]
		var h: Dictionary = e.data
		var vis := bool(h.get("visible", true)) and int(h.z) == current_level
		if vis and not e.moving:
			vis = fog_check.call(int(h.x), int(h.y), int(h.z))
		e.node.visible = vis or (e.moving and int(h.z) == current_level)
	_update_ring()


func select(id: int) -> void:
	selected = id
	_update_ring()


func _park_ring() -> void:
	if is_instance_valid(ring) and ring.get_parent() != self:
		ring.get_parent().remove_child(ring)
		add_child(ring)
	ring.visible = false


func _update_ring() -> void:
	if selected >= 0 and heroes.has(selected) and is_instance_valid(heroes[selected].node) and heroes[selected].node.visible:
		var n: Node3D = heroes[selected].node
		if ring.get_parent() != n:
			ring.get_parent().remove_child(ring)
			n.add_child(ring)
		ring.position = Vector3(0, 0.04, 0)
		var col := Defs.owner_color(int(heroes[selected].data.owner), players)
		(ring.material_override as StandardMaterial3D).albedo_color = col.lerp(Color(1, 0.9, 0.4), 0.5)
		ring.visible = true
	else:
		ring.visible = false


func is_moving() -> bool:
	for id in heroes:
		if heroes[id].moving:
			return true
	return false


func hero_pos(id: int) -> Vector3:
	return heroes[id].node.global_position if heroes.has(id) else Vector3.ZERO


## Animate along engine cells [[x,y,z],...]; level changes (gates) teleport.
func animate(id: int, cells: Array) -> void:
	if not heroes.has(id) or cells.is_empty():
		move_finished.emit(id)
		return
	var e: Dictionary = heroes[id]
	var n: Node3D = e.node
	e.moving = true
	if int(e.data.z) == current_level:
		n.visible = true
	var tw := create_tween()
	var prev := n.position
	var prev_z := int(e.data.z)
	var bob: Node3D = n.get_node("model")
	for c in cells:
		var z := int(c[2])
		var p := _cell_pos(int(c[0]), int(c[1]), z)
		if z != prev_z:
			tw.tween_callback(func(): n.position = p; n.visible = z == current_level)
			prev_z = z
			prev = p
			continue
		var dir := p - prev
		var dur := step_time * (1.41 if absf(dir.x) > 0.1 and absf(dir.z) > 0.1 else 1.0)
		var yaw := atan2(dir.x, dir.z)
		tw.tween_callback(func(): n.rotation.y = yaw)
		tw.tween_property(n, "position", p, dur)
		tw.parallel().tween_property(bob, "position:y", 0.05, dur * 0.5).set_trans(Tween.TRANS_SINE)
		tw.parallel().tween_property(bob, "position:y", 0.0, dur * 0.5).set_trans(Tween.TRANS_SINE).set_delay(dur * 0.5)
		prev = p
	tw.tween_callback(func():
		e.moving = false
		move_finished.emit(id))
