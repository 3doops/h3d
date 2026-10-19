extends Control
## HUD: resources, date, hero/town lists, End Turn, level switch, dialog panel, event log, connection indicator.

signal hero_clicked(id: int)
signal town_clicked(id: int)
signal end_turn_pressed
signal level_toggled
signal dialog_answered(id: int, answer: int)
signal town_window_requested(id: int)     # open the engine's town screen (passthrough window, PLAN 1.9)
signal hero_window_requested(id: int)     # open the engine's hero screen
signal system_menu_requested               # the engine's system options (load / save / main menu / quit, PLAN 4.5)

const Defs = preload("res://scripts/defs.gd")

var res_labels: Array[Label] = []
var date_label: Label
var conn_dot: ColorRect
var conn_label: Label
var turn_label: Label
var hero_box: VBoxContainer
var town_box: VBoxContainer
var log_text: RichTextLabel
var hover_label: Label
var dialog_panel: PanelContainer
var dialog_text: Label
var dialog_buttons: HBoxContainer
var level_btn: Button
var end_btn: Button
var menu_btn: Button                    # "Меню игры": the engine's system options window
var dialog_id := -1
var dialog_queue: Array = []
var hold_dialogs := false
var log_lines: Array[String] = []
var _selected_hero := -1
var windows_enabled := false            # the bridge streams engine windows (hello caps "windows")
var _my_turn := false
var _win_buttons: Array = []
var _win_by_key := {}                   # "town:ID" / "hero:ID" -> the window button (scripted runs click it)


func _ready() -> void:
	set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	theme = _make_theme()
	_build_top()
	_build_right()
	_build_log()
	_build_dialog()
	hover_label = Label.new()
	hover_label.add_theme_color_override("font_shadow_color", Color(0, 0, 0, 0.9))
	hover_label.add_theme_constant_override("shadow_offset_x", 1)
	hover_label.add_theme_constant_override("shadow_offset_y", 1)
	hover_label.set_anchors_preset(Control.PRESET_CENTER_BOTTOM)
	hover_label.position = Vector2(-200, -34)
	hover_label.size = Vector2(400, 26)
	hover_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	hover_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(hover_label)


func _make_theme() -> Theme:
	var th := Theme.new()
	var f := SystemFont.new()
	f.font_names = PackedStringArray(["Georgia", "Palatino Linotype", "Cambria", "Times New Roman", "DejaVu Serif", "serif"])
	f.antialiasing = TextServer.FONT_ANTIALIASING_GRAY
	th.default_font = f
	th.default_font_size = 17
	var panel := StyleBoxFlat.new()
	panel.bg_color = Color(0.07, 0.055, 0.045, 0.84)
	panel.border_color = Color(0.62, 0.48, 0.24)
	panel.set_border_width_all(2)
	panel.set_corner_radius_all(4)
	panel.set_content_margin_all(8)
	panel.shadow_color = Color(0, 0, 0, 0.45)
	panel.shadow_size = 6
	th.set_stylebox("panel", "PanelContainer", panel)
	var bn := StyleBoxFlat.new()
	bn.bg_color = Color(0.22, 0.15, 0.09, 0.95)
	bn.border_color = Color(0.55, 0.42, 0.2)
	bn.set_border_width_all(1)
	bn.set_corner_radius_all(3)
	bn.set_content_margin_all(5)
	var bh := bn.duplicate()
	bh.bg_color = Color(0.34, 0.24, 0.12, 0.95)
	bh.border_color = Color(0.9, 0.72, 0.35)
	var bp := bn.duplicate()
	bp.bg_color = Color(0.45, 0.32, 0.12, 0.95)
	th.set_stylebox("normal", "Button", bn)
	th.set_stylebox("hover", "Button", bh)
	th.set_stylebox("pressed", "Button", bp)
	th.set_stylebox("focus", "Button", StyleBoxEmpty.new())
	th.set_color("font_color", "Button", Color(0.95, 0.88, 0.7))
	th.set_color("font_hover_color", "Button", Color(1, 0.95, 0.75))
	th.set_color("font_color", "Label", Color(0.95, 0.9, 0.78))
	th.set_color("default_color", "RichTextLabel", Color(0.92, 0.87, 0.75))
	return th


func _panel(parent: Control) -> PanelContainer:
	var p := PanelContainer.new()
	parent.add_child(p)
	return p


func _res_icon(i: int) -> Control:
	var c := Control.new()
	c.custom_minimum_size = Vector2(22, 22)
	c.draw.connect(_draw_res_icon.bind(c, i))
	c.tooltip_text = Defs.RES_RU[i]
	return c


func _draw_res_icon(c: Control, i: int) -> void:
	var col: Color = Defs.RES_COLORS[i]
	var ctr := Vector2(11, 11)
	match i:
		0:
			c.draw_rect(Rect2(3, 7, 16, 8), col)
			c.draw_circle(Vector2(18, 11), 4, col.lightened(0.3))
		1:
			c.draw_circle(ctr, 8, col)
			c.draw_circle(ctr + Vector2(-3, -3), 3, Color(1, 1, 1, 0.8))
		2:
			c.draw_colored_polygon(PackedVector2Array([Vector2(2, 18), Vector2(6, 6), Vector2(14, 3), Vector2(20, 12), Vector2(17, 19)]), col)
		3:
			c.draw_colored_polygon(PackedVector2Array([Vector2(11, 2), Vector2(20, 19), Vector2(2, 19)]), col)
		4, 5:
			c.draw_colored_polygon(PackedVector2Array([Vector2(11, 1), Vector2(19, 9), Vector2(11, 21), Vector2(3, 9)]), col)
			c.draw_colored_polygon(PackedVector2Array([Vector2(11, 1), Vector2(15, 9), Vector2(11, 12), Vector2(7, 9)]), col.lightened(0.35))
		6:
			c.draw_circle(ctr + Vector2(0, 2), 8, col.darkened(0.2))
			c.draw_circle(ctr, 8, col)
			c.draw_circle(ctr, 5, col.lightened(0.25))


func _build_top() -> void:
	var p := _panel(self)
	p.set_anchors_preset(Control.PRESET_TOP_WIDE)
	p.offset_left = 8
	p.offset_right = -8
	p.offset_top = 6
	var hb := HBoxContainer.new()
	hb.add_theme_constant_override("separation", 16)
	p.add_child(hb)
	for i in 7:
		var b := HBoxContainer.new()
		b.add_child(_res_icon(i))
		var l := Label.new()
		l.text = "0"
		l.custom_minimum_size = Vector2(56, 0)
		b.add_child(l)
		hb.add_child(b)
		res_labels.append(l)
	var sp := Control.new()
	sp.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hb.add_child(sp)
	turn_label = Label.new()
	hb.add_child(turn_label)
	date_label = Label.new()
	date_label.text = "—"
	hb.add_child(date_label)
	var cb := HBoxContainer.new()
	conn_dot = ColorRect.new()
	conn_dot.custom_minimum_size = Vector2(12, 12)
	conn_dot.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	conn_dot.color = Color(0.8, 0.2, 0.2)
	cb.add_child(conn_dot)
	conn_label = Label.new()
	conn_label.text = "нет связи"
	cb.add_child(conn_label)
	hb.add_child(cb)


func _build_right() -> void:
	var p := _panel(self)
	p.set_anchors_preset(Control.PRESET_TOP_RIGHT)
	p.offset_left = -258
	p.offset_right = -8
	p.offset_top = 56
	p.custom_minimum_size = Vector2(250, 0)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 4)
	p.add_child(vb)
	var h1 := Label.new()
	h1.text = "Герои"
	h1.add_theme_color_override("font_color", Color(1.0, 0.82, 0.45))
	vb.add_child(h1)
	hero_box = VBoxContainer.new()
	vb.add_child(hero_box)
	var h2 := Label.new()
	h2.text = "Города"
	h2.add_theme_color_override("font_color", Color(1.0, 0.82, 0.45))
	vb.add_child(h2)
	town_box = VBoxContainer.new()
	vb.add_child(town_box)
	vb.add_child(HSeparator.new())
	level_btn = Button.new()
	level_btn.text = "Подземелье ▼"
	level_btn.pressed.connect(func(): level_toggled.emit())
	vb.add_child(level_btn)
	end_btn = Button.new()
	end_btn.text = "Конец хода"
	end_btn.custom_minimum_size = Vector2(0, 40)
	end_btn.pressed.connect(func(): end_turn_pressed.emit())
	vb.add_child(end_btn)
	menu_btn = Button.new()
	menu_btn.text = "Меню игры"
	menu_btn.tooltip_text = "Системное меню игры: сохранить, загрузить, начать заново, главное меню, выход, настройки (Esc)"
	menu_btn.pressed.connect(func(): system_menu_requested.emit())
	menu_btn.visible = false
	vb.add_child(menu_btn)


func _build_log() -> void:
	var p := _panel(self)
	p.set_anchors_preset(Control.PRESET_BOTTOM_LEFT)
	p.offset_left = 8
	p.offset_top = -128
	p.offset_bottom = -8
	p.offset_right = 430
	p.self_modulate = Color(1, 1, 1, 0.7)
	log_text = RichTextLabel.new()
	log_text.bbcode_enabled = true
	log_text.scroll_following = true
	log_text.custom_minimum_size = Vector2(410, 104)
	log_text.add_theme_font_size_override("normal_font_size", 14)
	log_text.mouse_filter = Control.MOUSE_FILTER_PASS
	p.add_child(log_text)


func _build_dialog() -> void:
	dialog_panel = PanelContainer.new()
	dialog_panel.set_anchors_preset(Control.PRESET_CENTER)
	dialog_panel.custom_minimum_size = Vector2(440, 0)
	dialog_panel.visible = false
	add_child(dialog_panel)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 14)
	dialog_panel.add_child(vb)
	dialog_text = Label.new()
	dialog_text.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	dialog_text.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	dialog_text.custom_minimum_size = Vector2(420, 60)
	dialog_text.add_theme_font_size_override("font_size", 19)
	vb.add_child(dialog_text)
	dialog_buttons = HBoxContainer.new()
	dialog_buttons.alignment = BoxContainer.ALIGNMENT_CENTER
	dialog_buttons.add_theme_constant_override("separation", 12)
	vb.add_child(dialog_buttons)


# ------------------------------------------------------------------------------------------- updates

func set_connection(status: String, detail: String) -> void:
	match status:
		"online":
			conn_dot.color = Color(0.3, 0.9, 0.35)
			conn_label.text = "связь"
		"connecting":
			conn_dot.color = Color(0.95, 0.75, 0.2)
			conn_label.text = "подключение…"
		_:
			conn_dot.color = Color(0.85, 0.2, 0.2)
			conn_label.text = "нет связи"
	conn_label.tooltip_text = detail


func set_resources(res: Array) -> void:
	for i in mini(res.size(), 7):
		res_labels[i].text = _fmt(int(res[i]))


func _fmt(v: int) -> String:
	var s := str(absi(v))
	var out := ""
	while s.length() > 3:
		out = " " + s.substr(s.length() - 3) + out
		s = s.substr(0, s.length() - 3)
	return ("-" if v < 0 else "") + s + out


func set_date(d: Dictionary) -> void:
	date_label.text = "Месяц %d, неделя %d, день %d" % [int(d.get("month", 1)), int(d.get("week", 1)), int(d.get("day", 1))]


func set_turn(current: int, me: int, color_name: String) -> void:
	turn_label.text = "" if current == me else "Ходит: %s" % color_name
	end_btn.disabled = current != me
	_my_turn = current == me
	_update_win_buttons()


func set_windows_enabled(on: bool) -> void:
	windows_enabled = on
	_update_win_buttons()


func _update_win_buttons() -> void:
	if menu_btn:
		menu_btn.visible = windows_enabled
		menu_btn.disabled = not (windows_enabled and _my_turn)
	for b in _win_buttons:
		if is_instance_valid(b):
			b.disabled = not (windows_enabled and _my_turn)
			b.visible = windows_enabled


func _row(main: Button, extra: Button) -> HBoxContainer:
	var hb := HBoxContainer.new()
	hb.add_theme_constant_override("separation", 4)
	hb.add_child(main)
	hb.add_child(extra)
	return hb


## Small button with a drawn icon ("town": gate with towers, "hero": helmet) that opens the engine's own screen.
func _win_button(kind: String, tip: String, cb: Callable) -> Button:
	var b := Button.new()
	b.custom_minimum_size = Vector2(34, 30)
	b.tooltip_text = tip
	b.focus_mode = Control.FOCUS_NONE
	b.pressed.connect(cb)
	b.draw.connect(_draw_win_icon.bind(b, kind))
	b.disabled = not (windows_enabled and _my_turn)
	b.visible = windows_enabled
	_win_buttons.append(b)
	return b


func _draw_win_icon(b: Button, kind: String) -> void:
	var col := Color(0.95, 0.8, 0.45) if not b.disabled else Color(0.55, 0.5, 0.42)
	var c := b.size * 0.5
	if kind == "town":
		var base := c + Vector2(-9, -2)
		b.draw_rect(Rect2(base, Vector2(18, 10)), col)
		for x in [-10, -2, 6]:
			b.draw_rect(Rect2(c + Vector2(x, -9), Vector2(4, 8)), col)
		b.draw_rect(Rect2(c + Vector2(-2.5, 2), Vector2(5, 6)), Color(0.15, 0.1, 0.06))
	else:
		var pts := PackedVector2Array([c + Vector2(-8, 8), c + Vector2(-8, -2), c + Vector2(-5, -8), c + Vector2(0, -10),
			c + Vector2(5, -8), c + Vector2(8, -2), c + Vector2(8, 8)])
		b.draw_colored_polygon(pts, col)
		b.draw_rect(Rect2(c + Vector2(-6, -2), Vector2(12, 3)), Color(0.15, 0.1, 0.06))
		b.draw_line(c + Vector2(0, 1), c + Vector2(0, 8), Color(0.15, 0.1, 0.06), 2.0)


func set_level(z: int) -> void:
	level_btn.text = "Подземелье ▼" if z == 0 else "Поверхность ▲"


func set_heroes(list: Array, me: int, selected: int) -> void:
	_selected_hero = selected
	_win_buttons = _win_buttons.filter(func(b): return is_instance_valid(b) and not hero_box.is_ancestor_of(b))
	for c in hero_box.get_children():
		c.queue_free()
	for h in list:
		if int(h.owner) != me:
			continue
		var b := Button.new()
		var mp := int(h.get("mp", 0))
		var mpm := maxi(int(h.get("mp_max", 1)), 1)
		b.text = "%s  ·  ур.%d  ·  %d%%" % [h.name, int(h.get("level", 1)), int(100.0 * mp / mpm)]
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.clip_text = true
		b.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		b.custom_minimum_size = Vector2(196, 0)
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		if int(h.id) == selected:
			b.add_theme_color_override("font_color", Color(1.0, 0.85, 0.35))
			b.text = "▶ " + b.text
		var id := int(h.id)
		b.pressed.connect(func(): hero_clicked.emit(id))
		var wb := _win_button("hero", "Экран героя (окно игры): армия, артефакты, навыки",
			func(): hero_window_requested.emit(id))
		_win_by_key["hero:%d" % id] = wb
		hero_box.add_child(_row(b, wb))


func set_towns(list: Array, me: int) -> void:
	_win_buttons = _win_buttons.filter(func(b): return is_instance_valid(b) and not town_box.is_ancestor_of(b))
	for c in town_box.get_children():
		c.queue_free()
	for t in list:
		if int(t.owner) != me:
			continue
		var b := Button.new()
		var fac := int(t.get("faction", 0))
		b.text = "%s (%s)" % [t.name, Defs.FACTION_NAMES[fac] if fac < Defs.FACTION_NAMES.size() else "?"]
		b.alignment = HORIZONTAL_ALIGNMENT_LEFT
		b.clip_text = true
		b.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		b.custom_minimum_size = Vector2(196, 0)
		b.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		b.tooltip_text = b.text
		var id := int(t.id)
		b.pressed.connect(func(): town_clicked.emit(id))
		var wb := _win_button("town", "Войти в город (окно игры): постройки, найм, рынок, таверна",
			func(): town_window_requested.emit(id))
		_win_by_key["town:%d" % id] = wb
		town_box.add_child(_row(b, wb))


func log_event(text: String, color := Color(0.92, 0.87, 0.75)) -> void:
	log_text.append_text("[color=#%s]%s[/color]\n" % [color.to_html(false), text])
	print("log: ", text)


func set_hover(text: String) -> void:
	hover_label.text = text


# ------------------------------------------------------------------------------------------- dialogs

## Engine text: "{Title}" headers -> plain title line.
static func plain(t: String) -> String:
	var r := RegEx.new()
	r.compile("[{]([^}]*)[}]")
	return r.sub(t, "$1\n", true).strip_edges()


func close_dialog(id: int) -> void:
	dialog_queue = dialog_queue.filter(func(d): return int(d.get("id", -1)) != id)
	if dialog_panel.visible and dialog_id == id:
		dialog_panel.visible = false
		dialog_id = -1
		_try_show()


func queue_dialog(d: Dictionary) -> void:
	dialog_queue.append(d)
	_try_show()


func _try_show() -> void:
	if dialog_panel.visible or hold_dialogs or dialog_queue.is_empty():
		return
	var d: Dictionary = dialog_queue.pop_front()
	dialog_id = int(d.id)
	dialog_text.text = plain(String(d.get("text", "")))
	for c in dialog_buttons.get_children():
		c.queue_free()
	var style := String(d.get("style", "ok"))
	match style:
		"yesno":
			_dlg_button("Да", 0)
			_dlg_button("Нет", 1)
		"choose":
			var opts: Array = d.get("options", [])
			for i in opts.size():
				_dlg_button(String(opts[i].get("text", "#%d" % i)), i)
		_:
			_dlg_button("OK", 0)
	if bool(d.get("cancel", false)):
		_dlg_button("Отмена", -1)
	dialog_panel.visible = true
	dialog_panel.reset_size()
	dialog_panel.position = (size - dialog_panel.size) * 0.5


func _dlg_button(text: String, answer: int) -> void:
	var b := Button.new()
	b.text = text
	b.custom_minimum_size = Vector2(110, 38)
	b.pressed.connect(func(): answer_dialog(answer))
	dialog_buttons.add_child(b)


func answer_dialog(answer: int) -> void:
	if not dialog_panel.visible:
		return
	dialog_panel.visible = false
	dialog_answered.emit(dialog_id, answer)
	dialog_id = -1
	_try_show()


## The window button of a town / hero in the lists ("town" | "hero"), null if there is none.
func window_button(kind: String, id: int) -> Button:
	var b = _win_by_key.get("%s:%d" % [kind, id])
	return b if is_instance_valid(b) and not b.is_queued_for_deletion() else null


## The i-th answer button of the open dialog (null if none).
func dialog_button(i: int) -> Button:
	var live := dialog_buttons.get_children().filter(func(c): return not c.is_queued_for_deletion())
	return live[i] if dialog_panel.visible and i >= 0 and i < live.size() else null


func dialog_open() -> bool:
	return dialog_panel.visible


func release_dialogs() -> void:
	hold_dialogs = false
	_try_show()
