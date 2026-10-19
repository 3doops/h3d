extends Node3D
## Path preview from the engine's `path` reply: arrows along the cells, green = reachable today, red = later.

var levels: Array = []
var cells: Array = []
var reachable := 0
var hero := -1
var target := Vector3i(-1, -1, -1)
var _mm_ok: MultiMeshInstance3D
var _mm_far: MultiMeshInstance3D
var _dest: MeshInstance3D


func _ready() -> void:
	_mm_ok = _make_mm(Color(0.35, 1.0, 0.35))
	_mm_far = _make_mm(Color(1.0, 0.3, 0.25))
	_dest = MeshInstance3D.new()
	var tm := TorusMesh.new()
	tm.inner_radius = 0.18
	tm.outer_radius = 0.3
	tm.ring_segments = 6
	tm.rings = 20
	_dest.mesh = tm
	_dest.scale = Vector3(1, 0.3, 1)
	_dest.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(_dest)
	clear()


func _arrow_mesh() -> ArrayMesh:
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	st.set_normal(Vector3.UP)
	# chevron pointing +Z, drawn clockwise from above
	var pts := [Vector3(0, 0, 0.22), Vector3(0.16, 0, -0.02), Vector3(0.07, 0, -0.02), Vector3(0.07, 0, -0.2),
		Vector3(-0.07, 0, -0.2), Vector3(-0.07, 0, -0.02), Vector3(-0.16, 0, -0.02)]
	var tris := [[0, 1, 6], [2, 3, 4], [2, 4, 5]]
	for t in tris:
		for i in [0, 2, 1]:
			st.add_vertex(pts[t[i]])
	return st.commit()


func _make_mm(col: Color) -> MultiMeshInstance3D:
	var mm := MultiMesh.new()
	mm.transform_format = MultiMesh.TRANSFORM_3D
	mm.mesh = _arrow_mesh()
	var mi := MultiMeshInstance3D.new()
	mi.multimesh = mm
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = col
	m.cull_mode = BaseMaterial3D.CULL_DISABLED
	m.no_depth_test = false
	mi.material_override = m
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(mi)
	return mi


func clear() -> void:
	cells = []
	reachable = 0
	hero = -1
	target = Vector3i(-1, -1, -1)
	_mm_ok.multimesh.instance_count = 0
	_mm_far.multimesh.instance_count = 0
	_dest.visible = false


func show_path(hero_id: int, path_cells: Array, reach: int, start: Vector3i) -> void:
	hero = hero_id
	cells = path_cells
	reachable = reach
	if cells.is_empty():
		clear()
		return
	var last: Array = cells[cells.size() - 1]
	target = Vector3i(int(last[0]), int(last[1]), int(last[2]))
	var ok := []
	var far := []
	var prev := Vector2(start.x + 0.5, start.y + 0.5)
	for i in cells.size():
		var c: Array = cells[i]
		var p := Vector2(int(c[0]) + 0.5, int(c[1]) + 0.5)
		var lv = levels[clampi(int(c[2]), 0, levels.size() - 1)]
		var h := maxf(lv.height_at(p.x, p.y), -0.2) + 0.06
		var d := (p - prev)
		var yaw := atan2(d.x, d.y)
		var tr := Transform3D(Basis(Vector3.UP, yaw), Vector3(p.x, h, p.y))
		if i < cells.size() - 1:
			if i < reach:
				ok.append(tr)
			else:
				far.append(tr)
		prev = p
	_fill(_mm_ok, ok)
	_fill(_mm_far, far)
	var lv2 = levels[clampi(target.z, 0, levels.size() - 1)]
	_dest.position = Vector3(target.x + 0.5, maxf(lv2.height_at(target.x + 0.5, target.y + 0.5), -0.2) + 0.06, target.y + 0.5)
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = Color(0.35, 1.0, 0.35) if cells.size() <= reach else Color(1.0, 0.3, 0.25)
	_dest.material_override = m
	_dest.visible = true


func _fill(mi: MultiMeshInstance3D, trs: Array) -> void:
	mi.multimesh.instance_count = trs.size()
	for i in trs.size():
		mi.multimesh.set_instance_transform(i, trs[i])


func set_level(z: int) -> void:
	visible = target.z == z or target.z < 0
