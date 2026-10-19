"""WebSocket bridge between the original game (h3hota.exe under Frida) and the Godot client. PROTOCOL.md v0.

    python -I bridge.py                       # start the game (GODOT_TEST.GM1), serve ws://127.0.0.1:8765
    python -I bridge.py --menu                # start at the game's main menu: the player picks any mode (streamed)
    python -I bridge.py --attach              # use an already running game (launch.py --keep)
    python -I bridge.py --host 0.0.0.0 --port 8765 --load OTHER.GM1
    python -I bridge.py --record out/rec      # save every snapshot and the whole traffic (jsonl)
    python -I bridge.py --mock                # no game: mock_server.MockGame (synthetic map)
    python -I bridge.py --mock --replay testdata/snapshot_newcomers.json   # no game: serve a recorded snapshot
    python -I bridge.py --snapshot-only testdata/snapshot_newcomers.json   # record a snapshot (+ objects tsv), exit
    python -I bridge.py --battles manual      # the human's battles on the engine's battle screen (passthrough)
    python -I bridge.py --menu --visible      # the game window on the desktop too: play in it and in the client
    python -I bridge.py --windows 'townScreen=close,heroMeeting=skip'   # per-window policy (README.md)
    python -I bridge.py --game-dir D:/Games/HotA     # another game folder (default: env HOTA_GAME_DIR, else repo game/)
    python -I bridge.py --control-dir DIR --log DIR/bridge.log   # started by the client (local mode): DIR/status.json,
                                              # exits when DIR/stop appears or DIR/heartbeat gets stale

The engine computes everything: the agent (game_agent.js + bridge_agent.js + bridge_runtime.js) runs commands on
the game thread at the place the original buttons do, forwards NormalDialog questions here and waits for the
answer, reports hero moves / battles / turns. Engine windows the client does not draw itself (town screen, hero
screen, meeting, market, battle screen ...) stay open for a client that announced caps ["windows"]: this module
streams the engine frame (window_frame, JPEG) and relays the client's mouse/keys (window_input). Outside the
adventure map (main menu, scenario / load / campaign screens, high scores; `--menu` starts there) the whole engine
screen is streamed the same way and `phase` messages tell the client when a map is entered (then a new snapshot)
or left. It also turns full state reads into `state` diffs. With --visible (PLAN 4.6) the game window is a normal
window on the desktop: engine dialogs and windows run in it and go to the client as well, and what the player does
directly in the game window comes to the client as `state` with why "external" (mirror). See README.md.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROTO = 0
AGENT_EXTRA = ('bridge_agent.js', 'bridge_runtime.js')
CMD_TIMEOUT = {'path': 30, 'select_hero': 30, 'move_hero': 900, 'end_turn': 900, 'open_town': 60, 'open_hero': 60,
               'adv_key': 60, 'system_menu': 60, 'adventure_options': 60}   # extended while a window is open
WINDOW_CMDS = ('open_town', 'open_hero', 'adv_key', 'system_menu', 'adventure_options')   # need caps "windows"


def jdump(o):
    return json.dumps(o, ensure_ascii=False, separators=(',', ':'))


def ensure_save(name):
    """games/<name> missing but testdata/<name> exists (GODOT_TEST.GM1, GODOT_BATTLE.GM1): copy it over."""
    import shutil
    from launch import GAME_DIR
    dst = os.path.join(GAME_DIR, 'games', name)
    src = os.path.join(HERE, 'testdata', name)
    if not os.path.exists(dst) and os.path.exists(src):
        shutil.copy2(src, dst)


class Control:
    """--control-dir: the client that started this bridge (local mode) talks to it through files, which works the
    same for a native Windows process and for one under Wine (no pids/signals across the Wine boundary):
      status.json  written here: {"state": starting|game|ready|error|exited, "pid", "game_pid", "url", "error", "t"}
      stop         created by the client: exit cleanly (the game this bridge started is killed, registry restored)
      heartbeat    rewritten by the client every few seconds: if it exists and is older than --heartbeat-timeout,
                   the client is gone (crashed) -> exit as on stop"""

    def __init__(self, path, heartbeat_timeout=900):
        self.dir = path
        self.hb_timeout = heartbeat_timeout
        self.st = {'pid': os.getpid()}
        self.lock = threading.Lock()
        if path:
            os.makedirs(path, exist_ok=True)
            try:
                os.remove(os.path.join(path, 'stop'))
            except FileNotFoundError:
                pass

    def status(self, state, **kw):
        if not self.dir:
            return
        with self.lock:
            self.st.update(kw, state=state, t=time.time())
            tmp = os.path.join(self.dir, 'status.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.st, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(self.dir, 'status.json'))

    def stop_reason(self):
        if not self.dir:
            return None
        if os.path.exists(os.path.join(self.dir, 'stop')):
            return 'stop file'
        try:
            age = time.time() - os.path.getmtime(os.path.join(self.dir, 'heartbeat'))
        except OSError:
            return None
        return f'client heartbeat {age:.0f}s old' if self.hb_timeout > 0 and age > self.hb_timeout else None

    def watch_startup(self):
        """Until the bridge is ready (the game loads in a blocking call), a stop request kills the spawned game and
        exits the process right away; after 'ready' the serve loop handles stop cleanly."""
        if not self.dir:
            return

        def run():
            while self.st.get('state') in ('starting', 'game'):
                why = self.stop_reason()
                if why:
                    print(f'stopping during startup: {why}', flush=True)
                    import launch
                    if self.st.get('game_pid'):
                        launch.kill_pid(self.st['game_pid'])
                        time.sleep(0.5)
                    launch.restore_registry()
                    self.status('exited', error='stopped during startup')
                    os._exit(0)
                time.sleep(0.5)
        threading.Thread(target=run, daemon=True).start()


class Recorder:
    def __init__(self, path):
        self.dir = path
        self.n = 0
        if path:
            os.makedirs(path, exist_ok=True)
            self.f = open(os.path.join(path, 'traffic.jsonl'), 'a', encoding='utf-8')

    def log(self, direction, msg):
        if not self.dir:
            return
        self.f.write(jdump({'ts': round(time.time(), 3), 'dir': direction, 'msg': msg}) + '\n')
        self.f.flush()
        if msg.get('t') == 'snapshot':
            self.n += 1
            with open(os.path.join(self.dir, f'snapshot_{self.n:03d}.json'), 'w', encoding='utf-8') as f:
                f.write(jdump(msg))


# ----------------------------------------------------------------------------------------------- engine

# DIK scan codes (set 1) for window_input / adv_key key names; numbers are Windows virtual-key codes
SCAN = {'escape': 0x01, 'esc': 0x01, 'minus': 0x0C, '-': 0x0C, 'equal': 0x0D, '=': 0x0D, 'backspace': 0x0E,
        'tab': 0x0F, '[': 0x1A, ']': 0x1B, 'enter': 0x1C, 'return': 0x1C, 'ctrl': 0x1D, 'control': 0x1D,
        ';': 0x27, "'": 0x28, '`': 0x29, 'shift': 0x2A, '\\': 0x2B, ',': 0x33, '.': 0x34, '/': 0x35, 'rshift': 0x36,
        'kp_multiply': 0x37, 'alt': 0x38, 'space': 0x39, ' ': 0x39, 'capslock': 0x3A, 'numlock': 0x45,
        'scrolllock': 0x46, 'home': 0x47, 'up': 0x48, 'pageup': 0x49, 'kp_minus': 0x4A, 'left': 0x4B, 'kp_5': 0x4C,
        'right': 0x4D, 'kp_plus': 0x4E, 'end': 0x4F, 'down': 0x50, 'pagedown': 0x51, 'insert': 0x52,
        'delete': 0x53, 'f11': 0x57, 'f12': 0x58}
SCAN.update({str(d): 0x02 + (d - 1) for d in range(1, 10)})
SCAN['0'] = 0x0B
for _row, _base in (('qwertyuiop', 0x10), ('asdfghjkl', 0x1E), ('zxcvbnm', 0x2C)):
    SCAN.update({ch: _base + i for i, ch in enumerate(_row)})
SCAN.update({f'f{i}': 0x3A + i for i in range(1, 11)})


def key_scan(key):
    """window_input/adv_key `key`: name ('Enter', 'Esc', 'A', 'F1', 'Left' ...) or Windows VK code -> DIK scan code."""
    if isinstance(key, bool) or key is None:
        raise ValueError(f'bad key {key!r}')
    if isinstance(key, (int, float)):
        import ctypes
        sc = ctypes.windll.user32.MapVirtualKeyW(int(key), 0)    # MAPVK_VK_TO_VSC
        if not sc:
            raise ValueError(f'no scan code for VK {key}')
        return sc
    k = str(key).strip().lower()
    if k in SCAN:
        return SCAN[k]
    if k.startswith('scan:'):
        return int(k[5:], 0)
    raise ValueError(f'unknown key {key!r}')


FRAME = object()   # queue marker: send the client's latest window_frame


class Conn:
    def __init__(self, ws):
        self.ws = ws
        self.q = asyncio.Queue()
        self.caps = set()
        self.frame = None
        self.frame_queued = False


class EngineBridge:
    """Relays between WebSocket clients and the game agent."""

    def __init__(self, game, loop, log=print, record=None, dialog_timeout=120, battles='auto', windows=None,
                 fps=8.0, jpeg_quality=80, window_timeout=0, debug=False, visible=False):
        from snapinfo import diff_objects, fog_opened
        from frames import FrameEncoder
        self._diff_objects, self._fog_opened = diff_objects, fog_opened
        self.g, self.loop, self.log = game, loop, log
        self.api = game.api
        self.rec = record or Recorder(None)
        self.clients = {}            # ws -> Conn
        self.pending = {}            # cmd id -> Future
        self.early = {}              # cmd_done that arrived before the future was registered
        self.objs = {}               # object id -> object as last sent
        self.fog = None
        self.size = 0
        self.lock = asyncio.Lock()   # one engine command at a time from this side
        # window passthrough
        self.pt = []                 # window_open messages of the open passthrough windows (outermost first)
        self.frame_task = None
        self.frame_period = 1.0 / max(0.5, fps)
        self.force_frame = False
        self.frame_seq = 0
        self.enc = FrameEncoder(jpeg_quality)
        self._frame = None
        self._frame_evt = threading.Event()
        self._grab_lock = threading.Lock()
        self.input_q = asyncio.Queue()
        self.input_task = None
        # visible mode: the player can close a window in the game window himself, nobody presses Esc for him
        self.window_timeout = 0 if visible else window_timeout
        self.visible = visible
        self.debug = debug
        self._no_pass_since = None
        self.phase = 'adventure'     # 'menu' | 'adventure' | 'exited' (PLAN 4.5; the agent reports changes)
        game.on_payload = self._on_payload
        r = self.api.bridge_enable({'dialogTimeout': dialog_timeout, 'battles': battles, 'windows': windows or {},
                                    'visible': bool(visible)})
        self.phase = r.get('phase') or 'adventure'
        self.log(f'bridge enabled in the agent: {r}; frames: {self.enc.kind}')
        if self.phase == 'menu':
            loop.call_soon(self._ensure_frames)

    # frida thread -> event loop, order preserved
    def _on_payload(self, p, data):
        if not isinstance(p, dict):
            return False
        t = p.get('t')
        if t == 'frame':             # answer to rpc frame(): RGB565 bytes of the engine screen
            self._frame = (p, data)
            self._frame_evt.set()
            return True
        if t in ('bridge', 'state_raw', 'snapshot_raw', 'cmd_done'):
            self.loop.call_soon_threadsafe(self._agent_msg, p)
            return True
        if t == 'log':
            self.loop.call_soon_threadsafe(self.log, '[game] ' + str(p.get('msg')))
            return True
        if t == 'messagebox':        # a Windows MessageBox of the game (hidden: answered OK by the agent; visible: shown)
            ev = {'t': 'event', 'kind': 'engine_message', 'caption': p.get('caption', ''), 'text': p.get('text', '')}
            if p.get('shown'):
                ev['shown'] = True
            self.loop.call_soon_threadsafe(self.broadcast, ev)
            return True
        return False

    def _agent_msg(self, p):
        t = p['t']
        if t == 'bridge':
            m = p['m']
            mt = m.get('t')
            if mt == 'window_open':
                self.pt.append(m)
                self.force_frame = True
                if self.frame_task is None:
                    self.frame_task = asyncio.ensure_future(self._frame_loop())
            elif mt == 'window_close':
                if self.pt:
                    self.pt.pop()
                if m.get('depth', 1) <= 1:
                    self.pt.clear()
            elif mt == 'phase':
                self.set_phase(m)
                return
            self.broadcast(m)
        elif t == 'state_raw':
            if self.phase == 'adventure':
                self.broadcast(self.state_msg(p['s'], p.get('why')))
        elif t == 'snapshot_raw':     # a map was entered (new game / load / restart / next scenario) or hot-seat view
            snap = p['s']
            snap['source'] = 'engine'
            snap['why'] = p.get('why', '')
            self.remember(snap)
            self.log(f"snapshot ({snap['why']}): map {snap['map']['size']}x{snap['map']['levels']}, "
                     f"{len(snap['objects'])} objects, me {snap.get('me')}")
            self.broadcast(snap)
        elif t == 'cmd_done':
            fut = self.pending.pop(p['id'], None)
            if fut is None:
                self.early[p['id']] = p
            elif not fut.done():
                fut.set_result(p)

    # ---- phase: menu <-> adventure map ----
    def set_phase(self, m):
        old, self.phase = self.phase, m.get('phase', self.phase)
        self.log(f'phase {old} -> {self.phase} {m}')
        if self.phase != 'adventure':
            self.objs, self.fog = {}, None
        self.broadcast(m)
        if self.phase == 'menu':
            self.force_frame = True
            self._ensure_frames()

    def _ensure_frames(self):
        if self.frame_task is None and self.streaming():
            self.frame_task = asyncio.ensure_future(self._frame_loop())

    def streaming(self):
        """Engine frames go to the client: a passthrough window is open or the menus are shown."""
        return bool(self.pt) or self.phase == 'menu'

    # ---- state ----
    def remember(self, snap):
        self.objs = {o['id']: o for o in snap['objects']}
        self.fog = snap['fog']
        self.size = snap['map']['size']

    def state_msg(self, s, why=None):
        new = {o['id']: o for o in s['objects']}
        changed, removed = self._diff_objects(self.objs, new)
        self.objs = new
        fog_open = self._fog_opened(self.fog, s['fog'], self.size) if self.fog is not None else []
        self.fog = s['fog']
        m = {'t': 'state', 'objects_changed': changed, 'objects_removed': removed, 'heroes': s['heroes'],
             'towns': s['towns'], 'players': s['players'], 'date': s['date'], 'current': s['current'],
             'fog_open': fog_open}
        if 'selected' in s:
            m['selected'] = s['selected']
        if why:
            m['why'] = why          # move_hero | end_turn | turn | window_open | window_close | external (visible mode)
        return m

    async def snapshot(self):
        """Full snapshot read on the game thread (between two input polls); direct read as a fallback."""
        def read():
            try:
                return self.g.run_job(self.api.snapshot_job(), timeout=10)
            except TimeoutError:
                return self.api.snapshot_now()
        snap = json.loads(await asyncio.to_thread(read))
        snap['source'] = 'engine'
        self.remember(snap)
        return snap

    # ---- clients ----
    def broadcast(self, msg):
        self.rec.log('out', msg)
        for c in self.clients.values():
            c.q.put_nowait(msg)

    def send(self, ws, msg):
        self.rec.log('out', msg)
        c = self.clients.get(ws)
        if c is not None:
            c.q.put_nowait(msg)

    def pass_clients(self):
        return [c for c in self.clients.values() if 'windows' in c.caps]

    async def update_passthrough(self):
        on = bool(self.pass_clients())
        await asyncio.to_thread(self.api.set_passthrough, on)
        self._no_pass_since = None if on else time.monotonic()

    # ---- window passthrough: frames out, input in ----
    def _capture(self, force):
        """rpc frame() -> (info, RGB565 bytes) via a 'frame' message; None when unchanged or not available."""
        self._frame_evt.clear()
        self._frame = None
        r = self.api.frame(force)
        if not r or r.get('same'):
            return None
        if not self._frame_evt.wait(3) or self._frame is None:
            return None
        return self._frame

    def _grab(self, force):
        """Engine frame -> JPEG window_frame; None when unchanged."""
        with self._grab_lock:
            got = self._capture(force)
            if got is None:
                return None
            p, data = got
            fmt, img = self.enc.encode(data, p['w'], p['h'], p['pitch'])
            self.frame_seq += 1
            return {'t': 'window_frame', 'fmt': fmt, 'w': p['w'], 'h': p['h'], 'seq': self.frame_seq,
                    'name': self.pt[-1]['name'] if self.pt else ('menu' if self.phase == 'menu' else None),
                    'data': base64.b64encode(img).decode('ascii')}

    def _shot_jpeg(self, path):
        """debug_shot to a .jpg: the same frame grab as window_frame, written to a file."""
        with self._grab_lock:
            got = self._capture(True)
        self.force_frame = True
        if got is None:
            return False
        p, data = got
        fmt, img = self.enc.encode(data, p['w'], p['h'], p['pitch'])
        if fmt != 'jpg':
            return False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(img)
        return True

    def send_frame(self, fr):
        self.rec.log('out', {k: (len(v) if k == 'data' else v) for k, v in fr.items()})
        for c in self.pass_clients():
            c.frame = fr                    # a slow client only gets the newest frame
            if not c.frame_queued:
                c.frame_queued = True
                c.q.put_nowait(FRAME)

    async def _frame_loop(self):
        """While a passthrough window is open or the menus are shown: send the engine frame when it changed, at most
        `fps` times a second."""
        try:
            await asyncio.sleep(0.03)
            last_abort = 0.0
            while self.streaming():
                t0 = time.monotonic()
                if self.pass_clients():
                    force, self.force_frame = self.force_frame, False
                    try:
                        fr = await asyncio.to_thread(self._grab, force)
                    except Exception as e:
                        self.log(f'frame: {type(e).__name__}: {e}')
                        fr = None
                    if fr is not None and self.streaming():
                        self.send_frame(fr)
                elif self.pt and self.window_timeout and self._no_pass_since is not None and \
                        t0 - self._no_pass_since > self.window_timeout and t0 - last_abort > 2:
                    # nobody to play the window for a long time: close it the way a player would (Esc)
                    last_abort = t0
                    top = self.pt[-1]['name'] if self.pt else ''
                    self.log(f'window {top}: no passthrough client for {self.window_timeout}s, pressing Esc')
                    await asyncio.to_thread(self.api.window_input, {'kind': 'key', 'scan': 1, 'action': 'press'})
                await asyncio.sleep(max(0.01, self.frame_period - (time.monotonic() - t0)))
        finally:
            self.frame_task = None

    def input_msg(self, msg):
        kind = msg.get('kind')
        o = {'kind': kind, 'action': str(msg.get('action', 'press' if kind == 'key' else 'move'))}
        for k in ('shift', 'ctrl', 'alt'):
            if msg.get(k):
                o[k] = True
        if kind == 'mouse':
            o['x'], o['y'] = int(msg['x']), int(msg['y'])
            o['button'] = str(msg.get('button', 'left'))
            if o['action'] not in ('move', 'down', 'up', 'click') or o['button'] not in ('left', 'right'):
                raise ValueError('mouse: action move|down|up|click, button left|right')
        elif kind == 'key':
            o['scan'] = int(msg['scan']) if 'scan' in msg else key_scan(msg.get('key'))
            if o['action'] not in ('down', 'up', 'press'):
                raise ValueError('key: action down|up|press')
        else:
            raise ValueError('window_input kind must be mouse or key')
        return o

    async def _input_loop(self):
        """Client input goes to the agent in order; consecutive mouse moves collapse into the last one."""
        while True:
            batch = [await self.input_q.get()]
            while not self.input_q.empty():
                batch.append(self.input_q.get_nowait())
            out = [o for i, o in enumerate(batch) if not (o['kind'] == 'mouse' and o['action'] == 'move' and
                   i + 1 < len(batch) and batch[i + 1]['kind'] == 'mouse' and batch[i + 1]['action'] == 'move')]
            try:
                await asyncio.to_thread(self.api.window_input, out)
            except Exception as e:
                self.log(f'window_input: {type(e).__name__}: {e}')
            self.force_frame = self.force_frame or any(o['action'] != 'move' for o in out)

    async def command(self, c):
        """Queue a command in the agent and wait for cmd_done (the engine runs it on the human's turn). A command
        that opened a passthrough window (move into a town, open_town ...) finishes when the window closes."""
        async with self.lock:
            cid = await asyncio.to_thread(self.api.cmd, c)
            fut = self.loop.create_future()
            if cid in self.early:
                fut.set_result(self.early.pop(cid))
            else:
                self.pending[cid] = fut
            deadline = time.monotonic() + CMD_TIMEOUT.get(c['cmd'], 120)
            while True:
                now = time.monotonic()
                if self.pt:
                    deadline = max(deadline, now + 30)
                if now >= deadline:
                    self.pending.pop(cid, None)
                    q = await asyncio.to_thread(self.api.queue)
                    return {'error': f"{c['cmd']}: engine did not run the command in time (queue {q})"}
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), min(5.0, deadline - now))
                except asyncio.TimeoutError:
                    pass

    async def phase_msg(self):
        if self.phase == 'exited':
            return {'t': 'phase', 'phase': 'exited'}
        return await asyncio.to_thread(self.api.phase)

    async def handle(self, ws, msg):
        t = msg.get('t')
        if t in ('get_snapshot', 'path', 'cmd') and self.phase != 'adventure':
            # no adventure map (main menu, load screen ...): the client shows the streamed screen meanwhile
            if t == 'get_snapshot':
                self.send(ws, await self.phase_msg())
            else:
                self.send(ws, {'t': 'error', 'msg': f'no adventure map now (phase {self.phase})', 're': msg})
            return
        if t == 'get_snapshot':
            self.send(ws, await self.snapshot())
        elif t == 'hello':               # client hello: {"t":"hello","client":"godot","caps":["windows"]}
            conn = self.clients.get(ws)
            if conn is None:
                return
            conn.caps = set(map(str, msg.get('caps') or []))
            self.log(f"client {msg.get('client', '?')} caps {sorted(conn.caps)}")
            await self.update_passthrough()
            if 'windows' in conn.caps and self.pt:     # (re)connected while a window is open
                for m in self.pt:
                    self.send(ws, m)
                self.force_frame = True
            if 'windows' in conn.caps and self.phase == 'menu':
                self.force_frame = True
                self._ensure_frames()
        elif t == 'path':
            r = await self.command({'cmd': 'path', 'hero': int(msg.get('hero', -1)), 'to': list(msg.get('to') or [])})
            if r.get('error'):
                self.send(ws, {'t': 'error', 'msg': r['error'], 're': msg})
            else:
                res = r['result']
                self.send(ws, {'t': 'path', 'hero': res['hero'], 'to': res['to'], 'cells': res['cells'],
                               'reachable': res['reachable']})
        elif t == 'cmd':
            c = msg.get('cmd')
            if c not in ('move_hero', 'select_hero', 'end_turn') + WINDOW_CMDS:
                self.send(ws, {'t': 'error', 'msg': f'unknown cmd {c}', 're': msg})
                return
            c2 = {'cmd': c}
            if 'hero' in msg:
                c2['hero'] = int(msg['hero'])
            if 'town' in msg:
                c2['town'] = int(msg['town'])
            if 'to' in msg:
                c2['to'] = list(msg['to'])
            if c == 'adv_key':
                c2['scan'] = int(msg['scan']) if 'scan' in msg else key_scan(msg.get('key'))
                for k in ('shift', 'ctrl', 'alt'):
                    if msg.get(k):
                        c2[k] = True
            conn = self.clients.get(ws)
            if c in WINDOW_CMDS and (conn is None or 'windows' not in conn.caps):
                self.send(ws, {'t': 'error', 'msg': f'{c}: send {{"t":"hello","caps":["windows"]}} first '
                               '(the engine window needs a client that shows window_frame)', 're': msg})
                return
            r = await self.command(c2)
            if r.get('error'):
                self.send(ws, {'t': 'error', 'msg': r['error'], 're': msg})
            else:
                self.send(ws, {'t': 'cmd_done', 'cmd': c, 'result': r.get('result')})
        elif t == 'window_input':
            if not self.streaming():
                return                   # the window closed meanwhile: drop
            self.input_q.put_nowait(self.input_msg(msg))
        elif t == 'window_refresh':
            self.force_frame = True
        elif t == 'dialog_answer':
            await asyncio.to_thread(self.api.dialog_answer, int(msg.get('id', -1)), int(msg.get('answer', 0)))
        elif t == 'debug_shot':   # development aid: save the engine's own frame as PNG
            path = msg.get('path') or os.path.join(HERE, 'out', 'shot.png')
            if path.lower().endswith(('.jpg', '.jpeg')):
                ok = await asyncio.to_thread(self._shot_jpeg, path)
            else:
                ok = await asyncio.to_thread(self.g.shot, path)
            self.send(ws, {'t': 'debug', 'what': 'shot', 'path': os.path.abspath(path), 'ok': ok})
        elif t == 'debug_eval' and self.debug:   # development aid (bridge.py --debug): JS in the game agent
            r = await asyncio.to_thread(self.api.debug_eval, str(msg.get('src', '')))
            self.send(ws, {'t': 'debug', 'what': 'eval', 'result': r})
        elif t == 'debug_queue':
            q = await asyncio.to_thread(self.api.queue)
            q['window'] = await asyncio.to_thread(self.api.window_state)
            q['mirror'] = await asyncio.to_thread(self.api.mirror_state)
            q['hwnd'] = await asyncio.to_thread(self.api.hwnd)
            self.send(ws, {'t': 'debug', 'what': 'queue', 'queue': q})
        else:
            self.send(ws, {'t': 'error', 'msg': f'unknown message type {t}'})

    async def serve_client(self, ws):
        conn = Conn(ws)
        self.clients[ws] = conn
        self.log(f'client connected {ws.remote_address}')
        if self.input_task is None:
            self.input_task = asyncio.ensure_future(self._input_loop())
        await asyncio.to_thread(self.api.set_client, True)

        async def writer():
            while True:
                m = await conn.q.get()
                if m is FRAME:
                    m, conn.frame, conn.frame_queued = conn.frame, None, False
                    if m is None:
                        continue
                await ws.send(jdump(m))
        wt = asyncio.create_task(writer())
        try:
            self.send(ws, {'t': 'hello', 'proto': PROTO, 'source': 'engine', 'game': 'HotA 1.8.0 (h3hota.exe)',
                           'caps': ['windows', 'phase'] + (['mirror'] if self.visible else []),
                           'visible': self.visible})
            self.send(ws, await self.phase_msg())
            if self.phase == 'adventure':
                self.send(ws, await self.snapshot())
            else:
                self.force_frame = True
            pend = await asyncio.to_thread(self.api.pending_dialog)
            if pend:
                self.send(ws, pend)
            tasks = set()
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    self.send(ws, {'t': 'error', 'msg': 'bad json'})
                    continue
                self.rec.log('in', msg)
                if msg.get('t') != 'window_input':
                    self.log(f'<- {raw[:200]}')
                # dialog answers must not wait behind the command that opened the dialog
                task = asyncio.create_task(self._safe_handle(ws, msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception as e:
            self.log(f'client error: {type(e).__name__}: {e}')
        finally:
            self.clients.pop(ws, None)
            await self.update_passthrough()
            if not self.clients:
                await asyncio.to_thread(self.api.set_client, False)
            await asyncio.sleep(0.2)
            wt.cancel()
            self.log('client disconnected')

    async def _safe_handle(self, ws, msg):
        try:
            await self.handle(ws, msg)
        except Exception as e:
            self.log(f'handle error: {type(e).__name__}: {e}')
            self.send(ws, {'t': 'error', 'msg': f'{type(e).__name__}: {e}', 're': msg})


def parse_windows(spec):
    """--windows 'townScreen=pass,heroMeeting=skip,*=cancel' -> {name: policy}"""
    out = {}
    for part in (spec or '').split(','):
        if part.strip():
            k, _, v = part.partition('=')
            out[k.strip()] = v.strip()
    return out


async def serve_engine(args):
    from launch import Game
    from websockets.asyncio.server import serve as ws_serve
    import launch
    log = (lambda *_: None) if args.quiet else (lambda *x: print(*x, flush=True))
    ctl = args.control
    started = False
    log(f'game folder {launch.GAME_DIR}')
    launch.ON_SPAWN = lambda pid: ctl.status('starting', game_pid=pid)
    ctl.watch_startup()
    if args.attach:
        g = Game.attach(extra=AGENT_EXTRA, verbose=False, visible=args.visible)
    elif args.menu:
        for name in ('GODOT_TEST.GM1', 'GODOT_BATTLE.GM1'):
            ensure_save(name)
        g = await asyncio.to_thread(start_at_menu, args.start_timeout, args.visible)
        started = True
    else:
        ensure_save(args.load)
        g = await asyncio.to_thread(Game.start, load=args.load, extra=AGENT_EXTRA, verbose=False,
                                    timeout=args.start_timeout, visible=args.visible)
        started = True
    log(f'game pid {g.pid} ({"started" if started else "attached"}, {"visible" if args.visible else "hidden"} window)')
    ctl.status('game', game_pid=g.pid, visible=args.visible)
    br = EngineBridge(g, asyncio.get_running_loop(), log, Recorder(args.record), args.dialog_timeout,
                      battles=args.battles, windows=parse_windows(args.windows), fps=args.fps,
                      jpeg_quality=args.jpeg_quality, window_timeout=args.window_timeout, debug=args.debug,
                      visible=args.visible)
    try:
        # generous keepalive: a client may block for minutes while it builds the 3D world (first frame compiles
        # shaders on a software renderer)
        async with ws_serve(br.serve_client, args.host, args.port, max_size=2 ** 24, ping_interval=60,
                            ping_timeout=600):
            log(f'bridge on ws://{args.host}:{args.port}')
            ctl.status('ready', url=f'ws://{args.host}:{args.port}')
            while g.alive():
                why = ctl.stop_reason()
                if why:
                    log(f'stopping: {why}')
                    break
                await asyncio.sleep(1)
            else:
                # the player quit from the engine's menu (or the game died): tell the clients, give them a moment
                log('game process exited')
                br.phase = 'exited'
                br.broadcast({'t': 'phase', 'phase': 'exited'})
                await asyncio.sleep(1.5)
    finally:
        try:
            g.api.bridge_disable()
        except Exception:
            pass
        if started and not args.keep_game:
            g.kill()
            await asyncio.sleep(0.5)
            launch.restore_registry()


def start_at_menu(timeout, visible=False):
    """Spawn the game and let it run to its main menu (no menu automation): the player chooses everything."""
    import launch
    from launch import Game
    g = Game.spawn(verbose=False, extra=AGENT_EXTRA, visible=visible)
    try:
        g.resume()
        g.wait_phase('main-menu', timeout)
        g.api.idle_polls(launch.IDLE_POLLS)
        return g
    except Exception:
        g.kill()
        raise


def snapshot_only(args, out):
    from launch import Game
    from snapinfo import write_objects_tsv
    if args.attach:
        g = Game.attach(extra=AGENT_EXTRA, verbose=False)
    else:
        ensure_save(args.load)
        g = Game.start(load=args.load, extra=AGENT_EXTRA, verbose=False)
    try:
        snap = json.loads(g.run_job(g.api.snapshot_job(), timeout=20))
        snap['source'] = 'engine'
        snap['file'] = args.load
        with open(out, 'w', encoding='utf-8') as f:
            f.write(jdump(snap))
        tsv = os.path.join(os.path.dirname(out), 'objects_' + os.path.basename(out).replace('snapshot_', '')
                           .replace('.json', '.tsv'))
        n = write_objects_tsv(snap, tsv)
        print(f'{out}: {len(snap["objects"])} objects, {len(snap["heroes"])} heroes; {tsv}: {n} kinds')
    finally:
        if not args.attach and not args.keep_game:
            g.kill()


# ----------------------------------------------------------------------------------------------- no engine

class ReplayGame:
    """Serves a recorded engine snapshot. No engine behind it: commands are refused (the client must not compute
    mechanics itself; use the real bridge or mock_server for interaction)."""

    def __init__(self, path):
        with open(path, encoding='utf-8') as f:
            self.snap = json.load(f)
        self.snap['t'] = 'snapshot'

    def hello(self):
        return {'t': 'hello', 'proto': PROTO, 'source': 'replay', 'game': 'recorded snapshot'}

    def snapshot(self):
        return self.snap

    def handle(self, msg):
        if msg.get('t') == 'get_snapshot':
            return [('self', self.snap)]
        return [('self', {'t': 'error', 'msg': 'replay mode: no engine behind this bridge'})]


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--attach', action='store_true', help='attach to a running h3hota.exe instead of starting one')
    ap.add_argument('--load', default='GODOT_TEST.GM1', help='save in games\\ to load (default %(default)s)')
    ap.add_argument('--menu', action='store_true',
                    help="start at the game's main menu instead of loading --load: the player chooses any mode "
                         '(new game, campaign, load, hot-seat ...) on the streamed engine screen')
    ap.add_argument('--keep-game', action='store_true', help='do not kill the game this bridge started on exit')
    ap.add_argument('--visible', action='store_true',
                    help='the game window is a normal window on the desktop (default: hidden): the player plays in it '
                         'and in the client at the same time; engine dialogs show in both, changes made in the game '
                         'window reach the client (state why "external"), no dialog / MessageBox auto-answers. '
                         'Needs a desktop (not Windows session 0); with --attach only the bridge side changes')
    ap.add_argument('--dialog-timeout', type=float, default=120, help='seconds to wait for a dialog answer')
    ap.add_argument('--battles', choices=('auto', 'manual', 'game'), default=None,
                    help="auto: the engine's quick combat; manual: the human's battles on the engine's battle screen "
                         "(passthrough, needs a client with caps windows); game: as the game's Quick Combat option "
                         'says (default with --menu, else auto)')
    ap.add_argument('--windows', default='', help="per-window policy, e.g. 'townScreen=close,heroMeeting=skip,*=pass' "
                    '(pass | run | ok | cancel; townScreen: close; heroMeeting: skip; README.md)')
    ap.add_argument('--fps', type=float, default=8, help='max window_frame rate (default %(default)s)')
    ap.add_argument('--jpeg-quality', type=int, default=80)
    ap.add_argument('--window-timeout', type=float, default=0,
                    help='seconds a passthrough window may stay open without a capable client before the bridge '
                         'presses Esc in it (0 = wait for the player to reconnect)')
    ap.add_argument('--record', help='directory: snapshots + traffic.jsonl')
    ap.add_argument('--mock', action='store_true', help='no game: mock_server.MockGame or --replay FILE')
    ap.add_argument('--replay', help='with --mock: serve this recorded snapshot JSON')
    ap.add_argument('--seed', type=int, default=7, help='--mock: map seed')
    ap.add_argument('--snapshot-only', metavar='OUT.json', help='write one engine snapshot (+ objects tsv) and exit')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--debug', action='store_true', help='accept debug_eval (JS in the game agent; never with --host 0.0.0.0)')
    ap.add_argument('--game-dir', help='game folder with h3hota.exe (default: env HOTA_GAME_DIR, else the repo game/Heroes3_HotA1)')
    ap.add_argument('--control-dir', help='local mode: status.json / stop / heartbeat files shared with the client')
    ap.add_argument('--heartbeat-timeout', type=float, default=900,
                    help='with --control-dir: exit when the heartbeat file is older than this (0 = never)')
    ap.add_argument('--log', help='append stdout and stderr to this file')
    ap.add_argument('--start-timeout', type=float, default=180,
                    help='seconds for each game start phase (menu, map load); generous: a client starting at the same '
                         'time on a software renderer can starve the game of CPU (default %(default)s)')
    args = ap.parse_args()
    if args.battles is None:
        args.battles = 'game' if args.menu else 'auto'
    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, 'a', encoding='utf-8', buffering=1)
    args.control = Control(args.control_dir, args.heartbeat_timeout)
    args.control.status('starting')
    try:
        if args.game_dir:
            import launch
            launch.set_game_dir(args.game_dir)
        if args.menu and not args.mock:
            import launch
            launch.REG_VALUES.pop('Quick Combat', None)   # the player's own option (battles 'game')
        if args.snapshot_only:
            snapshot_only(args, args.snapshot_only)
        elif args.mock:
            import mock_server
            game = ReplayGame(args.replay) if args.replay else mock_server.MockGame.create(args.seed)
            args.control.status('ready', url=f'ws://{args.host}:{args.port}')
            asyncio.run(mock_server.serve(game, args.host, args.port,
                                          log=(lambda *_: None) if args.quiet else (lambda *x: print(*x, flush=True))))
        else:
            asyncio.run(serve_engine(args))
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        import traceback
        traceback.print_exc()
        if 'launch' in sys.modules:
            sys.modules['launch'].restore_registry()
        args.control.status('error', error=f'{type(e).__name__}: {e}')
        sys.exit(1)
    args.control.status('exited')


if __name__ == '__main__':
    main()
