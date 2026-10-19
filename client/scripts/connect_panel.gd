extends CanvasLayer
## Autoload "ConnectPanel": bridge address for the client without touching main.gd / net.gd.
##  - `--url ws://HOST:PORT` (with or without `--` before it) overrides the address; `--server URL` (main.gd,
##    after `--`) still works as before;
##    without either the last address typed in the panel (user://connect.cfg) is used, else ws://127.0.0.1:8765.
##  - while there is no connection a small "connect to" panel is shown; F2 opens/closes it at any time
##    (e.g. to switch to another bridge), Esc closes it.
## Finds the net node (res://scripts/net.gd) when main.gd adds it and uses its public API: url, status,
## reconnect(url), status_changed. While the address field has focus the camera keys are off (rig.input_enabled).
## Local mode (autoload LocalMode, scripts/local_mode.gd: the client starts game + bridge itself): while it is on,
## the address is LocalMode.url() and this panel does not pop up by itself; its button switches to local mode,
## connecting to a typed address switches local mode off.

const NET_SCRIPT := "res://scripts/net.gd"
const RIG_SCRIPT := "res://scripts/camera_rig.gd"
const CFG := "user://connect.cfg"
const DEFAULT_PORT := 8765
const SHOW_DELAY := 1.0          # seconds without connection before the panel pops up (no flicker on reconnects)

var net: Node = null
var rig: Node = null
var _cli_url := ""               # from --url
var _has_server_arg := false     # --server given: main.gd's address wins over the saved one
var _no_ui := false
var _save_on_online := false     # remember an address typed in the panel once it goes online
var _suppress := false           # closed by the user while offline: don't pop up again until next online
var _not_online_for := 0.0
var _rig_input := true

var panel: PanelContainer
var edit: LineEdit
var status_label: Label


func _ready() -> void:
	layer = 50
	process_mode = Node.PROCESS_MODE_ALWAYS
	# --url is read from the user args (after `--`) and also from the plain command line, so that
	# `HotA3D.exe --url ws://10.10.0.108:8765` works without the `--` separator
	var user := OS.get_cmdline_user_args()
	for a in [OS.get_cmdline_args(), user]:
		for i in a.size():
			var s: String = a[i]
			if s == "--url" and i + 1 < a.size():
				_cli_url = normalize(a[i + 1])
			elif s.begins_with("--url="):
				_cli_url = normalize(s.substr(6))
	for i in user.size():
		var s: String = user[i]
		if s == "--server" or s.begins_with("--server="):
			_has_server_arg = true
		elif s == "--no-ui":
			_no_ui = true
		elif s == "--connect-shot" and i + 1 < user.size():
			_debug_shot.call_deferred(user[i + 1])    # debug: save a frame of the panel after 3 s and quit
	_build()
	get_tree().node_added.connect(_on_node_added)


## "10.10.0.108" -> "ws://10.10.0.108:8765", "host:9000" -> "ws://host:9000"; ws:// and wss:// are kept.
static func normalize(u: String) -> String:
	u = u.strip_edges()
	if u == "":
		return ""
	if not (u.begins_with("ws://") or u.begins_with("wss://")):
		u = "ws://" + u
	var i := u.find("://") + 3
	var rest := u.substr(i)
	var hostport := rest.split("/", true, 1)[0]
	var has_port := ("]:" in hostport) if hostport.begins_with("[") else (":" in hostport)
	if not has_port:
		u = u.substr(0, i) + hostport + ":%d" % DEFAULT_PORT + rest.substr(hostport.length())
	return u


func _saved_url() -> String:
	var cf := ConfigFile.new()
	if cf.load(CFG) == OK:
		return String(cf.get_value("bridge", "url", ""))
	return ""


func _save_url(u: String) -> void:
	var cf := ConfigFile.new()
	cf.load(CFG)
	cf.set_value("bridge", "url", u)
	cf.save(CFG)


# --------------------------------------------------------------------------------------- nodes of main.gd

func _on_node_added(n: Node) -> void:
	var sc: Script = n.get_script()
	if sc == null:
		return
	if sc.resource_path == RIG_SCRIPT and rig == null:
		rig = n
		n.tree_exiting.connect(func(): rig = null)
	elif sc.resource_path == NET_SCRIPT and net == null:
		net = n
		n.tree_exiting.connect(func(): net = null)
		# node_added fires before the node's _ready, i.e. before net.gd's first connect_now()
		var u := _cli_url
		var lm := _local()
		if lm != null and lm.active:
			u = lm.url()
		elif u == "" and not _has_server_arg:
			u = _saved_url()
		if u != "":
			net.url = u
		edit.text = String(net.url)
		net.status_changed.connect(_on_status)


func _on_status(s: String, detail: String) -> void:
	match s:
		"online":
			status_label.text = "Подключено: %s" % net.url
			_suppress = false
			if _save_on_online:
				_save_url(String(net.url))
				_save_on_online = false
			_hide()
		"connecting":
			status_label.text = "Подключение к %s…" % net.url
		_:
			status_label.text = "Нет связи с %s: %s" % [net.url, detail]


func _local() -> Node:
	return get_node_or_null("/root/LocalMode")


## Point the existing net node at a new address and connect right away.
func connect_to(u: String) -> void:
	u = normalize(u)
	if u == "" or net == null:
		return
	var lm := _local()
	if lm != null and lm.active:
		lm.disable()          # a remote address typed: stop the local game/bridge
	edit.text = u
	net.reconnect(u)
	_save_on_online = true
	_not_online_for = 0.0
	edit.release_focus()


# --------------------------------------------------------------------------------------- UI

func _show() -> void:
	if net:
		edit.text = String(net.url)
	panel.visible = true


func _hide() -> void:
	panel.visible = false
	edit.release_focus()


func _build() -> void:
	var root := Control.new()
	root.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.theme = _theme()
	add_child(root)
	panel = PanelContainer.new()
	panel.set_anchors_preset(Control.PRESET_CENTER_TOP)
	panel.grow_horizontal = Control.GROW_DIRECTION_BOTH
	panel.offset_top = 64
	panel.visible = false
	root.add_child(panel)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 6)
	panel.add_child(vb)
	var title := Label.new()
	title.text = "Подключение к мосту HotA (bridge.py)"
	vb.add_child(title)
	var hb := HBoxContainer.new()
	hb.add_theme_constant_override("separation", 8)
	vb.add_child(hb)
	edit = LineEdit.new()
	edit.custom_minimum_size = Vector2(340, 0)
	edit.placeholder_text = "ws://10.10.0.108:8765"
	edit.tooltip_text = "IP машины с игрой (bridge.py --host 0.0.0.0) или ws://127.0.0.1:8765 через SSH-туннель"
	edit.text_submitted.connect(func(t): connect_to(t))
	edit.focus_entered.connect(_on_focus.bind(true))
	edit.focus_exited.connect(_on_focus.bind(false))
	hb.add_child(edit)
	var button := Button.new()
	button.text = "Подключиться"
	button.pressed.connect(func(): connect_to(edit.text))
	hb.add_child(button)
	var local_btn := Button.new()
	local_btn.text = "Игра на этом ПК…"
	local_btn.tooltip_text = "Локальный режим: клиент сам запускает оригинальную игру и мост (F3 — проверка и папка игры)"
	local_btn.pressed.connect(func():
		var lm := _local()
		if lm != null:
			_hide()
			lm.enable())
	hb.add_child(local_btn)
	status_label = Label.new()
	status_label.add_theme_font_size_override("font_size", 14)
	status_label.add_theme_color_override("font_color", Color(0.85, 0.8, 0.68))
	status_label.text = "Нет связи"
	vb.add_child(status_label)
	var hint := Label.new()
	hint.add_theme_font_size_override("font_size", 13)
	hint.add_theme_color_override("font_color", Color(0.7, 0.66, 0.56))
	hint.text = "F2 — открыть/закрыть панель · адрес запоминается · порт по умолчанию %d · F3 — игра на этом ПК" % DEFAULT_PORT
	vb.add_child(hint)


func _on_focus(focused: bool) -> void:
	if rig == null or not ("input_enabled" in rig):
		return
	if focused:
		_rig_input = rig.input_enabled
		rig.input_enabled = false
	else:
		rig.input_enabled = _rig_input


func _theme() -> Theme:
	var th := Theme.new()
	var f := SystemFont.new()
	f.font_names = PackedStringArray(["Georgia", "Palatino Linotype", "Cambria", "Times New Roman", "DejaVu Serif", "serif"])
	th.default_font = f
	th.default_font_size = 17
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.07, 0.055, 0.045, 0.92)
	sb.border_color = Color(0.62, 0.48, 0.24)
	sb.set_border_width_all(2)
	sb.set_corner_radius_all(4)
	sb.set_content_margin_all(10)
	sb.shadow_color = Color(0, 0, 0, 0.45)
	sb.shadow_size = 6
	th.set_stylebox("panel", "PanelContainer", sb)
	var bn := StyleBoxFlat.new()
	bn.bg_color = Color(0.22, 0.15, 0.09, 0.95)
	bn.border_color = Color(0.55, 0.42, 0.2)
	bn.set_border_width_all(1)
	bn.set_corner_radius_all(3)
	bn.set_content_margin_all(5)
	var bh := bn.duplicate()
	bh.bg_color = Color(0.34, 0.24, 0.12, 0.95)
	bh.border_color = Color(0.9, 0.72, 0.35)
	var le := bn.duplicate()
	le.bg_color = Color(0.03, 0.025, 0.02, 0.95)
	th.set_stylebox("normal", "Button", bn)
	th.set_stylebox("hover", "Button", bh)
	th.set_stylebox("pressed", "Button", bh)
	th.set_stylebox("normal", "LineEdit", le)
	th.set_stylebox("focus", "LineEdit", bh)
	th.set_color("font_color", "Button", Color(0.95, 0.88, 0.7))
	th.set_color("font_color", "Label", Color(0.95, 0.9, 0.78))
	th.set_color("font_color", "LineEdit", Color(0.98, 0.94, 0.82))
	return th


func _process(delta: float) -> void:
	if net == null or _no_ui:
		return
	if net.status == "online":
		_not_online_for = 0.0
		return
	_not_online_for += delta
	var lm := _local()
	if lm != null and lm.active:
		return                # LocalMode shows its own panel
	if _not_online_for >= SHOW_DELAY and not panel.visible and not _suppress:
		_show()


func _debug_shot(path: String) -> void:
	await get_tree().create_timer(3.0).timeout
	await RenderingServer.frame_post_draw
	var img := get_viewport().get_texture().get_image()
	if not path.is_absolute_path():
		path = OS.get_executable_path().get_base_dir().path_join(path)
	print("connect-shot ", path, " ", img.save_png(path))
	get_tree().quit()


func _input(ev: InputEvent) -> void:
	if _no_ui or not (ev is InputEventKey) or not ev.pressed or ev.echo:
		return
	if ev.keycode == KEY_F2:
		if panel.visible:
			_hide()
			_suppress = net == null or net.status != "online"
		else:
			_show()
			edit.grab_focus()
			edit.caret_column = edit.text.length()
		get_viewport().set_input_as_handled()
	elif ev.keycode == KEY_ESCAPE and panel.visible and edit.has_focus():
		_hide()
		_suppress = net == null or net.status != "online"
		get_viewport().set_input_as_handled()
