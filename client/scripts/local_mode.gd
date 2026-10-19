extends CanvasLayer
## Autoload "LocalMode": local mode — the client itself starts the original game + bridge on this machine
## (instead of connecting to a bridge somewhere else) and stops them on exit.
##
##  - chosen when there is no `--url` / `--server` and the saved mode is "local" (first start: local unless an
##    address was saved in the connect panel); `--local` forces it, `--game-dir DIR` sets the folder (and implies it);
##    the connect panel (F2) switches modes, F3 opens this panel (folder, checklist, restart);
##  - game folder: saved (user://local.cfg [game] dir) → env HOTA_GAME_DIR → doctor.py's search (registry, GOG/Steam,
##    Wine prefixes…); missing/invalid → this panel explains what is needed and opens a folder dialog;
##  - checks: bridge/doctor.py --json (Python, frida, websockets; on Linux wine, prefix, 32-bit, Windows Python in
##    the prefix), shown as a checklist with one-line fixes and printed to the console;
##  - Python: the packages ship a portable Windows Python next to bridge/ (python/python.exe: embeddable 3.12 +
##    frida + websockets + Pillow). Windows: a system Python that has frida + websockets, else the bundled one;
##    Linux: `wine python/python.exe bridge.py` in the client's own prefix ~/.local/share/HotA3D/wine (doctor.py
##    creates it on first run; WINEPREFIX / HOTA3D_WINE_PYTHON override). Linux without host python3 (doctor.py
##    cannot run): the client does the same essential checks itself (_linux_direct);
##  - first start with no game folder found: the folder dialog opens by itself;
##  - launch: the command doctor.py returns (Windows: python bridge.py --game-dir …; Linux: wine python.exe bridge.py
##    …), control files in user://local_run (status.json, stop, heartbeat, bridge.log, see bridge.py --control-dir);
##  - the original game window (PLAN 4.6): "auto" (default) = visible when this machine has a desktop (doctor.py
##    decides: Windows interactive window station, Linux DISPLAY / WAYLAND_DISPLAY), then the player can play in the
##    game window and in the client at the same time (bridge.py --visible); the panel's check box or
##    `--game-window on|off|auto` overrides it (user://local.cfg [local] game_window).
## Debug: `--local-shot PATH` saves a frame of the panel once the checks are done and quits;
## `--local-shot-dialog` also opens the (non-native) folder dialog for that frame.

signal state_changed(state: String)

const CFG := "user://local.cfg"
const CONNECT_CFG := "user://connect.cfg"
const DEFAULT_PORT := 8765
const REQUIRED := ["h3hota.exe", "HotA.dll", "Data/H3bitmap.lod", "Data/H3sprite.lod", "Data/HotA.lod"]
const HEARTBEAT_SEC := 5.0
const MARK := {"ok": "[color=#8fd18f]✓[/color]", "warn": "[color=#e8c060]![/color]",
	"fail": "[color=#ff7a6a]✘[/color]", "info": "[color=#9ab]·[/color]"}

var active := false            # local mode chosen
var game_dir := ""
var port := DEFAULT_PORT
var load_save := ""            # bridge.py --load (empty = bridge.py --menu: start at the game's main menu)
var game_window := "auto"      # the original game window: auto | on | off (bridge.py --visible, PLAN 4.6)
var python := ""               # Windows: the bridge's interpreter; Linux: host python3 that runs doctor.py
var bridge_dir := ""
var control_dir := ""
var bridge_pid := -1
var report: Dictionary = {}    # doctor.py result
var launch: Dictionary = {}
var state := "idle"            # idle | checking | need_folder | blocked | starting | running | failed | exited
var game_quit := false         # the player quit from the game's own menu (bridge `phase exited`): not a failure

var _thread: Thread = null
var _hb := 0.0
var _poll := 0.0
var _status: Dictionary = {}
var _no_ui := false
var _shot_path := ""
var _shot_dialog := false
var _cli_dir := ""
var _picked := false           # folder chosen in the dialog: doctor checks exactly it (no search)
var _remote_cli := false
var _asked_dir := false        # the folder dialog was opened by itself once (first start)
var _cli_window := ""          # --game-window on|off|auto (not saved)

var panel: PanelContainer
var title_label: Label
var status_label: Label
var list: RichTextLabel
var btn_dir: Button
var btn_retry: Button
var btn_remote: Button
var chk_window: CheckBox
var dialog: FileDialog


func _ready() -> void:
	layer = 51
	process_mode = Node.PROCESS_MODE_ALWAYS
	var force_local := false
	for a in [OS.get_cmdline_args(), OS.get_cmdline_user_args()]:
		for i in a.size():
			var s: String = a[i]
			if s == "--url" or s.begins_with("--url=") or s == "--server" or s.begins_with("--server="):
				_remote_cli = true
			elif s == "--local":
				force_local = true
			elif s == "--game-dir" and i + 1 < a.size():
				_cli_dir = a[i + 1]
				force_local = true
			elif s == "--local-port" and i + 1 < a.size():
				port = int(a[i + 1])
			elif s == "--game-window" and i + 1 < a.size():
				_cli_window = a[i + 1]
			elif s == "--local-shot" and i + 1 < a.size():
				_shot_path = a[i + 1]
			elif s == "--local-shot-dialog":
				_shot_dialog = true
			elif s == "--no-ui":
				_no_ui = true
	var cf := ConfigFile.new()
	cf.load(CFG)
	game_dir = String(cf.get_value("game", "dir", ""))
	port = int(cf.get_value("local", "port", port)) if port == DEFAULT_PORT else port
	load_save = String(cf.get_value("local", "load", ""))
	python = String(cf.get_value("local", "python", ""))
	game_window = String(cf.get_value("local", "game_window", "auto"))
	if _cli_window in ["on", "off", "auto"]:
		game_window = _cli_window
	if _cli_dir != "":
		game_dir = _cli_dir
	var mode := String(cf.get_value("local", "mode", ""))
	if mode == "":
		var cc := ConfigFile.new()
		mode = "remote" if cc.load(CONNECT_CFG) == OK and String(cc.get_value("bridge", "url", "")) != "" else "local"
	active = force_local or (not _remote_cli and mode == "local")
	control_dir = OS.get_user_data_dir().path_join("local_run")
	bridge_dir = _find_bridge_dir()
	_build()
	if active:
		print("local: local mode (game + bridge on this machine)")
		start.call_deferred()


func url() -> String:
	return "ws://127.0.0.1:%d" % port


# ============================================================================================ mode / config

func _save_cfg() -> void:
	var cf := ConfigFile.new()
	cf.load(CFG)
	cf.set_value("local", "mode", "local" if active else "remote")
	if game_dir != "":
		cf.set_value("game", "dir", game_dir)
	if python != "":
		cf.set_value("local", "python", python)
	cf.set_value("local", "game_window", game_window)
	cf.save(CFG)


## Switch to local mode (connect panel button) and start.
func enable() -> void:
	active = true
	_save_cfg()
	start()


## Back to a remote bridge: stop what we started, remember the choice.
func disable() -> void:
	stop_bridge()
	active = false
	_save_cfg()
	_set_state("idle")
	panel.visible = false


func _net() -> Node:
	var cp := get_node_or_null("/root/ConnectPanel")
	return cp.net if cp != null else null


# ============================================================================================ checks

func _find_bridge_dir() -> String:
	var c: Array[String] = [OS.get_executable_path().get_base_dir().path_join("bridge")]
	var res := ProjectSettings.globalize_path("res://")
	if res != "":
		c.append(res.path_join("../bridge").simplify_path())
	for d in c:
		if FileAccess.file_exists(d.path_join("bridge.py")) and FileAccess.file_exists(d.path_join("doctor.py")):
			return d
	return ""


static func _ci_exists(base: String, rel: String) -> bool:
	var cur := base
	for part in rel.split("/"):
		var p := cur.path_join(part)
		if FileAccess.file_exists(p) or DirAccess.dir_exists_absolute(p):
			cur = p
			continue
		var found := ""
		if DirAccess.dir_exists_absolute(cur):
			for n in Array(DirAccess.get_files_at(cur)) + Array(DirAccess.get_directories_at(cur)):
				if String(n).to_lower() == part.to_lower():
					found = n
					break
		if found == "":
			return false
		cur = cur.path_join(found)
	return true


## Required files missing in a folder ([] = valid game folder).
static func missing_files(d: String) -> Array:
	var out := []
	if d == "" or not DirAccess.dir_exists_absolute(d):
		return REQUIRED.duplicate()
	for r in REQUIRED:
		if not _ci_exists(d, r):
			out.append(r)
	return out


## Folder known without searching: saved/--game-dir → HOTA_GAME_DIR → the repo copy (editor run).
func _known_game_dir() -> String:
	if game_dir != "" and (_picked or game_dir == _cli_dir):
		return game_dir  # chosen by the user: checked as it is, never replaced by a found one
	var c: Array[String] = []
	if game_dir != "":
		c.append(game_dir)
	if OS.get_environment("HOTA_GAME_DIR") != "":
		c.append(OS.get_environment("HOTA_GAME_DIR"))
	if bridge_dir != "":
		c.append(bridge_dir.path_join("../../game/Heroes3_HotA1").simplify_path())
	for d in c:
		if missing_files(d).is_empty():
			return d
	return game_dir      # invalid saved/explicit folder: doctor reports what is missing in it


## Runs in a thread: find an interpreter, run doctor.py --json. Returns {python, rc, out, report}.
func _check_work(gd: String, py_saved: String, explicit: bool) -> Dictionary:
	var res := {"python": "", "report": {}, "out": ""}
	var py := _find_python(py_saved)
	res.python = py
	if py == "" and bridge_dir != "" and OS.get_name() != "Windows":
		res.report = _linux_direct(gd)
		return res
	if py == "" or bridge_dir == "":
		return res
	DirAccess.make_dir_recursive_absolute(control_dir)
	var out_file := control_dir.path_join("doctor.json")
	DirAccess.remove_absolute(out_file)
	var a := ["-I", bridge_dir.path_join("doctor.py"), "--json", "--out", out_file, "--port", str(port),
		"--control-dir", control_dir]
	if gd != "" and (explicit or missing_files(gd).is_empty()):
		a += ["--game-dir", gd]
	if OS.get_name() == "Windows":
		a += ["--python", py]
	if load_save != "":
		a += ["--load", load_save]
	a += ["--visible", game_window]
	var output := []
	res.rc = OS.execute(py, a, output, true)
	res.out = "".join(output)
	var txt := FileAccess.get_file_as_string(out_file)
	var j = JSON.parse_string(txt) if txt != "" else null
	if j is Dictionary:
		res.report = j
	return res


func _bundled_python() -> String:
	var p := bridge_dir.path_join("../python/python.exe").simplify_path() if bridge_dir != "" else ""
	return p if p != "" and FileAccess.file_exists(p) else ""


static func _zpath(p: String) -> String:
	## Linux path -> as Windows programs under Wine see it (Z: = / in every prefix)
	return "Z:" + p.simplify_path().replace("/", "\\")


static func _chk(id: String, status: String, title: String, detail := "", fix := "") -> Dictionary:
	return {"id": id, "status": status, "title": title, "detail": detail, "fix": fix}


## Linux without a host python3 (doctor.py cannot run): the essential checks + launch command, done here.
## Same rules as doctor.py linux_checks with the bundled Python: own prefix unless WINEPREFIX is set.
func _linux_direct(gd: String) -> Dictionary:
	var checks := []
	var r := {"ok": false, "checks": checks, "game_dir": "", "port": port, "launch": null}
	checks.append(_chk("os", "ok", "ОС", OS.get_name() + " (python3 нет: проверка без doctor.py)"))
	var miss := missing_files(gd)
	if gd == "":
		checks.append(_chk("game_dir", "fail", "Папка игры не указана", "",
			"Кнопка «Папка игры…»: папка с h3hota.exe, HotA.dll и Data/*.lod"))
	elif not miss.is_empty():
		checks.append(_chk("game_dir", "fail", "Папка игры: не хватает файлов", "%s: нет %s" % [gd, ", ".join(miss)],
			"Укажите папку HotA (кнопка «Папка игры…»)"))
	else:
		checks.append(_chk("game_dir", "ok", "Папка игры", gd))
	var wine := OS.get_environment("WINE") if OS.get_environment("WINE") != "" else "wine"
	var out := []
	if OS.execute(wine, ["--version"], out, true) != 0:
		checks.append(_chk("wine", "fail", "Wine не найден", "",
			"Debian/Ubuntu: sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install wine wine32 wine64; "
			+ "Fedora: sudo dnf install wine; Arch: включить [multilib], sudo pacman -S wine"))
		return r
	checks.append(_chk("wine", "ok", "Wine", "".join(out).strip_edges()))
	var wpy := OS.get_environment("HOTA3D_WINE_PYTHON")
	if wpy == "":
		wpy = _bundled_python()
	if wpy == "":
		checks.append(_chk("wine_python", "fail", "Нет python/python.exe из пакета", bridge_dir,
			"Распакуйте пакет клиента целиком (папка python/ рядом с bridge/)"))
		return r
	var prefix := OS.get_environment("WINEPREFIX")
	if prefix == "":
		var xdg := OS.get_environment("XDG_DATA_HOME")
		prefix = (xdg if xdg != "" else OS.get_environment("HOME").path_join(".local/share")).path_join("HotA3D/wine")
	OS.set_environment("WINEPREFIX", prefix)
	OS.set_environment("WINEDEBUG", "-all")
	if not FileAccess.file_exists(prefix.path_join("system.reg")):
		DirAccess.make_dir_recursive_absolute(prefix)
		OS.set_environment("WINEDLLOVERRIDES", "mscoree=d;mshtml=d")   # no Mono/Gecko install dialogs
		OS.execute(wine, ["wineboot", "-i"], [], true)
		OS.unset_environment("WINEDLLOVERRIDES")
		if not FileAccess.file_exists(prefix.path_join("system.reg")):
			checks.append(_chk("wineprefix", "fail", "Не удалось создать префикс Wine", prefix,
				"WINEPREFIX=\"%s\" wineboot -u — и посмотрите, что пишет Wine" % prefix))
			return r
		checks.append(_chk("wineprefix_new", "info", "Создан префикс Wine", prefix))
	checks.append(_chk("wineprefix", "ok", "Префикс Wine", prefix))
	out = []
	var rc := OS.execute(wine, [wpy, "-c", "import frida, websockets; print('HOTA_PY_OK', frida.__version__, websockets.__version__)"], out, true)
	var txt := "".join(out)
	if rc != 0 or txt.find("HOTA_PY_OK") < 0:
		checks.append(_chk("wine_python", "fail", "Python под Wine не запускается или нет frida", txt.strip_edges().right(200),
			"Нужен Wine 8+ (WineHQ stable); подробнее: ./doctor.sh"))
		return r
	checks.append(_chk("wine_python", "ok", "Windows-Python в Wine",
		"frida, websockets " + txt.substr(txt.find("HOTA_PY_OK") + 11).strip_edges() + ": " + wpy))
	var srv := TCPServer.new()
	var p := port
	while srv.listen(p, "127.0.0.1") != OK and p < port + 50:
		p += 1
	srv.stop()
	checks.append(_chk("port", "ok" if p == port else "warn", "Порт %d" % p, "свободен"))
	for c in checks:
		if c.status == "fail":
			return r
	var ctl := _zpath(control_dir)
	var cmd := [wine, wpy, "-I", _zpath(bridge_dir.path_join("bridge.py")), "--game-dir", _zpath(gd), "--port", str(p),
		"--control-dir", ctl, "--log", ctl + "\\bridge.log"]
	cmd += ["--load", load_save] if load_save != "" else ["--menu"]     # default: the game's main menu (PLAN 4.5)
	var vis := game_window == "on" or (game_window == "auto" and (OS.get_environment("DISPLAY") != ""
		or OS.get_environment("WAYLAND_DISPLAY") != ""))
	if vis:
		cmd.append("--visible")      # the game window on the desktop too (PLAN 4.6)
	r["visible"] = vis
	r.ok = true
	r.game_dir = gd
	r.port = p
	r.launch = {"cmd": cmd, "env": {"WINEPREFIX": prefix, "WINEDEBUG": "-all"}, "url": "ws://127.0.0.1:%d" % p,
		"wine": true, "kill": [wine, "taskkill", "/F", "/PID"]}
	return r


func _py_ok(exe: String, pre: Array = []) -> String:
	## -> sys.executable of a working interpreter ("" if it does not run); `pre` e.g. ["-3"] for py.exe
	var out := []
	var rc := OS.execute(exe, pre + ["-c", "import sys; print('PYEXE=' + sys.executable)"], out, true)
	if rc != 0:
		return ""
	var s := "".join(out)
	var i := s.find("PYEXE=")
	return s.substr(i + 6).strip_edges().split("\n")[0].strip_edges() if i >= 0 else ""


func _has_modules(exe: String) -> bool:
	return OS.execute(exe, ["-c", "import frida, websockets"], [], true) == 0


func _find_python(saved: String) -> String:
	if OS.get_name() != "Windows":
		# Linux: host python3 only runs doctor.py; the bridge itself runs under the Windows Python in Wine
		for c in [saved, OS.get_environment("HOTA3D_PYTHON"), "python3", "python"]:
			if c != "" and _py_ok(c) != "":
				return c
		return ""
	var cands: Array[String] = []
	for c in [saved, OS.get_environment("HOTA3D_PYTHON")]:
		if c != "":
			cands.append(c)
	var base := OS.get_environment("LOCALAPPDATA").path_join("Programs/Python")
	if DirAccess.dir_exists_absolute(base):
		var vs := Array(DirAccess.get_directories_at(base)).filter(func(n): return String(n).begins_with("Python3"))
		vs.sort_custom(func(x, y): return int(String(x).substr(7)) > int(String(y).substr(7)))
		for v in vs:
			cands.append(base.path_join(v).path_join("python.exe"))
	var exes: Array[String] = []
	for c in cands:
		var e := _py_ok(c)
		if e != "" and not e in exes:
			exes.append(e)
	for pair in [["py", ["-3"]], ["python", []]]:
		var e := _py_ok(pair[0], pair[1])
		if e != "" and not e in exes:
			exes.append(e)
	var bundled := _bundled_python()          # the package's own Python (has frida + websockets): after a system
	if bundled != "" and _py_ok(bundled) != "":  # Python that has the modules, before one that lacks them
		exes.append(bundled)
	for e in exes:                           # prefer one that already has frida + websockets
		if _has_modules(e):
			return e
	return exes[0] if not exes.is_empty() else ""


## Run the checks (in a thread) and, if nothing failed, start the bridge.
func start() -> void:
	if _thread != null:
		return
	stop_bridge()
	_set_state("checking")
	_show()
	report = {}
	_render()
	var gd := _known_game_dir()
	var explicit := gd != "" and (_picked or gd == _cli_dir)
	_thread = Thread.new()
	_thread.start(_check_work.bind(gd, python, explicit))


func _process(delta: float) -> void:
	if _thread != null and not _thread.is_alive():
		var res: Dictionary = _thread.wait_to_finish()
		_thread = null
		_on_checked(res)
	if bridge_pid > 0:
		_hb -= delta
		if _hb <= 0.0:
			_hb = HEARTBEAT_SEC
			var f := FileAccess.open(control_dir.path_join("heartbeat"), FileAccess.WRITE)
			if f:
				f.store_string(str(Time.get_unix_time_from_system()))
				f.close()
		_poll -= delta
		if _poll <= 0.0:
			_poll = 1.0
			_poll_bridge()


func _local_checks(py: String) -> Array:
	## when doctor.py could not run: what the client can tell by itself
	var out := []
	out.append({"status": "ok", "title": "ОС", "detail": OS.get_name() + " " + OS.get_version(), "fix": ""})
	var miss := missing_files(game_dir)
	if game_dir == "":
		out.append({"id": "game_dir", "status": "fail", "title": "Папка игры не указана", "detail": "",
			"fix": "Кнопка «Папка игры…»: папка с h3hota.exe, HotA.dll и Data\\*.lod"})
	elif not miss.is_empty():
		out.append({"id": "game_dir", "status": "fail", "title": "Папка игры: не хватает файлов",
			"detail": "%s: нет %s" % [game_dir, ", ".join(miss)], "fix": "Укажите папку HotA (кнопка «Папка игры…»)"})
	else:
		out.append({"id": "game_dir", "status": "ok", "title": "Папка игры", "detail": game_dir, "fix": ""})
	if bridge_dir == "":
		out.append({"status": "fail", "title": "Нет файлов моста (bridge/)",
			"detail": "искал рядом с программой: " + OS.get_executable_path().get_base_dir().path_join("bridge"),
			"fix": "Распакуйте пакет клиента целиком: папка bridge/ должна лежать рядом с HotA3D"})
	if py == "":
		if OS.get_name() == "Windows":
			out.append({"status": "fail", "title": "Python не найден",
				"detail": "искал: py -3, python, %LOCALAPPDATA%\\Programs\\Python, HOTA3D_PYTHON",
				"fix": "Поставьте Python 3.12 (64-бит) с python.org, затем: py -m pip install \"frida>=17\" \"websockets>=13\""})
		else:
			out.append({"status": "fail", "title": "python3 не найден (нужен для проверки и запуска моста)",
				"detail": "", "fix": "sudo apt install python3  (Fedora: sudo dnf install python3; Arch: sudo pacman -S python)"})
	return out


func _on_checked(res: Dictionary) -> void:
	if not active:
		return
	report = res.report
	if res.python != "" and OS.get_name() == "Windows":
		python = res.python
	if report.is_empty():
		report = {"ok": false, "checks": _local_checks(res.python)}
		if res.python != "" and bridge_dir != "":
			report.checks.append({"status": "fail", "title": "doctor.py не отработал",
				"detail": String(res.get("out", "")).strip_edges().right(300), "fix": "Запустите вручную: python bridge/doctor.py"})
	elif report.get("game_dir") is String and String(report.game_dir) != "":
		if String(report.game_dir) != game_dir:
			print("local: game folder: ", report.game_dir)
		game_dir = String(report.game_dir)      # found by the search (or confirmed): remember it
		_save_cfg()
	if report.get("port") != null:
		port = int(report.get("port", port))
	_print_report()
	var game_bad := false
	for c in report.get("checks", []):
		if String(c.get("id", "")) == "game_dir" and c.status == "fail":
			game_bad = true
	if report.get("ok", false) and report.get("launch") is Dictionary:
		_launch(report.launch)
	else:
		_set_state("need_folder" if game_bad else "blocked")
		if game_bad and not _asked_dir and not _picked and _cli_dir == "" and not _no_ui and _shot_path == "":
			_asked_dir = true                # first start, nothing found: ask for the folder right away
			open_dialog.call_deferred()
	_render()


func _print_report() -> void:
	print("local: checklist (%s)" % ("ok" if report.get("ok", false) else "something missing"))
	for c in report.get("checks", []):
		var tag: String = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[INFO]"}.get(c.status, "[ ?? ]")
		print("  %s %s%s" % [tag, c.title, (": " + String(c.detail)) if String(c.get("detail", "")) != "" else ""])
		if String(c.get("fix", "")) != "" and c.status in ["fail", "warn"]:
			print("         -> ", c.fix)


# ============================================================================================ bridge process

func _launch(L: Dictionary) -> void:
	launch = L
	DirAccess.make_dir_recursive_absolute(control_dir)
	for n in ["stop", "status.json"]:
		DirAccess.remove_absolute(control_dir.path_join(n))
	var lf := FileAccess.open(control_dir.path_join("bridge.log"), FileAccess.WRITE)   # fresh log per run
	if lf:
		lf.close()
	_hb = 0.0
	_status = {}
	var env: Dictionary = L.get("env", {})
	for k in env:
		OS.set_environment(String(k), String(env[k]))
	var cmd: Array = L.cmd
	var argv := PackedStringArray()
	for x in cmd.slice(1):
		argv.append(String(x))
	print("local: start bridge: ", " ".join(PackedStringArray(cmd.map(func(x): return String(x)))))
	bridge_pid = OS.create_process(String(cmd[0]), argv, false)
	if bridge_pid <= 0:
		bridge_pid = -1
		_fail("Не удалось запустить: %s" % cmd[0])
		return
	port = int(String(L.url).get_slice(":", 2))
	var net := _net()
	if net != null:
		net.reconnect(String(L.url))
	_set_state("starting")
	_poll = 1.0


func _read_status() -> Dictionary:
	var t := FileAccess.get_file_as_string(control_dir.path_join("status.json"))
	var j = JSON.parse_string(t) if t != "" else null
	return j if j is Dictionary else {}


func _poll_bridge() -> void:
	var st := _read_status()
	if not st.is_empty():
		_status = st
	var s := String(_status.get("state", ""))
	var net := _net()
	if s == "error":
		_fail("Мост остановился с ошибкой: %s" % _status.get("error", "?"))
		return
	if game_quit and (s == "exited" or not OS.is_process_running(bridge_pid)):
		print("local: the game was quit from its menu, bridge ", "exited" if s == "exited" else "gone")
		bridge_pid = -1
		_set_state("exited")
		return
	if not OS.is_process_running(bridge_pid):
		_fail("Мост завершился (код состояния: %s)" % (s if s != "" else "нет status.json"))
		return
	if state == "starting":
		if s == "ready" and net != null and net.status == "offline":
			net.reconnect(String(launch.url))
		if net != null and net.status == "online":
			_set_state("running")
			print("local: bridge online at ", launch.url)
			panel.visible = false
		else:
			_render()


func _log_tail(n := 14) -> String:
	var t := FileAccess.get_file_as_string(control_dir.path_join("bridge.log"))
	var lines := t.strip_edges().split("\n")
	return "\n".join(lines.slice(maxi(0, lines.size() - n)))


func _fail(msg: String) -> void:
	print("local: ", msg)
	var tail := _log_tail()
	if tail != "":
		print("local: bridge.log:\n", tail)
	_kill_rest()
	_status["fail"] = msg
	_set_state("failed")
	_show()
	_render()


## main.gd: the bridge reported `phase exited` (the player chose "quit" in the game's menu); the bridge and the game
## end by themselves, which is then not reported as a failure.
func on_game_quit() -> void:
	game_quit = true


## Stop the bridge this client started (and the game it started): stop file → wait → kill.
func stop_bridge() -> void:
	if bridge_pid <= 0:
		return
	var f := FileAccess.open(control_dir.path_join("stop"), FileAccess.WRITE)
	if f:
		f.store_string("stop")
		f.close()
	var t0 := Time.get_ticks_msec()
	while OS.is_process_running(bridge_pid) and Time.get_ticks_msec() - t0 < 8000:
		OS.delay_msec(200)
	var clean := not OS.is_process_running(bridge_pid)
	print("local: bridge stopped (%s)" % ("clean" if clean else "killed"))
	_kill_rest()


func _kill_rest() -> void:
	if bridge_pid > 0 and OS.is_process_running(bridge_pid):
		OS.kill(bridge_pid)
	var st := _read_status()
	var gp := int(st.get("game_pid", 0))
	var clean := String(st.get("state", "")) in ["exited"]
	if gp > 0 and not clean:
		# the bridge did not get to kill its game: do it here (Windows pid; under Wine via wine taskkill)
		if launch.get("wine", false):
			var k: Array = launch.get("kill", [])
			if not k.is_empty():
				OS.execute(String(k[0]), PackedStringArray(k.slice(1).map(func(x): return String(x))) + PackedStringArray([str(gp)]), [], true)
		else:
			OS.execute("taskkill", ["/F", "/PID", str(gp)], [], true)
	bridge_pid = -1


func _exit_tree() -> void:
	if _thread != null:
		_thread.wait_to_finish()
		_thread = null
	stop_bridge()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_CRASH:
		stop_bridge()


# ============================================================================================ UI

func _set_state(s: String) -> void:
	state = s
	state_changed.emit(s)


func _show() -> void:
	if not _no_ui:
		panel.visible = true


func _build() -> void:
	var root := Control.new()
	root.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var cp := get_node_or_null("/root/ConnectPanel")
	if cp != null and cp.has_method("_theme"):
		root.theme = cp._theme()
	add_child(root)
	panel = PanelContainer.new()
	panel.set_anchors_preset(Control.PRESET_CENTER)
	panel.grow_horizontal = Control.GROW_DIRECTION_BOTH
	panel.grow_vertical = Control.GROW_DIRECTION_BOTH
	panel.visible = false
	root.add_child(panel)
	var vb := VBoxContainer.new()
	vb.add_theme_constant_override("separation", 8)
	panel.add_child(vb)
	title_label = Label.new()
	title_label.text = "Локальная игра: оригинальная HotA на этом компьютере"
	vb.add_child(title_label)
	status_label = Label.new()
	status_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	status_label.custom_minimum_size = Vector2(860, 0)
	status_label.add_theme_color_override("font_color", Color(0.85, 0.8, 0.68))
	status_label.add_theme_font_size_override("font_size", 15)
	vb.add_child(status_label)
	list = RichTextLabel.new()
	list.bbcode_enabled = true
	list.fit_content = true
	list.scroll_active = true
	list.custom_minimum_size = Vector2(860, 120)
	list.add_theme_font_size_override("normal_font_size", 14)
	list.add_theme_font_size_override("bold_font_size", 14)
	list.selection_enabled = true
	vb.add_child(list)
	var hb := HBoxContainer.new()
	hb.add_theme_constant_override("separation", 8)
	vb.add_child(hb)
	btn_dir = Button.new()
	btn_dir.text = "Папка игры…"
	btn_dir.tooltip_text = "Выбрать папку с h3hota.exe (Heroes 3 Complete + HotA 1.8)"
	btn_dir.pressed.connect(open_dialog)
	hb.add_child(btn_dir)
	btn_retry = Button.new()
	btn_retry.text = "Проверить и запустить"
	btn_retry.pressed.connect(start)
	hb.add_child(btn_retry)
	btn_remote = Button.new()
	btn_remote.text = "Удалённый мост…"
	btn_remote.tooltip_text = "Не запускать игру здесь, а подключиться к мосту на другой машине"
	btn_remote.pressed.connect(func():
		disable()
		var c := get_node_or_null("/root/ConnectPanel")
		if c != null:
			c._show())
	hb.add_child(btn_remote)
	chk_window = CheckBox.new()
	chk_window.text = "Окно игры на экране"
	chk_window.tooltip_text = ("Оригинальная игра открыта своим окном рядом с клиентом: играть можно и там, и здесь, "
		+ "клиент повторяет всё, что сделано в окне игры. Выключено — игра работает скрыто. "
		+ "Применяется при следующем запуске («Проверить и запустить»).")
	chk_window.toggled.connect(_on_window_toggled)
	hb.add_child(chk_window)
	var close := Button.new()
	close.text = "Скрыть (F3)"
	close.pressed.connect(func(): panel.visible = false)
	hb.add_child(close)
	var hint := Label.new()
	hint.add_theme_font_size_override("font_size", 13)
	hint.add_theme_color_override("font_color", Color(0.7, 0.66, 0.56))
	hint.text = "F3 — эта панель · F2 — адрес удалённого моста · то же из консоли: python bridge/doctor.py"
	vb.add_child(hint)
	dialog = FileDialog.new()
	dialog.file_mode = FileDialog.FILE_MODE_OPEN_DIR
	dialog.access = FileDialog.ACCESS_FILESYSTEM
	dialog.title = "Папка оригинальной игры (где лежит h3hota.exe)"
	dialog.use_native_dialog = not _shot_dialog
	dialog.size = Vector2i(900, 560)
	dialog.dir_selected.connect(_on_dir_selected)
	add_child(dialog)


func _on_window_toggled(on: bool) -> void:
	if _syncing_check:
		return
	game_window = "on" if on else "off"
	_save_cfg()
	print("local: game window ", game_window, " (next start)")
	_render()


var _syncing_check := false

## The check box shows what the running / next start uses: doctor.py's decision for "auto".
func _window_visible() -> bool:
	if report.has("visible"):
		return bool(report.visible)
	return game_window != "off"


func open_dialog() -> void:
	if game_dir != "" and DirAccess.dir_exists_absolute(game_dir):
		dialog.current_dir = game_dir
	elif OS.get_name() == "Windows":
		dialog.current_dir = "C:/"
	else:
		dialog.current_dir = OS.get_environment("HOME")
	dialog.popup_centered()


func _on_dir_selected(d: String) -> void:
	var miss := missing_files(d)
	if not miss.is_empty() and missing_files(d.path_join("..")).is_empty():
		d = d.path_join("..").simplify_path()     # picked Data/ or games/ inside the game folder
		miss = []
	print("local: folder chosen: %s (missing: %s)" % [d, miss])
	game_dir = d
	_picked = true
	if miss.is_empty():
		_save_cfg()
	start()


func _render() -> void:
	var t := ""
	match state:
		"checking":
			status_label.text = "Проверяю, всё ли есть для запуска игры…"
		"need_folder":
			status_label.text = ("Не найдена папка оригинальной игры. Нужна установленная Heroes of Might and Magic III "
				+ "Complete с HotA 1.8: в папке должны быть %s. Нажмите «Папка игры…» и укажите её — выбор запомнится."
				% ", ".join(REQUIRED))
		"blocked":
			status_label.text = "Для запуска не хватает того, что отмечено ✘ — исправьте и нажмите «Проверить и запустить»."
		"starting":
			var s := String(_status.get("state", "starting"))
			status_label.text = "Запускаю игру и мост (%s)… %s" % [
				{"starting": "старт моста", "game": "игра загружена, мост поднимается", "ready": "мост готов, подключаюсь"}.get(s, s),
				launch.get("url", "")]
		"failed":
			status_label.text = String(_status.get("fail", "Ошибка запуска")) + " — журнал: " + control_dir.path_join("bridge.log")
		"running":
			status_label.text = "Работает: игра и мост запущены этим клиентом (%s), остановятся при выходе." % launch.get("url", "")
		_:
			status_label.text = "Локальный режим выключен."
	for c in report.get("checks", []):
		t += "%s [b]%s[/b]" % [MARK.get(c.status, "?"), _esc(c.title)]
		if String(c.get("detail", "")) != "":
			t += " — " + _esc(c.detail)
		t += "\n"
		if String(c.get("fix", "")) != "" and c.status in ["fail", "warn"]:
			t += "      [color=#e8d8a8]→ %s[/color]\n" % _esc(c.fix)
	if state == "failed":
		var tail := _log_tail(8)
		if tail != "":
			t += "\n[color=#b0a890]bridge.log:\n%s[/color]" % _esc(tail)
	if chk_window != null:
		_syncing_check = true
		chk_window.button_pressed = _window_visible() if game_window == "auto" else game_window == "on"
		_syncing_check = false
		if state == "running" and bool(report.get("visible", false)) != chk_window.button_pressed:
			t += "\n[color=#e8d8a8]Окно игры: изменится при следующем запуске («Проверить и запустить»).[/color]\n"
	list.text = t
	btn_retry.disabled = state in ["checking", "starting"]
	btn_dir.disabled = state == "checking"
	if _shot_path != "" and state != "checking":
		var p := _shot_path
		_shot_path = ""
		_debug_shot.call_deferred(p)


static func _esc(v) -> String:
	return String(v).replace("[", "[lb]")


func _debug_shot(path: String) -> void:
	if _shot_dialog:
		open_dialog()
	await get_tree().create_timer(3.0).timeout
	await RenderingServer.frame_post_draw
	var img := get_viewport().get_texture().get_image()
	print("local-shot ", path, " ", img.save_png(path))
	get_tree().quit()


func _input(ev: InputEvent) -> void:
	if _no_ui or not (ev is InputEventKey) or not ev.pressed or ev.echo:
		return
	if ev.keycode == KEY_F3:
		if panel.visible:
			panel.visible = false
		else:
			_show()
			_render()
		get_viewport().set_input_as_handled()
