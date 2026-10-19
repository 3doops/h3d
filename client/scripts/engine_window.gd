extends Control
## Engine window passthrough (PLAN 1.9, PROTOCOL.md "window_*"). An engine window the client does not draw itself
## (town screen, hero screen, meeting, market, tavern, HotA windows, battle screen with `bridge.py --battles manual`)
## stays open in the engine; the bridge streams its frame (`window_frame`, JPEG, engine size 800×600) and this
## overlay shows it over the dimmed world, centred and scaled to fit with the aspect kept. Mouse and keys over the
## frame go back as `window_input` in engine coordinates; the engine closes the window (`window_close`).
##
## Screen mode (PLAN 4.5, `open_screen`): outside the adventure map (the game's main menu, scenario / load /
## campaign screens, high scores, credits) the whole engine screen is shown fullscreen on black, scaled with the
## aspect kept (letterboxed), with the same mouse/key passthrough; the 3D pass is off.
##
## While a window is open the 3D world is frozen: one dimmed, blurred snapshot stays behind the frame and the 3D
## pass is switched off (a software renderer then spends its time on the frame, not on the hidden world);
## `live_world = true` keeps the world rendering under a plain dim instead.

signal input_msg(msg: Dictionary)      # a window_input message to send to the bridge
signal opened
signal closed

const TITLES := {
	"townScreen": "Город", "heroScreen": "Герой", "heroMeeting": "Встреча героев", "recruit": "Найм существ",
	"garrison": "Гарнизон", "tavern": "Таверна", "marketplace": "Рынок", "blackMarket": "Чёрный рынок",
	"university": "Университет", "hillFort": "Форт на холме", "altar": "Алтарь жертвоприношений",
	"townGate": "Городские врата", "dimensionDoor": "Дверь измерений", "spellbook": "Книга заклинаний",
	"viewWorld": "Обзор мира", "splitStack": "Разделение отряда", "adventureOptions": "Параметры",
	"scenarioInfo": "Сведения о сценарии", "kingdomOverview": "Обзор королевства", "combat": "Битва",
	"dialogBox": "Сообщение", "systemOptions": "Системные опции", "combatResults": "Итоги битвы",
}
const MARGIN := 22.0
const TITLE_H := 34.0
const BORDER := 5.0
const MOVE_INTERVAL := 1.0 / 30.0      # mouse moves to the engine at most this often

var live_world := false
var screen := false                    # screen mode: the engine's menus fullscreen (open_screen)
var stack: Array = []                  # window_open messages, outermost first
var frame_size := Vector2i(800, 600)
var frames := 0                        # window_frame messages shown
var last_seq := -1
var title_text := ""

var backdrop: TextureRect
var dim: ColorRect
var box: Panel
var title: Label
var hint: Label
var close_btn: Button
var view: TextureRect
var waiting: Label
var tex: ImageTexture
var _scale := 1.0
var _mouse := Vector2i(-1, -1)         # last position sent to the engine
var _mouse_want := Vector2i(-1, -1)    # newest position over the frame, sent on the next tick
var _move_t := 0.0
var _buttons := {}                     # held mouse buttons ("left"/"right") -> true, released on close
var _frozen := false


func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_STOP
	visible = false
	backdrop = TextureRect.new()
	backdrop.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	backdrop.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	backdrop.stretch_mode = TextureRect.STRETCH_SCALE
	backdrop.texture_filter = CanvasItem.TEXTURE_FILTER_LINEAR
	backdrop.modulate = Color(0.42, 0.4, 0.44)
	backdrop.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(backdrop)
	dim = ColorRect.new()
	dim.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	dim.color = Color(0.03, 0.025, 0.02, 0.35)
	dim.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(dim)
	box = Panel.new()
	box.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.07, 0.055, 0.045, 0.96)
	sb.border_color = Color(0.62, 0.48, 0.24)
	sb.set_border_width_all(2)
	sb.set_corner_radius_all(4)
	sb.shadow_color = Color(0, 0, 0, 0.6)
	sb.shadow_size = 14
	box.add_theme_stylebox_override("panel", sb)
	add_child(box)
	title = Label.new()
	title.add_theme_color_override("font_color", Color(1.0, 0.82, 0.45))
	title.add_theme_font_size_override("font_size", 19)
	title.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	title.mouse_filter = Control.MOUSE_FILTER_IGNORE
	box.add_child(title)
	hint = Label.new()
	hint.add_theme_color_override("font_color", Color(0.78, 0.7, 0.55))
	hint.add_theme_font_size_override("font_size", 14)
	hint.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	hint.text = "окно движка · мышь и клавиши идут в игру"
	hint.mouse_filter = Control.MOUSE_FILTER_IGNORE
	box.add_child(hint)
	close_btn = Button.new()
	close_btn.text = "✕"
	close_btn.tooltip_text = "Закрыть окно (Esc в игре)"
	close_btn.focus_mode = Control.FOCUS_NONE
	close_btn.pressed.connect(func(): send_key("esc", "press"))
	box.add_child(close_btn)
	view = TextureRect.new()
	view.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	view.stretch_mode = TextureRect.STRETCH_SCALE
	view.texture_filter = CanvasItem.TEXTURE_FILTER_LINEAR
	view.mouse_filter = Control.MOUSE_FILTER_STOP
	view.mouse_default_cursor_shape = Control.CURSOR_ARROW
	view.gui_input.connect(_on_view_input)
	view.mouse_exited.connect(func(): _mouse_want = Vector2i(-1, -1))
	add_child(view)
	waiting = Label.new()
	waiting.text = "ожидание кадра от движка…"
	waiting.add_theme_color_override("font_color", Color(0.85, 0.78, 0.6))
	waiting.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	waiting.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	waiting.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(waiting)
	resized.connect(_layout)
	_layout()


func is_open() -> bool:
	return not stack.is_empty()


## A window or the menu screen is shown: mouse and keys go to the engine.
func active() -> bool:
	return screen or not stack.is_empty()


func top_name() -> String:
	return String(stack[-1].get("name", "")) if not stack.is_empty() else ""


static func title_for(wname: String) -> String:
	if TITLES.has(wname):
		return TITLES[wname]
	if wname.begins_with("hota:"):
		return "Окно HotA"
	return "Окно игры"


# ------------------------------------------------------------------------------------------ protocol

## window_open: the engine opened a window (nested ones come with a higher depth). `caption` from main.gd
## (e.g. the town's name).
func open_window(m: Dictionary, caption: String) -> void:
	if screen:                   # (not expected: windows come on the adventure map only)
		screen = false
		box.visible = true
	var depth := int(m.get("depth", stack.size() + 1))
	while stack.size() >= depth:
		stack.pop_back()
	stack.append(m)
	frame_size = Vector2i(int(m.get("w", 800)), int(m.get("h", 600)))
	title_text = caption
	title.text = caption
	if stack.size() == 1 and not visible:
		await _show()
	_layout()


## window_close: depth <= 1 closes the overlay (the bridge then sends `state`).
func close_window(m: Dictionary) -> void:
	var depth := int(m.get("depth", 1))
	while stack.size() >= depth and not stack.is_empty():
		stack.pop_back()
	if depth <= 1:
		stack.clear()
	if stack.is_empty():
		_hide()
	else:
		frame_size = Vector2i(int(stack[-1].get("w", 800)), int(stack[-1].get("h", 600)))
		_layout()


## Connection lost: the engine window stays open in the game; the bridge sends window_open again on reconnect.
func reset() -> void:
	stack.clear()
	screen = false
	box.visible = true
	_buttons.clear()
	_hide()


func show_frame(m: Dictionary) -> void:
	var seq := int(m.get("seq", 0))
	if seq <= last_seq and seq != 0:
		return
	var raw := Marshalls.base64_to_raw(String(m.get("data", "")))
	var img := Image.new()
	var err := img.load_png_from_buffer(raw) if String(m.get("fmt", "jpg")) == "png" else img.load_jpg_from_buffer(raw)
	if err != OK or img.is_empty():
		push_warning("window_frame %d: decode error %d" % [seq, err])
		return
	last_seq = seq
	frames += 1
	if tex == null or tex.get_size() != Vector2(img.get_size()):
		tex = ImageTexture.create_from_image(img)
		view.texture = tex
	else:
		tex.update(img)
	if img.get_size() != frame_size:
		frame_size = img.get_size()
		_layout()
	waiting.visible = false


# ------------------------------------------------------------------------------------------ screen mode

## The engine's menus fullscreen (phase "menu"): black around the letterboxed frame, no title box, no 3D pass.
func open_screen() -> void:
	if screen:
		return
	stack.clear()
	screen = true
	box.visible = false
	backdrop.texture = null
	dim.color = Color(0, 0, 0, 1)
	waiting.visible = tex == null
	if not _frozen:
		get_viewport().disable_3d = true
		_frozen = true
	visible = true
	_layout()
	opened.emit()


## The adventure map was entered: back to the 3D world (the snapshot follows).
func close_screen() -> void:
	if not screen:
		return
	screen = false
	box.visible = true
	_hide()


# ------------------------------------------------------------------------------------------ show / hide

func _show() -> void:
	last_seq = -1
	waiting.visible = true
	view.texture = null
	tex = null
	if not live_world:
		# the backdrop: this frame of the world (with the HUD), small and dimmed = blurred; then no 3D pass
		await RenderingServer.frame_post_draw
		if stack.is_empty():
			return
		var img := get_viewport().get_texture().get_image()
		if img and not img.is_empty():
			img.resize(maxi(img.get_width() / 6, 1), maxi(img.get_height() / 6, 1), Image.INTERPOLATE_BILINEAR)
			backdrop.texture = ImageTexture.create_from_image(img)
		get_viewport().disable_3d = true
		_frozen = true
		dim.color = Color(0.03, 0.025, 0.02, 0.25)
	else:
		backdrop.texture = null
		dim.color = Color(0.02, 0.02, 0.025, 0.62)
	visible = true
	_layout()
	opened.emit()


func _hide() -> void:
	if not visible and not _frozen:
		return
	for b in _buttons.keys():      # never leave a button pressed in the engine
		_send_mouse(_mouse if _mouse.x >= 0 else Vector2i(0, 0), "up", b)
	_buttons.clear()
	visible = false
	if _frozen:
		get_viewport().disable_3d = false
		_frozen = false
	backdrop.texture = null
	view.texture = null
	tex = null
	_mouse = Vector2i(-1, -1)
	_mouse_want = Vector2i(-1, -1)
	closed.emit()


func _layout() -> void:
	if box == null:
		return
	var vp := size
	if screen:
		var fs0 := Vector2(maxi(frame_size.x, 1), maxi(frame_size.y, 1))
		_scale = maxf(minf(vp.x / fs0.x, vp.y / fs0.y), 0.1)
		var sz0 := (fs0 * _scale).floor()
		view.position = ((vp - sz0) * 0.5).floor()
		view.size = sz0
		waiting.position = view.position
		waiting.size = sz0
		return
	var avail := vp - Vector2(MARGIN * 2.0 + BORDER * 2.0, MARGIN * 2.0 + TITLE_H + BORDER * 2.0)
	var fs := Vector2(maxi(frame_size.x, 1), maxi(frame_size.y, 1))
	_scale = maxf(minf(avail.x / fs.x, avail.y / fs.y), 0.1)
	var sz := (fs * _scale).floor()
	var pos := Vector2(floorf((vp.x - sz.x) * 0.5), floorf(MARGIN + TITLE_H + BORDER + (avail.y - sz.y) * 0.5))
	view.position = pos
	view.size = sz
	waiting.position = pos
	waiting.size = sz
	box.position = pos - Vector2(BORDER, BORDER + TITLE_H)
	box.size = sz + Vector2(BORDER * 2.0, BORDER * 2.0 + TITLE_H)
	title.position = Vector2(12, 0)
	title.size = Vector2(box.size.x * 0.55, TITLE_H)
	close_btn.size = Vector2(30, 26)
	close_btn.position = Vector2(box.size.x - 36, 4)
	hint.position = Vector2(box.size.x * 0.5, 0)
	hint.size = Vector2(box.size.x * 0.5 - 46, TITLE_H)


# ------------------------------------------------------------------------------------------ input

## Overlay position -> engine coordinates; (-1, -1) outside the frame.
func to_engine(p: Vector2) -> Vector2i:
	return _local_to_engine(p - view.position)


## Engine coordinates -> overlay coordinates (centre of that engine pixel); for scripted clicks.
func to_screen(e: Vector2i) -> Vector2:
	return view.position + (Vector2(e) + Vector2(0.5, 0.5)) * _scale


func _on_view_input(ev: InputEvent) -> void:
	if not active():
		return
	if ev is InputEventMouseMotion:
		var e := _local_to_engine(ev.position)
		if e.x >= 0:
			_mouse_want = e
		accept_event()
	elif ev is InputEventMouseButton:
		var mb := ev as InputEventMouseButton
		var btn := ""
		if mb.button_index == MOUSE_BUTTON_LEFT:
			btn = "left"
		elif mb.button_index == MOUSE_BUTTON_RIGHT:
			btn = "right"
		accept_event()
		if btn == "":
			return
		var e := _local_to_engine(mb.position)
		if e.x < 0:
			return
		if e != _mouse:              # the engine sees the pointer arrive first (hover state), then the button
			_send_mouse(e, "move", "left", mb)
		if mb.pressed:
			_buttons[btn] = true
			_send_mouse(e, "down", btn, mb)
		elif _buttons.has(btn):
			_buttons.erase(btn)
			_send_mouse(e, "up", btn, mb)


func _local_to_engine(p: Vector2) -> Vector2i:
	var l := p / _scale
	return Vector2i(clampi(int(l.x), 0, frame_size.x - 1), clampi(int(l.y), 0, frame_size.y - 1)) \
		if l.x >= 0 and l.y >= 0 and l.x < frame_size.x and l.y < frame_size.y else Vector2i(-1, -1)


func _send_mouse(e: Vector2i, action: String, button: String, ev: InputEventWithModifiers = null) -> void:
	var m := {"t": "window_input", "kind": "mouse", "x": e.x, "y": e.y, "action": action, "button": button}
	if ev:
		_mods(m, ev)
	_mouse = e
	_mouse_want = Vector2i(-1, -1) if _mouse_want == e else _mouse_want
	input_msg.emit(m)


func send_key(key: String, action: String, ev: InputEventWithModifiers = null) -> void:
	var m := {"t": "window_input", "kind": "key", "key": key, "action": action}
	if ev:
		_mods(m, ev)
	input_msg.emit(m)


static func _mods(m: Dictionary, ev: InputEventWithModifiers) -> void:
	if ev.shift_pressed:
		m["shift"] = true
	if ev.ctrl_pressed:
		m["ctrl"] = true
	if ev.alt_pressed:
		m["alt"] = true


func _process(delta: float) -> void:
	if not active():
		return
	_move_t += delta
	if _mouse_want.x >= 0 and _mouse_want != _mouse and _move_t >= MOVE_INTERVAL:
		_move_t = 0.0
		_send_mouse(_mouse_want, "move", "left")


## Keys go to the engine (by physical key = scan code) while a window is open; F2 stays with the connect panel,
## and a focused text field (connect panel) keeps its keys.
func _input(ev: InputEvent) -> void:
	if not active() or not visible or not (ev is InputEventKey):
		return
	var k := ev as InputEventKey
	var fo := get_viewport().gui_get_focus_owner()
	if fo is LineEdit or fo is TextEdit:
		return
	var code := k.physical_keycode if k.physical_keycode != KEY_NONE else k.keycode
	if code == KEY_F2 or code == KEY_F3:     # connect panel / local mode panel
		return
	var kn := key_name(code)
	get_viewport().set_input_as_handled()
	if kn == "":
		return
	send_key(kn, "down" if k.pressed else "up", k)


## Godot key -> bridge key name (bridge.py SCAN; DIK scan codes on the engine side).
static func key_name(code: int) -> String:
	if code >= KEY_A and code <= KEY_Z:
		return char(code).to_lower()
	if code >= KEY_0 and code <= KEY_9:
		return char(code)
	if code >= KEY_F1 and code <= KEY_F12:
		return "f%d" % (code - KEY_F1 + 1)
	match code:
		KEY_ESCAPE: return "esc"
		KEY_ENTER, KEY_KP_ENTER: return "enter"
		KEY_TAB: return "tab"
		KEY_SPACE: return "space"
		KEY_BACKSPACE: return "backspace"
		KEY_UP: return "up"
		KEY_DOWN: return "down"
		KEY_LEFT: return "left"
		KEY_RIGHT: return "right"
		KEY_HOME: return "home"
		KEY_END: return "end"
		KEY_PAGEUP: return "pageup"
		KEY_PAGEDOWN: return "pagedown"
		KEY_INSERT: return "insert"
		KEY_DELETE: return "delete"
		KEY_SHIFT: return "shift"
		KEY_CTRL: return "ctrl"
		KEY_ALT: return "alt"
		KEY_MINUS: return "minus"
		KEY_EQUAL: return "equal"
		KEY_BRACKETLEFT: return "["
		KEY_BRACKETRIGHT: return "]"
		KEY_SEMICOLON: return ";"
		KEY_APOSTROPHE: return "'"
		KEY_QUOTELEFT: return "`"
		KEY_BACKSLASH: return "\\"
		KEY_COMMA: return ","
		KEY_PERIOD: return "."
		KEY_SLASH: return "/"
		KEY_KP_ADD: return "kp_plus"
		KEY_KP_SUBTRACT: return "kp_minus"
		KEY_KP_MULTIPLY: return "kp_multiply"
	return ""
