extends Node3D
## Orbit camera: target on the map, yaw/pitch/distance. Mouse: right drag orbit, middle drag pan, wheel zoom.
## Keys: WASD/arrows pan, Q/E rotate, R/F pitch, Z/X zoom.

var cam: Camera3D
var target := Vector3(24, 0, 24)
var yaw := 0.0              # 0: looking north (camera south of the target)
var pitch := 50.0           # degrees above the horizon
var distance := 18.0
var bounds := Rect2(0, 0, 48, 48)
var follow: Callable        # optional: returns Vector3 to follow
var _goal_target := Vector3(24, 0, 24)
var _goal_dist := 18.0
var _drag_orbit := false
var _drag_pan := false
var input_enabled := true


func _ready() -> void:
	cam = Camera3D.new()
	cam.fov = 38.0
	cam.near = 0.1
	cam.far = 400.0
	add_child(cam)
	_goal_target = target
	_goal_dist = distance
	_apply()


func focus(p: Vector3, instant := false) -> void:
	_goal_target = Vector3(p.x, maxf(p.y, 0.0), p.z)
	if instant:
		target = _goal_target
		_apply()


func set_view(y: float, p: float, d: float) -> void:
	yaw = y
	pitch = p
	distance = d
	_goal_dist = d
	_apply()


func _apply() -> void:
	var pr := deg_to_rad(pitch)
	var yr := deg_to_rad(yaw)
	var off := Vector3(sin(yr) * cos(pr), sin(pr), cos(yr) * cos(pr)) * distance
	cam.position = target + off
	cam.look_at(target, Vector3.UP)


func _unhandled_input(ev: InputEvent) -> void:
	if not input_enabled:
		return
	if ev is InputEventMouseButton:
		var mb := ev as InputEventMouseButton
		if mb.button_index == MOUSE_BUTTON_WHEEL_UP and mb.pressed:
			_goal_dist = clampf(_goal_dist * 0.88, 4.0, 60.0)
		elif mb.button_index == MOUSE_BUTTON_WHEEL_DOWN and mb.pressed:
			_goal_dist = clampf(_goal_dist / 0.88, 4.0, 60.0)
		elif mb.button_index == MOUSE_BUTTON_RIGHT:
			_drag_orbit = mb.pressed
		elif mb.button_index == MOUSE_BUTTON_MIDDLE:
			_drag_pan = mb.pressed
	elif ev is InputEventMouseMotion:
		var mm := ev as InputEventMouseMotion
		if _drag_orbit:
			yaw -= mm.relative.x * 0.3
			pitch = clampf(pitch + mm.relative.y * 0.2, 20.0, 85.0)
		elif _drag_pan:
			var k := distance * 0.0022
			_pan(Vector2(-mm.relative.x, -mm.relative.y) * k)


func _pan(d: Vector2) -> void:
	var yr := deg_to_rad(yaw)
	var right := Vector3(cos(yr), 0, -sin(yr))
	var fwd := Vector3(-sin(yr), 0, -cos(yr))
	_goal_target += right * d.x - fwd * d.y
	follow = Callable()


func _process(delta: float) -> void:
	if input_enabled:
		var mv := Vector2.ZERO
		if Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT): mv.x -= 1
		if Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT): mv.x += 1
		if Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP): mv.y -= 1
		if Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN): mv.y += 1
		if mv != Vector2.ZERO:
			_pan(mv * distance * 0.9 * delta)
		if Input.is_key_pressed(KEY_Q): yaw += 70.0 * delta
		if Input.is_key_pressed(KEY_E): yaw -= 70.0 * delta
		if Input.is_key_pressed(KEY_R): pitch = clampf(pitch + 40.0 * delta, 20.0, 85.0)
		if Input.is_key_pressed(KEY_F): pitch = clampf(pitch - 40.0 * delta, 20.0, 85.0)
		if Input.is_key_pressed(KEY_Z): _goal_dist = clampf(_goal_dist * (1.0 - delta), 4.0, 60.0)
		if Input.is_key_pressed(KEY_X): _goal_dist = clampf(_goal_dist * (1.0 + delta), 4.0, 60.0)
	if follow.is_valid():
		var p: Vector3 = follow.call()
		_goal_target = Vector3(p.x, maxf(p.y, 0.0), p.z)
	_goal_target.x = clampf(_goal_target.x, bounds.position.x - 2, bounds.end.x + 2)
	_goal_target.z = clampf(_goal_target.z, bounds.position.y - 2, bounds.end.y + 2)
	var k := 1.0 - exp(-delta * 6.0)
	target = target.lerp(_goal_target, k)
	distance = lerpf(distance, _goal_dist, k)
	_apply()


func screen_ray(pos: Vector2) -> Array:
	return [cam.project_ray_origin(pos), cam.project_ray_normal(pos)]
