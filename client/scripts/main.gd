extends Node3D
## HotA 3D client: connects to the bridge (PROTOCOL.md), renders the world, sends player commands.
## Command line (after `--`): see README.md.

const Defs = preload("res://scripts/defs.gd")
const Net = preload("res://scripts/net.gd")
const TerrainLevel = preload("res://scripts/terrain.gd")
const Objects = preload("res://scripts/objects.gd")
const Heroes = preload("res://scripts/heroes.gd")
const PathView = preload("res://scripts/path_view.gd")
const CameraRig = preload("res://scripts/camera_rig.gd")
const Hud = preload("res://scripts/hud.gd")
const EngineWindow = preload("res://scripts/engine_window.gd")

var args := {}
var net: Node
var hud: Control
var overlay: Control              # engine window passthrough (engine_window.gd)
var server_caps: Array = []       # hello caps of the bridge ("windows": engine windows are streamed)
var phase := ""                   # bridge `phase`: "menu" (the game's menus, streamed fullscreen) | "adventure" | "exited"
var _menu_demo_started := false
var rig: Node3D
var world: Node3D
var objects: Node3D
var heroes: Node3D
var path_view: Node3D
var levels: Array = []
var sun: DirectionalLight3D
var env: Environment
var shared := {}

var snap := {}
var me := 0
var current := 0
var level := 0
var selected := -1
var players: Array = []
var hero_list: Array = []
var town_list: Array = []
var world_ready := false
var world_time := 0.0
var hover_cell := Vector2i(-1, -1)
var pending_path := {}
var _snapshot_wait := 0.0
var _got_snapshot := false
var _shots: Array = []      # [time, path]
var _exit_after := -1.0
var _move_pending := false
var _auto_events := {}
var date := {}                    # current game date (snapshot / state / turn)
var _auto_probe := false          # --auto is trying cells for a long path: refusals are not logged
var _auto_obj := -1               # --auto: id of the object the last hero walked to
var mirror := false               # bridge --visible (hello caps "mirror"): the original game window is open too and
                                  # the player may play there; its changes arrive as state why "external"
var _external_moves := {}         # hero id -> true: the running move animation was made in the game window


func _ready() -> void:
	_parse_args()
	if args.has("size") or args.has("shot") or args.has("shots") or args.has("auto"):
		var sz := String(args.get("size", "1600x900")).split("x")
		get_tree().root.content_scale_mode = Window.CONTENT_SCALE_MODE_VIEWPORT
		get_tree().root.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_IGNORE
		get_tree().root.content_scale_size = Vector2i(int(sz[0]), int(sz[1]))
	_setup_atmosphere()
	_load_shared()
	world = Node3D.new()
	world.name = "World"
	add_child(world)
	objects = Objects.new()
	objects.name = "Objects"
	add_child(objects)
	objects.load_manifest()
	heroes = Heroes.new()
	heroes.name = "Heroes"
	heroes.objects_layer = objects
	add_child(heroes)
	heroes.move_finished.connect(_on_move_finished)
	path_view = PathView.new()
	path_view.name = "Path"
	add_child(path_view)
	rig = CameraRig.new()
	rig.name = "CameraRig"
	add_child(rig)
	var ui := CanvasLayer.new()
	add_child(ui)
	hud = Hud.new()
	ui.add_child(hud)
	if args.has("no-ui"):
		hud.visible = false
	hud.hero_clicked.connect(_on_hero_button)
	hud.town_clicked.connect(_on_town_button)
	hud.end_turn_pressed.connect(_end_turn)
	hud.level_toggled.connect(func(): set_level(1 - level))
	hud.dialog_answered.connect(_on_dialog_answered)
	hud.town_window_requested.connect(open_town_window)
	hud.hero_window_requested.connect(open_hero_window)
	hud.system_menu_requested.connect(open_system_menu)
	# engine windows over everything of the client (the connect panel autoload stays above, layer 50)
	var wl := CanvasLayer.new()
	wl.layer = 10
	add_child(wl)
	overlay = EngineWindow.new()
	overlay.name = "EngineWindow"
	overlay.theme = hud.theme
	overlay.live_world = args.has("overlay-live")
	wl.add_child(overlay)
	overlay.input_msg.connect(func(m): net.send(m))
	overlay.opened.connect(_on_window_opened)
	overlay.closed.connect(_on_window_closed)
	net = Net.new()
	net.url = String(args.get("server", "ws://127.0.0.1:8765"))
	add_child(net)
	net.message.connect(_on_message)
	net.status_changed.connect(_on_status)
	if args.has("shot"):
		_shots.append([float(args.get("after", "3")), String(args.shot)])
	if args.has("shots"):
		for part in String(args.shots).split(","):
			var kv := part.split(":", true, 1)
			if kv.size() == 2:
				_shots.append([float(kv[0]), kv[1]])
	if args.has("exit-after"):
		_exit_after = float(args["exit-after"])
	elif not _shots.is_empty() and not args.has("auto") and not args.has("keep"):
		var mx := 0.0
		for s in _shots:
			mx = maxf(mx, float(s[0]))
		_exit_after = mx + 0.5
	print("client: renderer %s, server %s" % [RenderingServer.get_video_adapter_name(), net.url])
	get_tree().root.size_changed.connect(func(): print("viewport ", get_viewport().get_visible_rect().size, " hud ", hud.size))
	await get_tree().process_frame
	print("viewport ", get_viewport().get_visible_rect().size, " hud ", hud.size)


func _parse_args() -> void:
	var a := OS.get_cmdline_user_args()
	var i := 0
	while i < a.size():
		var s: String = a[i]
		if s.begins_with("--"):
			var key := s.substr(2)
			if "=" in key:
				var kv := key.split("=", true, 1)
				args[kv[0]] = kv[1]
			elif i + 1 < a.size() and not a[i + 1].begins_with("--"):
				args[key] = a[i + 1]
				i += 1
			else:
				args[key] = "1"
		i += 1


# ============================================================================================= setup

func _setup_atmosphere() -> void:
	var we := WorldEnvironment.new()
	env = Environment.new()
	env.background_mode = Environment.BG_SKY
	var sky := Sky.new()
	var psm := ProceduralSkyMaterial.new()
	psm.sky_top_color = Color(0.3, 0.42, 0.62)
	psm.sky_horizon_color = Color(0.86, 0.74, 0.62)
	psm.sky_curve = 0.12
	psm.ground_horizon_color = Color(0.72, 0.64, 0.56)
	psm.ground_bottom_color = Color(0.2, 0.2, 0.24)
	psm.sun_angle_max = 25.0
	psm.sun_curve = 0.1
	sky.sky_material = psm
	env.sky = sky
	env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	env.ambient_light_energy = 0.75
	env.ambient_light_sky_contribution = 0.85
	env.reflected_light_source = Environment.REFLECTION_SOURCE_SKY
	env.tonemap_mode = Environment.TONE_MAPPER_ACES
	env.tonemap_exposure = 0.95
	env.tonemap_white = 6.0
	env.ssao_enabled = not args.has("fast")
	env.ssao_radius = 0.8
	env.ssao_intensity = 1.6
	env.ssao_power = 1.4
	env.glow_enabled = true
	env.glow_intensity = 0.55
	env.glow_bloom = 0.04
	env.glow_hdr_threshold = 1.1
	env.fog_enabled = true
	env.fog_light_color = Color(0.72, 0.7, 0.72)
	env.fog_light_energy = 0.9
	env.fog_sun_scatter = 0.25
	env.fog_density = 0.0045
	env.fog_aerial_perspective = 0.35
	env.fog_sky_affect = 0.4
	env.adjustment_enabled = true
	env.adjustment_saturation = 1.08
	env.adjustment_contrast = 1.06
	we.environment = env
	add_child(we)
	sun = DirectionalLight3D.new()
	sun.name = "Sun"
	sun.light_color = Color(1.0, 0.86, 0.68)
	sun.light_energy = 1.55
	sun.shadow_enabled = true
	sun.directional_shadow_mode = DirectionalLight3D.SHADOW_PARALLEL_4_SPLITS
	sun.directional_shadow_max_distance = 70.0
	sun.shadow_blur = 1.2
	sun.shadow_bias = 0.04
	sun.shadow_normal_bias = 1.2
	add_child(sun)
	_set_sun(float(args.get("sun-az", "-62")), float(args.get("sun-el", "34")))
	var fill := DirectionalLight3D.new()
	fill.name = "Fill"
	fill.light_color = Color(0.55, 0.65, 0.9)
	fill.light_energy = 0.18
	fill.shadow_enabled = false
	fill.rotation = Vector3(deg_to_rad(-50), deg_to_rad(120), 0)
	add_child(fill)


## az: degrees, 0 = sun in the south (behind the camera), -90 = west; el: elevation degrees
func _set_sun(az: float, el: float) -> void:
	var a := deg_to_rad(az)
	var e := deg_to_rad(el)
	var to_sun := Vector3(sin(a) * cos(e), sin(e), cos(a) * cos(e))
	sun.global_transform = Transform3D(Basis.looking_at(-to_sun, Vector3.UP), Vector3.ZERO)


func _load_shared() -> void:
	shared.terrain_shader = load("res://shaders/terrain.gdshader")
	shared.water_shader = load("res://shaders/water.gdshader")
	shared.cloud_shader = load("res://shaders/clouds.gdshader")
	var nz := FastNoiseLite.new()
	nz.noise_type = FastNoiseLite.TYPE_SIMPLEX_SMOOTH
	nz.frequency = 0.012
	nz.fractal_octaves = 5
	nz.seed = 11
	var img := nz.get_seamless_image(512, 512, false, false, 0.1, true)
	img.convert(Image.FORMAT_RGBA8)
	img.generate_mipmaps()
	shared.noise_tex = ImageTexture.create_from_image(img)
	var nimg := nz.get_seamless_image(512, 512, false, false, 0.1, true)
	nimg.convert(Image.FORMAT_RGBA8)
	nimg.bump_map_to_normal_map(6.0)
	nimg.generate_mipmaps()
	shared.noise_nrm = ImageTexture.create_from_image(nimg)
	var cn := FastNoiseLite.new()
	cn.noise_type = FastNoiseLite.TYPE_CELLULAR
	cn.cellular_return_type = FastNoiseLite.RETURN_DISTANCE2_SUB
	cn.frequency = 0.03
	cn.fractal_type = FastNoiseLite.FRACTAL_NONE
	cn.seed = 5
	var cimg := cn.get_seamless_image(512, 512, false, false, 0.1, true)
	cimg.convert(Image.FORMAT_RGBA8)
	cimg.generate_mipmaps()
	shared.crack_tex = ImageTexture.create_from_image(cimg)
	_load_terrain_textures()
	preload("res://scripts/procgen.gd").set_detail(shared.noise_tex, shared.crack_tex)


func _load_terrain_textures() -> void:
	var images: Array[Image] = []
	var mask := 0
	for t in Defs.TERRAIN_NAMES.size():
		var img: Image = null
		for ext in ["jpg", "png", "jpeg"]:
			var p := "res://assets/terrain/%s_albedo.%s" % [Defs.TERRAIN_NAMES[t], ext]
			if FileAccess.file_exists(p):
				img = Image.load_from_file(ProjectSettings.globalize_path(p))
				if img:
					break
		if img:
			mask |= 1 << t
			img.convert(Image.FORMAT_RGBA8)
			img.resize(1024, 1024, Image.INTERPOLATE_LANCZOS)
		else:
			img = Image.create(1024, 1024, false, Image.FORMAT_RGBA8)
			img.fill(Color(0.5, 0.5, 0.5))
		img.generate_mipmaps()
		images.append(img)
	if mask != 0:
		var arr := Texture2DArray.new()
		arr.create_from_images(images)
		shared.tex_array = arr
		shared.tex_mask = mask
		print("terrain textures: mask %x" % mask)


# ============================================================================================= world

func build_world(s: Dictionary) -> void:
	var t0 := Time.get_ticks_msec()
	snap = s
	for c in world.get_children():
		c.queue_free()
	levels.clear()
	var map: Dictionary = s.map
	var nlev := int(map.get("levels", 1))
	me = int(s.get("me", 0))
	current = int(s.get("current", 0))
	players = s.get("players", [])
	for z in nlev:
		net.keepalive()
		var lv := TerrainLevel.new()
		lv.name = "Level%d" % z
		world.add_child(lv)
		lv.setup(map, s.get("fog", []), z, s.get("objects", []), shared)
		levels.append(lv)
	objects.keepalive = net.keepalive
	objects.build(s, levels)
	heroes.levels = levels
	heroes.players = players
	heroes.clear()
	path_view.levels = levels
	path_view.clear()
	hero_list = s.get("heroes", [])
	town_list = s.get("towns", [])
	heroes.sync(hero_list, _fog_open)
	rig.bounds = Rect2(0, 0, int(map.size), int(map.size))
	var first := true if not world_ready else false
	world_ready = true
	set_level(int(args.get("level", "0")) if first else level)
	_refresh_hud()
	date = s.get("date", {})
	hud.set_date(date)
	if first:
		_initial_camera()
	print("world built in %d ms" % (Time.get_ticks_msec() - t0))
	hud.log_event("Мир загружен: карта %d×%d, объектов %d" % [int(map.size), int(map.size), s.get("objects", []).size()], Color(0.7, 0.85, 1.0))


func _initial_camera() -> void:
	rig.set_view(float(args.get("yaw", "0")), float(args.get("pitch", "50")), float(args.get("dist", "17")))
	var focus := Vector3(rig.bounds.size.x * 0.5, 0, rig.bounds.size.y * 0.5)
	var mine := _my_heroes()
	var eng_sel := int(snap.get("selected", -1))    # the hero selected in the engine (bridge snapshot)
	if args.has("hero"):
		select_hero(int(args.hero), false)
	elif eng_sel >= 0 and int(_hero_data(eng_sel).get("owner", -1)) == me:
		select_hero(eng_sel, false)
	elif not mine.is_empty():
		select_hero(int(mine[0].id), false)
	if selected >= 0:
		focus = heroes.hero_pos(selected)
	if args.has("cam"):
		var c := String(args.cam).split(",")
		focus = Vector3(float(c[0]) + 0.5, 0, float(c[1]) + 0.5)
	rig.focus(focus, true)


func _fog_open(x: int, y: int, z: int) -> bool:
	return z < levels.size() and levels[z].is_open(x, y)


func set_level(z: int) -> void:
	level = clampi(z, 0, maxi(levels.size() - 1, 0))
	for i in levels.size():
		levels[i].visible = i == level
	objects.set_level_visible(level)
	heroes.current_level = level
	heroes.update_visibility(_fog_open)
	path_view.set_level(level)
	hud.set_level(level)
	var under := level > 0
	env.background_mode = Environment.BG_COLOR if under else Environment.BG_SKY
	env.background_color = Color(0.02, 0.02, 0.03)
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR if under else Environment.AMBIENT_SOURCE_SKY
	env.ambient_light_color = Color(0.45, 0.38, 0.35)
	env.ambient_light_energy = 0.9 if under else 0.75
	env.fog_light_color = Color(0.05, 0.04, 0.05) if under else Color(0.72, 0.7, 0.72)
	env.fog_density = 0.012 if under else 0.0045
	sun.light_energy = 1.15 if under else 1.55
	sun.light_color = Color(0.85, 0.75, 0.75) if under else Color(1.0, 0.86, 0.68)


func _my_heroes() -> Array:
	return hero_list.filter(func(h): return int(h.owner) == me)


func _hero_data(id: int) -> Dictionary:
	for h in hero_list:
		if int(h.id) == id:
			return h
	return {}


func _player(id: int) -> Dictionary:
	for p in players:
		if int(p.id) == id:
			return p
	return {}


func _refresh_hud() -> void:
	var p := _player(me)
	hud.set_resources(p.get("res", [0, 0, 0, 0, 0, 0, 0]))
	hud.set_heroes(hero_list, me, selected)
	hud.set_towns(town_list, me)
	var cp := _player(current)
	hud.set_turn(current, me, String(cp.get("color", "?")))


# ============================================================================================= protocol

func _on_status(status: String, detail: String) -> void:
	hud.set_connection(status, detail)
	if status == "online":
		_got_snapshot = false
		_snapshot_wait = 1.5
		hud.log_event("Подключено к %s" % net.url, Color(0.6, 0.9, 0.6))
	elif status == "offline":
		_move_pending = false
		overlay.reset()       # the engine window stays open in the game; window_open comes again on reconnect
		if phase == "menu":
			phase = ""        # the bridge sends `phase` again on reconnect


func _on_message(m: Dictionary) -> void:
	if args.has("auto-report"):
		_rep_msg(m)
	match String(m.get("t", "")):
		"hello":
			_snapshot_wait = 4.0
			hud.log_event("Мост: %s, %s, протокол %s" % [m.get("source", "?"), m.get("game", "?"), str(m.get("proto", "?"))], Color(0.7, 0.85, 1.0))
			server_caps = m.get("caps", []) if m.get("caps") is Array else []
			if "windows" in server_caps:
				# we show engine windows: the bridge keeps town/hero/market/... screens open and streams them
				net.send({"t": "hello", "client": "godot", "caps": ["windows"]})
			hud.set_windows_enabled("windows" in server_caps)
			mirror = "mirror" in server_caps
			if mirror:
				hud.log_event("Окно оригинальной игры тоже открыто: играть можно и там, клиент повторяет", Color(0.7, 0.85, 1.0))
		"window_open":
			overlay.open_window(m, _window_title(m))
			hud.log_event("Окно игры: %s" % _window_title(m), Color(0.75, 0.8, 0.95))
			_auto_events["window_open"] = true
		"window_frame":
			overlay.show_frame(m)
			_auto_events["window_frame"] = int(m.get("seq", 0))
		"window_close":
			overlay.close_window(m)
			_auto_events["window_close"] = true
		"phase":
			_on_phase(m)
		"snapshot":
			_got_snapshot = true
			if phase == "menu":
				overlay.close_screen()
				phase = "adventure"
			if String(m.get("why", "")) in ["adventure", "player"]:
				world_ready = false     # another map / another hot-seat player: camera to that player's hero
				selected = -1
			build_world(m)
			_auto_events["snapshot"] = int(_auto_events.get("snapshot", 0)) + 1
		"state":
			_apply_state(m)
		"path":
			_on_path(m)
		"event":
			_on_event(m)
		"dialog":
			if m.has("auto"):
				# nobody answered in time: the engine already used the default
				hud.log_event("Диалог (ответ по умолчанию %s): %s" % [str(m.auto), Hud.plain(String(m.get("text", "")))], Color(0.75, 0.75, 0.6))
			else:
				hud.queue_dialog(m)
				_auto_events["dialog"] = true
				if args.has("auto"):
					_auto_answer(m)
		"cmd_done":
			_move_pending = false
			_auto_events["done_%s" % String(m.get("cmd", ""))] = true
		"debug":
			pass
		"error":
			var re: Dictionary = m.get("re", {}) if m.get("re") is Dictionary else {}
			hud.log_event("Ошибка: %s%s" % [m.get("msg", "?"), (" (%s)" % re.get("cmd", re.get("t", ""))) if not re.is_empty() else ""], Color(1.0, 0.45, 0.4))
			_move_pending = false
			_auto_events["error"] = true
		_:
			hud.log_event("Неизвестное сообщение: %s" % m.get("t", "?"), Color(0.8, 0.8, 0.5))


func _apply_state(m: Dictionary) -> void:
	if not world_ready:
		return
	for id in m.get("objects_removed", []):
		objects.remove_object(int(id))
	for o in m.get("objects_changed", []):
		objects.change_object(o)
	var fo: Array = m.get("fog_open", [])
	if not fo.is_empty():
		for lv in levels:
			lv.open_fog(fo)
		objects.fog_changed()
	if m.has("players"):
		var old: Array = _player(me).get("res", [])
		players = m.players
		objects.set_players(players)
		heroes.players = players
		var nw: Array = _player(me).get("res", [])
		if old.size() == nw.size():
			var parts := []
			for i in nw.size():
				var d := int(nw[i]) - int(old[i])
				if d != 0:
					parts.append("%s%d %s" % ["+" if d > 0 else "", d, Defs.RES_RU[i].to_lower()])
			if not parts.is_empty():
				hud.log_event("Ресурсы: " + ", ".join(parts), Color(1.0, 0.85, 0.4))
	if m.has("heroes"):
		hero_list = m.heroes
		heroes.sync(hero_list, _fog_open)
	if m.has("towns"):
		town_list = m.towns
	if m.has("date"):
		date = m.date
		hud.set_date(m.date)
	if m.has("current"):
		current = int(m.current)
	# the player selected another hero in the original game window (visible mode): follow quietly, camera stays
	if String(m.get("why", "")) == "external" and m.has("selected"):
		var sel := int(m.selected)
		if sel >= 0 and sel != selected and int(_hero_data(sel).get("owner", -1)) == me:
			select_hero(sel, false, false)
	_refresh_hud()


func _on_path(m: Dictionary) -> void:
	if int(m.get("hero", -1)) != selected:
		return
	var h := _hero_data(selected)
	var cells: Array = m.get("cells", [])
	if cells.is_empty():
		path_view.clear()
		if not _auto_probe:
			hud.log_event("Пути нет", Color(1.0, 0.6, 0.4))
		_auto_events["path"] = true      # answered (no path): the auto scenario tries the next cell at once
		return
	path_view.show_path(selected, cells, int(m.get("reachable", 0)), Vector3i(int(h.get("x", 0)), int(h.get("y", 0)), int(h.get("z", 0))))
	path_view.set_level(level)
	_auto_events["path"] = true


func _on_event(m: Dictionary) -> void:
	var kind := String(m.get("kind", ""))
	match kind:
		"hero_move":
			var id := int(m.get("hero", -1))
			path_view.clear()
			hud.hold_dialogs = true
			var cells: Array = m.get("cells", [])
			var external := bool(m.get("external", false))   # moved in the original game window
			if external:
				_external_moves[id] = true
			elif id == selected:
				rig.follow = func(): return heroes.hero_pos(id)
			heroes.animate(id, cells)
			var h := _hero_data(id)
			if int(h.get("owner", -1)) != me:
				hud.log_event("%s идёт (%d кл.)" % [h.get("name", "?"), cells.size()], Color(0.75, 0.75, 0.85))
			elif external:
				hud.log_event("%s идёт в окне игры (%d кл.)" % [h.get("name", "?"), cells.size()], Color(0.75, 0.8, 0.9))
		"battle":
			var who := _hero_data(int(m.get("attacker", -1)))
			var def: Dictionary = m.get("defender", {})
			var dtext := ""
			match String(def.get("type", "")):
				"monster":
					dtext = "монстров (%d)" % _army_count(def.get("army", []))
				"hero":
					dtext = String(_hero_data(int(def.get("id", -1))).get("name", "героя #%s" % str(def.get("id", "?"))))
				_:
					dtext = "города #%s" % str(def.get("id", "?"))
			var won := String(m.get("winner", "")) == "attacker"
			var losses: Dictionary = m.get("losses", {}) if m.get("losses") is Dictionary else {}
			hud.log_event("Битва: %s против %s — %s, потери %d / %d" % [who.get("name", "?"), dtext, "победа" if won else "поражение",
				_army_count(losses.get("attacker", [])), _army_count(losses.get("defender", []))],
				Color(1.0, 0.55, 0.35) if not won else Color(0.95, 0.8, 0.45))
		"turn":
			current = int(m.get("current", current))
			if m.get("date") is Dictionary:
				date = m.date
				hud.set_date(m.date)
			var p := _player(current)
			hud.log_event("Ход игрока: %s" % p.get("color", str(current)), Color(0.75, 0.8, 1.0))
			_refresh_hud()
			_auto_events["turn_%d" % current] = true
		"dialog_closed":
			hud.close_dialog(int(m.get("id", -1)))
			match String(m.get("by", "")):
				"native":      # visible mode: answered in the original game window
					hud.log_event("Диалог: ответ дан в окне игры (%s)" % str(m.get("answer", "?")), Color(0.75, 0.8, 0.9))
				"client":
					pass
				_:
					hud.log_event("Диалог закрыт движком (ответ %s)" % str(m.get("answer", "?")), Color(0.75, 0.75, 0.6))
		"engine_message":
			hud.log_event("Сообщение игры: %s %s" % [m.get("caption", ""), m.get("text", "")], Color(1.0, 0.6, 0.45))
		"window":
			hud.log_event("Окно движка «%s»: %s" % [m.get("name", "?"), m.get("action", "?")], Color(0.7, 0.7, 0.7))
		_:
			hud.log_event("Событие %s" % kind, Color(0.7, 0.7, 0.7))


## Creatures in an army / losses list ([[creature, count], ..., -1 for empty slots]).
static func _army_count(army) -> int:
	var n := 0
	if army is Array:
		for sl in army:
			if sl is Array and sl.size() >= 2:
				n += int(sl[1])
	return n


func _on_move_finished(id: int) -> void:
	heroes.sync(hero_list, _fog_open)
	objects.update_fog()
	if _external_moves.has(id):        # made in the game window: the camera stays where the player left it
		_external_moves.erase(id)
		if id == selected:
			var hx := _hero_data(id)
			if not hx.is_empty() and int(hx.z) != level:
				set_level(int(hx.z))
		hud.release_dialogs()
		_auto_events["moved_%d" % id] = true
		return
	if id == selected:
		var h := _hero_data(id)
		if not h.is_empty() and int(h.z) != level:
			set_level(int(h.z))
		rig.follow = Callable()
		rig.focus(heroes.hero_pos(id))
	_move_pending = false
	hud.release_dialogs()
	_auto_events["moved_%d" % id] = true


func _on_dialog_answered(id: int, answer: int) -> void:
	net.send({"t": "dialog_answer", "id": id, "answer": answer})


# ============================================================================================= engine windows

func _window_title(m: Dictionary) -> String:
	var t := EngineWindow.title_for(String(m.get("name", "")))
	if m.has("town"):
		for tw in town_list:
			if int(tw.id) == int(m.town):
				return "%s «%s»" % [t, tw.name]
	if String(m.get("name", "")) == "heroScreen" and selected >= 0:
		var h := _hero_data(selected)
		if not h.is_empty():
			return "%s: %s" % [t, h.name]
	return t


func open_town_window(id: int) -> void:
	if not ("windows" in server_caps) or overlay.is_open():
		return
	net.send({"t": "cmd", "cmd": "open_town", "town": id})


func open_hero_window(id: int) -> void:
	if not ("windows" in server_caps) or overlay.is_open():
		return
	if id != selected:
		select_hero(id)
	net.send({"t": "cmd", "cmd": "open_hero", "hero": id})


## The engine's system options (load / save / restart / main menu / quit / settings) as a passthrough window; leaving
## the map from there brings `phase menu` (the client then shows the game's menus fullscreen).
func open_system_menu() -> void:
	if not ("windows" in server_caps) or overlay.is_open() or overlay.screen or not world_ready:
		return
	if current != me:
		hud.log_event("Меню игры — в свой ход", Color(0.8, 0.8, 0.6))
		return
	net.send({"t": "cmd", "cmd": "system_menu"})
	_auto_events.erase("done_system_menu")


# ============================================================================================= phase (PLAN 4.5)

## `phase`: the bridge says whether the game is on an adventure map. menu -> the game's own menus fullscreen (main
## menu, new game / load / campaign screens, high scores ...), mouse and keys go to the engine; adventure -> the 3D
## world (a snapshot of the new map follows); exited -> the game was quit from its menu.
func _on_phase(m: Dictionary) -> void:
	var ph := String(m.get("phase", ""))
	var old := phase
	phase = ph
	_auto_events["phase_" + ph] = int(_auto_events.get("phase_" + ph, 0)) + 1
	match ph:
		"menu":
			_move_pending = false
			path_view.clear()
			world_ready = false
			_got_snapshot = false
			overlay.open_screen()
			if old != "menu":
				var why := String(m.get("why", ""))
				hud.log_event("Меню игры%s" % (" (игра окончена)" if why == "game_over" else ""), Color(0.75, 0.8, 0.95))
			if args.has("menu-demo") and not _menu_demo_started:
				_menu_demo_started = true
				_run_wdemo(String(args["menu-demo"]))
		"adventure":
			overlay.close_screen()
			_got_snapshot = false
			_snapshot_wait = 6.0      # the snapshot comes by itself right after this message
			hud.log_event("Карта приключений: %s, %d×%d" % [String(m.get("file", "?")), int(m.get("size", 0)), int(m.get("size", 0))], Color(0.7, 0.85, 1.0))
		"exited":
			overlay.close_screen()
			hud.log_event("Игра закрыта из её меню", Color(1.0, 0.8, 0.5))
			_show_exit_message()


func _show_exit_message() -> void:
	var lm := get_node_or_null("/root/LocalMode")
	var local: bool = lm != null and bool(lm.get("active")) and int(lm.get("bridge_pid")) > 0
	if local:
		lm.on_game_quit()
	var lbl := Label.new()
	lbl.text = "Игра закрыта (выход из меню игры)." + ("\nКлиент закроется через 3 секунды." if local else "\nПодключитесь к другому мосту (F2) или закройте клиент.")
	lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	lbl.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	lbl.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	lbl.add_theme_font_size_override("font_size", 26)
	lbl.add_theme_color_override("font_color", Color(1.0, 0.85, 0.55))
	var bg := ColorRect.new()
	bg.color = Color(0, 0, 0, 0.85)
	bg.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	var cl := CanvasLayer.new()
	cl.layer = 20
	add_child(cl)
	cl.add_child(bg)
	cl.add_child(lbl)
	if local or args.has("quit-on-exit"):
		await _wait(3.0)
		_quit()


func _on_window_opened() -> void:
	rig.input_enabled = false
	path_view.clear()
	hud.set_hover("")


func _on_window_closed() -> void:
	rig.input_enabled = true
	if world_ready:            # (not when the menu screen gives way to a new map)
		hud.log_event("Окно игры закрыто", Color(0.7, 0.7, 0.7))


# ============================================================================================= commands

func select_hero(id: int, send := true, focus := true) -> void:
	selected = id
	heroes.select(id)
	path_view.clear()
	var h := _hero_data(id)
	if not h.is_empty() and focus:
		if int(h.z) != level:
			set_level(int(h.z))
		rig.focus(Vector3(int(h.x) + 0.5, 0, int(h.y) + 0.5))
	if send:
		net.send({"t": "cmd", "cmd": "select_hero", "hero": id})
	_refresh_hud()


func request_path(cell: Vector3i) -> void:
	if selected < 0:
		return
	pending_path = {"hero": selected, "to": cell}
	net.send({"t": "path", "hero": selected, "to": [cell.x, cell.y, cell.z]})


func move_to(cell: Vector3i) -> void:
	if selected < 0 or _move_pending or heroes.is_moving():
		return
	_move_pending = true
	net.send({"t": "cmd", "cmd": "move_hero", "hero": selected, "to": [cell.x, cell.y, cell.z]})


func _end_turn() -> void:
	path_view.clear()
	net.send({"t": "cmd", "cmd": "end_turn"})


func _on_hero_button(id: int) -> void:
	if id == selected:
		rig.focus(heroes.hero_pos(id))
	select_hero(id)


func _on_town_button(id: int) -> void:
	for t in town_list:
		if int(t.id) == id:
			if int(t.z) != level:
				set_level(int(t.z))
			rig.focus(Vector3(int(t.x) + 0.5, 0, int(t.y)))


# ============================================================================================= input

func _cell_under_mouse(pos: Vector2) -> Vector2i:
	if levels.is_empty():
		return Vector2i(-1, -1)
	var r: Array = rig.screen_ray(pos)
	var hit = levels[level].ray_pick(r[0], r[1])
	if hit == null:
		return Vector2i(-1, -1)
	var c := Vector2i(floori(hit.x), floori(hit.z))
	var n: int = levels[level].n
	if c.x < 0 or c.y < 0 or c.x >= n or c.y >= n:
		return Vector2i(-1, -1)
	return c


func _update_hover(c: Vector2i) -> void:
	hover_cell = c
	if levels.is_empty():
		return
	levels[level].set_hover(c)
	if c.x < 0:
		hud.set_hover("")
		return
	var lv = levels[level]
	if not lv.is_open(c.x, c.y):
		hud.set_hover("(%d, %d) — не разведано" % [c.x, c.y])
		return
	var t: int = lv.ttype(c.x, c.y)
	var txt := "(%d, %d, %d) %s" % [c.x, c.y, level, Defs.TERRAIN_NAMES[t]]
	var o = objects.object_at(level, c)
	if o != null:
		txt += " · %s %d/%d %s" % [Defs.category(o), int(o.type), int(o.subtype), o.get("def", "")]
	for h in hero_list:
		if int(h.x) == c.x and int(h.y) == c.y and int(h.z) == level:
			txt += " · герой %s" % h.name
	hud.set_hover(txt)


func _unhandled_input(ev: InputEvent) -> void:
	if not world_ready or overlay.is_open():
		return
	if ev is InputEventMouseMotion:
		_update_hover(_cell_under_mouse(ev.position))
	elif ev is InputEventMouseButton and ev.pressed and ev.button_index == MOUSE_BUTTON_LEFT:
		if hud.dialog_open():
			return
		var c := _cell_under_mouse(ev.position)
		if c.x < 0:
			return
		click_cell(c)
	elif ev is InputEventKey and ev.pressed and not ev.echo:
		match ev.keycode:
			KEY_TAB, KEY_PAGEDOWN, KEY_PAGEUP:
				set_level(1 - level)
			KEY_SPACE:
				if selected >= 0:
					rig.focus(heroes.hero_pos(selected))
			KEY_G:
				for lv in levels:
					lv.set_grid(not lv.mat.get_shader_parameter("show_grid"))
			KEY_ESCAPE:
				# as in the game: Esc on the map opens the system options (a shown path is cleared first)
				if not path_view.cells.is_empty():
					path_view.clear()
				elif not hud.dialog_open():
					open_system_menu()
			KEY_ENTER, KEY_KP_ENTER:
				if hud.dialog_open():
					hud.answer_dialog(0)
				else:
					_end_turn()
			KEY_H:
				hud.visible = not hud.visible


func click_cell(c: Vector2i) -> void:
	for h in hero_list:
		if int(h.x) == c.x and int(h.y) == c.y and int(h.z) == level and int(h.owner) == me:
			select_hero(int(h.id))
			return
	if selected < 0 or current != me:
		return
	var cell := Vector3i(c.x, c.y, level)
	if path_view.hero == selected and path_view.target == cell:
		move_to(cell)
	else:
		request_path(cell)


# ============================================================================================= loop, shots, auto

var _fps_t := 0


func _process(delta: float) -> void:
	if args.has("fps-log") and Time.get_ticks_msec() - _fps_t > 10000:
		_fps_t = Time.get_ticks_msec()
		print("fps %.2f frames %d t=%.0fs" % [Engine.get_frames_per_second(), Engine.get_frames_drawn(), _fps_t / 1000.0])
	if net.status == "online" and not _got_snapshot and phase != "menu" and phase != "exited":
		_snapshot_wait -= minf(delta, 0.05)
		if _snapshot_wait <= 0.0:
			_snapshot_wait = 5.0
			net.send({"t": "get_snapshot"})
	if not world_ready:
		return
	if world_time == 0.0:
		print("world ready at frame %d" % Engine.get_frames_drawn())     # tools/e2e.sh cuts the video here
	if world_time == 0.0 and args.has("auto"):
		_run_auto()
	if world_time == 0.0 and args.has("wdemo"):
		_run_wdemo(String(args.wdemo))
	world_time += delta
	for s in _shots.duplicate():
		if world_time >= float(s[0]):
			_shots.erase(s)
			_save_shot(String(s[1]))
	if _exit_after > 0.0 and world_time >= _exit_after:
		_exit_after = -1.0
		await get_tree().create_timer(0.2).timeout
		_quit()


func _quit() -> void:
	# stop talking to the bridge first: tearing down a big world can take a while on a software renderer
	net.set_process(false)
	net.ws.close()
	print("client: quit")
	get_tree().quit()


func _save_shot(path: String) -> void:
	await RenderingServer.frame_post_draw
	var img := get_viewport().get_texture().get_image()
	var p := path if path.is_absolute_path() or path.begins_with("res://") or path.begins_with("user://") else ProjectSettings.globalize_path("res://").path_join(path)
	DirAccess.make_dir_recursive_absolute(p.get_base_dir())
	var err := img.save_jpg(p, 0.88) if p.get_extension().to_lower() in ["jpg", "jpeg"] else img.save_png(p)
	print("shot: %s (%dx%d) err=%d at %.1fs" % [p, img.get_width(), img.get_height(), err, Time.get_ticks_msec() / 1000.0])


func _wait(sec: float) -> void:
	await get_tree().create_timer(sec).timeout


func _wait_flag(key: String, timeout: float) -> bool:
	var t := 0.0
	while not _auto_events.has(key) and t < timeout:
		await get_tree().process_frame
		t += get_process_delta_time()
	return _auto_events.has(key)


func _auto_shot(tag: String) -> void:
	if args.has("auto-shots"):
		await _save_shot("%s_%s.%s" % [String(args["auto-shots"]), tag, String(args.get("auto-ext", "png"))])


# --auto-report FILE: what the engine answered during the scenario (timeline of protocol messages + checks), JSON
var _report := {"timeline": [], "checks": {}, "frames": 0}


func _rep_msg(m: Dictionary) -> void:
	var t := String(m.get("t", ""))
	if t == "window_frame":
		_report.frames += 1
		return
	var e := {"t": t, "at": snappedf(world_time, 0.1), "ms": Time.get_ticks_msec()}
	match t:
		"hello":
			e.merge({"source": m.get("source"), "game": m.get("game"), "caps": m.get("caps", [])})
		"snapshot":
			e.merge({"size": m.map.size, "levels": m.map.levels, "objects": m.objects.size(), "heroes": m.heroes.size(),
				"date": m.date, "current": m.current, "me": m.me})
		"state":
			e.merge({"removed": m.get("objects_removed", []), "changed": m.get("objects_changed", []).size(),
				"fog_open": m.get("fog_open", []).size(), "date": m.get("date"), "current": m.get("current")})
			for p in m.get("players", []):
				if int(p.id) == me:
					e["res"] = p.res
		"path":
			var cells: Array = m.get("cells", [])
			e.merge({"hero": m.get("hero"), "cells": cells.size(), "reachable": m.get("reachable"),
				"to": cells[-1] if not cells.is_empty() else null})
		"event", "dialog", "window_open", "window_close", "cmd_done", "error":
			e.merge(m)
		_:
			return
	_report.timeline.append(e)


func _rep_check(name: String, ok: bool, info = "") -> void:
	if not args.has("auto-report"):
		return
	_report.checks[name] = {"ok": ok, "info": info}
	print("auto check [%s] %s %s" % ["PASS" if ok else "FAIL", name, JSON.stringify(info)])
	_rep_save()


func _rep_save() -> void:
	if not args.has("auto-report"):
		return
	var p := String(args["auto-report"])
	if not p.is_absolute_path():
		p = ProjectSettings.globalize_path("res://").path_join(p)
	DirAccess.make_dir_recursive_absolute(p.get_base_dir())
	var f := FileAccess.open(p, FileAccess.WRITE)
	if f:
		f.store_string(JSON.stringify(_report, "  "))


func _my_res() -> Array:
	return (_player(me).get("res", []) as Array).duplicate()


func _town_data(id: int) -> Dictionary:
	for t in town_list:
		if int(t.id) == id:
			return t
	return {}


## Scripted demo for headless screenshots/videos: select hero, preview path, move, answer dialog, [engine windows],
## end turn. Options (after `--`): --auto-target CATS (object categories to walk to, default
## resource,chest,artifact,campfire; `monster` for a battle), --auto-wdemo SPEC (engine window steps after the
## heroes, the --wdemo syntax), --auto-no-far (no long path), --auto-no-end-turn, --auto-shots PREFIX
## [--auto-ext jpg], --auto-report FILE, --auto-exit SEC.
func _run_auto() -> void:
	hud.log_event("Авто-сценарий", Color(0.6, 0.9, 1.0))
	await _wait(float(args.get("auto-delay", "1.5")))
	await _auto_shot("start")
	_rep_check("snapshot from the engine", not snap.is_empty() and int(snap.get("me", -1)) == me,
		{"objects": snap.get("objects", []).size(), "heroes": hero_list.size(), "date": snap.get("date"), "me": me,
		"caps": server_caps})
	var mine := _my_heroes()
	if mine.is_empty():
		return
	for step in mine.size():
		var h: Dictionary = _hero_data(int(mine[step].id))
		select_hero(int(h.id))
		await _wait(1.2)
		var goal := _auto_target(h)
		if goal.x < 0:
			continue
		_auto_events.erase("path")
		request_path(goal)
		await _wait_flag("path", 5.0)
		var pc: Array = path_view.cells.duplicate()
		var preach: int = path_view.reachable
		await _wait(1.6)
		await _auto_shot("%d_path" % step)
		_auto_events.erase("moved_%d" % int(h.id))
		_auto_events.erase("dialog")
		_auto_events.erase("done_move_hero")
		_auto_events.erase("error")
		var res0 := _my_res()
		var obj = objects.object_at(goal.z, Vector2i(goal.x, goal.y))
		var oid := int(obj.id) if obj != null else -1
		_auto_obj = oid
		click_cell(Vector2i(goal.x, goal.y))
		await _wait_flag("moved_%d" % int(h.id), 40.0)
		await _wait(1.0)
		await _auto_shot("%d_moved" % step)
		while hud.dialog_open() or not hud.dialog_queue.is_empty():
			await _wait(0.5)
		await _wait(1.0)
		var h1 := _hero_data(int(h.id))
		var res1 := _my_res()
		var gained := {}
		for i in mini(res0.size(), res1.size()):
			if int(res1[i]) != int(res0[i]):
				gained[Defs.RES_RU[i]] = int(res1[i]) - int(res0[i])
		_rep_check("hero %d %s: engine path, walk, visit" % [int(h.id), h.get("name", "")],
			not pc.is_empty() and Vector3i(int(h1.get("x", -1)), int(h1.get("y", -1)), int(h1.get("z", -1))) != Vector3i(int(h.x), int(h.y), int(h.z)),
			{"target": [goal.x, goal.y, goal.z], "object": obj.get("def", "") if obj != null else "", "path_cells": pc.size(),
			"reachable_today": preach,
			"from": [h.x, h.y, h.z], "to": [h1.get("x"), h1.get("y"), h1.get("z")], "object_gone": oid >= 0 and not objects.objs.has(oid),
			"res_gained": gained, "exp": [h.get("exp"), h1.get("exp")], "level": [h.get("level"), h1.get("level")]})
		if overlay.is_open() or args.has("auto-no-far"):
			continue
		# long path beyond today's movement (red tail); the engine may refuse some cells, try a few
		_auto_probe = true
		for far in _auto_far_list(_hero_data(int(h.id))):
			_auto_events.erase("path")
			request_path(far)
			await _wait_flag("path", 8.0)
			if not path_view.cells.is_empty():
				_auto_probe = false
				await _wait(1.8)
				await _auto_shot("%d_farpath" % step)
				break
		_auto_probe = false
	if args.has("auto-wdemo"):
		var r0 := _my_res()
		var towns0 := town_list.duplicate(true)
		await _run_wdemo(String(args["auto-wdemo"]))
		await _wait(0.5)
		var r1 := _my_res()
		var spent := {}
		for i in mini(r0.size(), r1.size()):
			if int(r1[i]) != int(r0[i]):
				spent[Defs.RES_RU[i]] = int(r1[i]) - int(r0[i])
		var garrison := {}
		for t0 in towns0:
			var t1 := _town_data(int(t0.id))
			if not t1.is_empty() and str(t1.get("garrison")) != str(t0.get("garrison")):
				garrison[str(t0.id)] = [t0.get("garrison"), t1.get("garrison")]
		var wins: Array = _report.timeline.filter(func(e): return e.t in ["window_open", "window_close"])
		_rep_check("engine windows in the client (passthrough)", not wins.is_empty() and not overlay.is_open(),
			{"windows": wins.map(func(e): return "%s %s" % [e.t, e.get("name", "")]), "frames": _report.frames,
			"resources": spent, "garrison_changed": garrison})
		var bat: Array = _report.timeline.filter(func(e): return e.t == "event" and e.get("kind") == "battle")
		if not bat.is_empty():
			var bi: Dictionary = bat[-1].duplicate()
			bi["target_object"] = _auto_obj
			bi["target_gone"] = _auto_obj >= 0 and not objects.objs.has(_auto_obj)
			_rep_check("battle: engine result", String(bi.get("winner", "")) in ["attacker", "defender"] and
				(bi.target_gone or String(bi.get("winner", "")) == "defender"), bi)
	if not args.has("auto-no-end-turn"):
		var date0 := date.duplicate()
		var nturns: int = _report.timeline.size()
		_auto_events.erase("turn_%d" % me)
		_end_turn()
		await _wait_flag("turn_%d" % me, 90.0)
		await _wait(1.5)
		while hud.dialog_open() or not hud.dialog_queue.is_empty():
			await _wait(0.5)
		await _auto_shot("end_turn")
		var order: Array = _report.timeline.slice(nturns).filter(func(e): return e.t == "event" and e.get("kind") == "turn").map(func(e): return e.get("current"))
		_rep_check("end turn: AI turns in the engine, new day", _auto_events.has("turn_%d" % me) and str(date) != str(date0),
			{"date": [date0, date], "turn_order": order, "current": current})
	hud.log_event("Авто-сценарий завершён", Color(0.6, 0.9, 1.0))
	_rep_save()
	if args.has("auto-exit"):
		await _wait(float(args["auto-exit"]))
		_quit()


## In --auto mode engine dialogs (chest, level-up, "heroes can still move") are answered after a short pause by a
## click on the dialog's first button (gold / yes / OK).
func _auto_answer(m: Dictionary) -> void:
	var id := int(m.get("id", -1))
	var t := 0.0
	while not (hud.dialog_open() and hud.dialog_id == id) and t < 90.0:    # held while the hero walks, or queued
		await _wait(0.25)
		t += 0.25
	if not (hud.dialog_open() and hud.dialog_id == id):
		return
	await _wait(2.0)
	if hud.dialog_open() and hud.dialog_id == id:
		await _auto_shot("dialog_%d" % id)
		var b: Button = hud.dialog_button(0)
		var how := "click"
		if b != null:
			await _push_click(b.get_global_rect().get_center(), MOUSE_BUTTON_LEFT, 0.3)
		if hud.dialog_open() and hud.dialog_id == id:
			how = "direct"
			hud.answer_dialog(0)
		_rep_check("dialog %d answered in the client" % id, true,
			{"style": m.get("style"), "text": Hud.plain(String(m.get("text", ""))).left(160),
			"options": (m.get("options", []) as Array).map(func(o): return o.get("text", "")), "answer": 0, "how": how})


## Real mouse events at a viewport position (GUI buttons, the engine window overlay).
func _push_click(p: Vector2, btn: int, hold: float) -> void:
	var mm := InputEventMouseMotion.new()
	mm.position = p
	mm.global_position = p
	get_viewport().push_input(mm, true)
	await _wait(0.3)
	for pressed in [true, false]:
		var mb := InputEventMouseButton.new()
		mb.button_index = btn
		mb.pressed = pressed
		mb.position = p
		mb.global_position = p
		get_viewport().push_input(mb, true)
		await _wait(hold)


func _auto_target(h: Dictionary) -> Vector3i:
	var best := Vector3i(-1, -1, -1)
	var bd := 1e9
	var hp := Vector2(int(h.x), int(h.y))
	var cats := String(args.get("auto-target", "resource,chest,artifact,campfire")).split(",")
	for id in objects.objs:
		var e: Dictionary = objects.objs[id]
		var o: Dictionary = e.data
		if int(o.z) != int(h.z) or not (e.cat in cats):
			continue
		# the entrance cell (e.g. a wandering monster stands left of its anchor), else the anchor
		var tr: Array = o.trigger if o.get("trigger") is Array and (o.trigger as Array).size() == 3 else [o.x, o.y, o.z]
		var d := hp.distance_to(Vector2(int(tr[0]), int(tr[1])))
		if d >= 2.0 and d < bd and d < 9.0:
			bd = d
			best = Vector3i(int(tr[0]), int(tr[1]), int(tr[2]))
	return best


func _auto_far_list(h: Dictionary) -> Array:
	var out := []
	if h.is_empty():
		return out
	var lv = levels[int(h.z)]
	for r in [14, 11, 9, 7, 5]:
		for k in 16:
			var a := TAU * k / 16.0 + 0.3
			var x := int(h.x) + int(round(cos(a) * r))
			var y := int(h.y) + int(round(sin(a) * r))
			if x < 1 or y < 1 or x >= lv.n - 1 or y >= lv.n - 1:
				continue
			var t: int = lv.ttype(x, y)
			if t == Defs.WATER or t == Defs.ROCK or objects.object_at(int(h.z), Vector2i(x, y)) != null:
				continue
			if not lv.is_open(x, y):
				continue
			out.append(Vector3i(x, y, int(h.z)))
			if out.size() >= 10:
				return out
	return out


## Scripted engine-window demo for headless evidence (`--wdemo "town:2;frame;wait:2;shot:out/pt/a.jpg;click:170,265;..."`).
## Steps: town:ID / hero:ID (the HUD buttons' commands), hudtown:ID / hudhero:ID (a real click on the HUD button),
## open (wait for a window the game opens itself, e.g. a battle), select:HERO, frame (wait for a newer window_frame), wait:SEC,
## click:X,Y / rclick:X,Y (engine coordinates, sent through the overlay as real mouse events at the mapped screen
## position), click_close:X,Y (click until the window closes, up to 4 tries), key:NAME (Godot key name, e.g. Escape),
## close (wait for window_close), shot:PATH, quit;
## debug: fake:SEC (the overlay without a bridge window), fps:SEC (log frame durations).
func _run_wdemo(spec: String) -> void:
	await _wait(float(args.get("wdemo-delay", "1.0")))
	for step in spec.split(";", false):
		var kv := step.strip_edges().split(":", true, 1)
		var op: String = kv[0]
		var arg: String = kv[1] if kv.size() > 1 else ""
		print("wdemo: ", step)
		match op:
			"town":
				_auto_events.erase("window_open")
				open_town_window(int(arg))
				await _wait_flag("window_open", 60.0)
			"hero":
				_auto_events.erase("window_open")
				open_hero_window(int(arg))
				await _wait_flag("window_open", 60.0)
			"hudtown", "hudhero":   # the same through a real click on the window button in the HUD lists
				_auto_events.erase("window_open")
				var b: Button = hud.window_button("town" if op == "hudtown" else "hero", int(arg))
				if b != null and not b.disabled and b.is_visible_in_tree():
					await _push_click(b.get_global_rect().get_center(), MOUSE_BUTTON_LEFT, 0.3)
				var opened: bool = await _wait_flag("window_open", 10.0)
				if not opened:
					print("wdemo: no window after the HUD button, sending the command")
					if op == "hudtown":
						open_town_window(int(arg))
					else:
						open_hero_window(int(arg))
					await _wait_flag("window_open", 60.0)
			"phase":      # wait for the bridge's `phase` NAME (menu / adventure), counted from now on
				var n0 := int(_auto_events.get("phase_" + arg, 0))
				var tp := 0.0
				while int(_auto_events.get("phase_" + arg, 0)) <= n0 and phase != arg and tp < 300.0:
					await get_tree().process_frame
					tp += get_process_delta_time()
			"inphase":    # wait until the current phase is NAME (already or later)
				var ti := 0.0
				while phase != arg and ti < 300.0:
					await get_tree().process_frame
					ti += get_process_delta_time()
			"world":      # wait for the 3D world of the current map, then ARG seconds
				var tw := 0.0
				while not world_ready and tw < 900.0:
					await get_tree().process_frame
					tw += get_process_delta_time()
				await _wait(float(arg) if arg != "" else 1.0)
			"clickuntil":   # clickuntil:PHASE:X,Y — click X,Y every 5 s until the bridge reports PHASE (e.g. the
				# scenario intro, still in the menu phase, until the map runs)
				var pa := arg.split(":", true, 1)
				var cxy := pa[1].split(",")
				var tc := 0.0
				while phase != pa[0] and tc < 300.0:
					await _push_click(overlay.to_screen(Vector2i(int(cxy[0]), int(cxy[1]))), MOUSE_BUTTON_LEFT, 0.3)
					var tk := 0.0
					while phase != pa[0] and tk < 5.0:
						await get_tree().process_frame
						tk += get_process_delta_time()
					tc += 5.0
			"check":      # checks for --auto-report (menu run of tools/e2e.sh)
				match arg:
					"menu":
						_rep_check("menu: the game's main menu fullscreen (engine frames, input)", overlay.screen and phase == "menu" and overlay.frames > 0,
							{"frames": overlay.frames, "frame": [overlay.frame_size.x, overlay.frame_size.y], "phase": phase})
					"world":
						_rep_check("menu: new game -> 3D world of the new map", world_ready and phase == "adventure" and not overlay.screen,
							{"size": snap.get("map", {}).get("size"), "objects": snap.get("objects", []).size(), "me": me, "why": snap.get("why")})
					"sysmenu":
						_rep_check("menu: HUD button -> the engine's system options", overlay.is_open() and overlay.top_name() == "systemOptions",
							{"window": overlay.top_name()})
					"back":
						_rep_check("menu: main menu from the system options -> menu screen again", overlay.screen and phase == "menu",
							{"phase": phase, "frames": overlay.frames})
			"sysmenu":    # the HUD button "Меню игры" (a real click), then wait for the engine window
				_auto_events.erase("window_open")
				var mb: Button = hud.menu_btn
				if mb != null and mb.is_visible_in_tree() and not mb.disabled:
					await _push_click(mb.get_global_rect().get_center(), MOUSE_BUTTON_LEFT, 0.3)
				if not await _wait_flag("window_open", 20.0):
					print("wdemo: no window after the HUD button, sending system_menu")
					open_system_menu()
					await _wait_flag("window_open", 60.0)
			"select":     # select a hero (camera + level follow him), as a click on him in the list
				select_hero(int(arg))
			"open":       # wait for an engine window opened by the game itself (e.g. a battle after a move)
				await _wait_flag("window_open", float(arg) if arg != "" else 60.0)
				_auto_events.erase("window_open")
			"frame":
				var seq := int(_auto_events.get("window_frame", -1))
				var t := 0.0
				while int(_auto_events.get("window_frame", -1)) <= seq and t < 30.0:
					await get_tree().process_frame
					t += get_process_delta_time()
			"wait":
				await _wait(float(arg))
			"click", "rclick":
				var xy := arg.split(",")
				var p: Vector2 = overlay.to_screen(Vector2i(int(xy[0]), int(xy[1])))
				await _push_click(p, MOUSE_BUTTON_LEFT if op == "click" else MOUSE_BUTTON_RIGHT, 0.4 if op == "click" else 1.5)
			"click_close":   # click, wait for the window to close; again after a pause if the engine dropped the click
				# (input during an animation / the enemy's move in battle), up to 4 times
				var xy := arg.split(",")
				for attempt in 4:
					if not overlay.is_open():
						break
					await _push_click(overlay.to_screen(Vector2i(int(xy[0]), int(xy[1]))), MOUSE_BUTTON_LEFT, 0.4)
					var t := 0.0
					while overlay.is_open() and t < 40.0:
						await get_tree().process_frame
						t += get_process_delta_time()
					if overlay.is_open():
						print("wdemo: window still open after click %d" % (attempt + 1))
						await _wait(6.0)
			"key":
				for pressed in [true, false]:
					var k := InputEventKey.new()
					k.keycode = OS.find_keycode_from_string(arg)
					k.physical_keycode = k.keycode
					k.pressed = pressed
					get_viewport().push_input(k, true)
					await _wait(0.2)
			"close":
				var t := 0.0
				while overlay.is_open() and t < 60.0:
					await get_tree().process_frame
					t += get_process_delta_time()
			"shot":
				await _save_shot(arg)
			"fake":       # debug: the overlay alone for SEC seconds (no bridge window), e.g. to time the 3D freeze
				overlay.open_window({"name": "test", "w": 800, "h": 600, "depth": 1}, "test")
				await _wait(float(arg))
				overlay.close_window({"depth": 1})
			"fps":        # debug: log every frame's duration for SEC seconds
				var t0 := Time.get_ticks_msec()
				var last := t0
				while Time.get_ticks_msec() - t0 < int(float(arg) * 1000.0):
					await get_tree().process_frame
					var now := Time.get_ticks_msec()
					print("wdemo frame %d ms" % (now - last))
					last = now
			"quit":
				_quit()
				return
