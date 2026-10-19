extends Node
## WebSocket client for PROTOCOL.md: JSON text frames, automatic reconnect, connection status.

signal message(msg: Dictionary)
signal status_changed(status: String, detail: String)   # "connecting" | "online" | "offline"

var url := "ws://127.0.0.1:8765"
var ws := WebSocketPeer.new()
var status := "offline"
var _retry_in := 0.0
var _backoff := 0.5
var _was_open := false


func _ready() -> void:
	ws.inbound_buffer_size = 1 << 24
	ws.outbound_buffer_size = 1 << 20
	connect_now()


func connect_now() -> void:
	ws = WebSocketPeer.new()
	ws.inbound_buffer_size = 1 << 24
	ws.max_queued_packets = 4096
	var err := ws.connect_to_url(url)
	if err != OK:
		_set_status("offline", "error %d" % err)
		_retry_in = _backoff
		return
	_set_status("connecting", url)


## Switch to another bridge address (empty = the same one) and connect right away: the old socket is closed,
## a pending retry and the backoff are dropped; if the link was up, "offline" is emitted first (so the client resets
## the per-connection state) and "online" fires again when the new socket opens.
func reconnect(new_url := "") -> void:
	if new_url != "":
		url = new_url
	if ws.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		ws.close()
	_retry_in = 0.0
	_backoff = 0.5
	if _was_open:
		_was_open = false
		_set_status("offline", "reconnect")
	connect_now()


## Keep the connection alive (answer pings) while the main thread is busy building the world.
## Received frames stay queued in the peer and are handled on the next _process.
func keepalive() -> void:
	if ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		ws.poll()


func send(msg: Dictionary) -> void:
	if ws.get_ready_state() != WebSocketPeer.STATE_OPEN:
		push_warning("net: not connected, dropped %s" % msg.get("t"))
		return
	if msg.get("t") != "window_input":
		print("net -> ", msg.get("t"), " ", msg.get("cmd", ""))
	ws.send_text(JSON.stringify(msg))


func _set_status(s: String, detail: String) -> void:
	if s == status and detail == "":
		return
	status = s
	status_changed.emit(s, detail)


func _process(delta: float) -> void:
	if _retry_in > 0.0:
		_retry_in -= delta
		if _retry_in <= 0.0:
			connect_now()
		return
	ws.poll()
	match ws.get_ready_state():
		WebSocketPeer.STATE_OPEN:
			if not _was_open:
				_was_open = true
				_backoff = 0.5
				_set_status("online", url)
			while ws.get_available_packet_count() > 0:
				var pkt := ws.get_packet()
				if not ws.was_string_packet():
					continue
				var data = JSON.parse_string(pkt.get_string_from_utf8())
				if data is Dictionary:
					if data.get("t") != "window_frame":     # engine window frames come up to 8 per second
						print("net <- ", data.get("t"), " ", data.get("kind", ""))
					message.emit(data)
				else:
					push_warning("net: bad frame")
		WebSocketPeer.STATE_CLOSED:
			var was := _was_open
			_was_open = false
			_backoff = minf(_backoff * 2.0, 5.0) if not was else 0.5
			_retry_in = _backoff
			_set_status("offline", "retry in %.1fs (code %d)" % [_retry_in, ws.get_close_code()])
