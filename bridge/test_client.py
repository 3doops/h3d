"""End-to-end test of the engine bridge through the WebSocket (PROTOCOL.md), as a client would do it.

    python -I test_client.py              # starts bridge.py (which starts the game), runs the scenario, stops both
    python -I test_client.py --url ws://127.0.0.1:8765    # use a running bridge.py
    python -I test_client.py --scenario menu  # the game's main menu through the bridge (PLAN 4.5), see below
    python -I test_client.py --scenario mirror  # bridge.py --visible: play in the game window and the client (PLAN 4.6)

Scenario on GODOT_TEST.GM1 (The Newcomers, red = human):
  1. hello + snapshot
  2. select Клавий; engine path to the nearest resource pile reachable today; move; state shows more resources
  3. path + move to a treasure chest -> `dialog` (choose gold / experience) -> answer -> state shows it
     (experience can raise the level: the level-up choice comes as a dialog too and is answered)
  4. window passthrough (PLAN 1.9; the client announced caps ["windows"]): the hero walks into his own town -> the
     town screen stays open, its frames stream (window_frame); by clicks in engine coordinates: the skeleton
     dwelling -> recruit window -> "max" -> "recruit" -> exit; after window_close the state shows less gold and
     more creatures. Then open_hero (hero screen, closed with Esc) and adv_key K (kingdom overview, closed by OK)
  5. end_turn -> "heroes can still move" dialog answered yes -> AI turns run in the engine -> red's turn again,
     date advanced
  6. second game GODOT_BATTLE.GM1 (Too Many Monsters; on The Newcomers no monster is reachable in the first
     days), bridge --battles manual: attack the stack next to the hero -> the engine's battle screen streams ->
     click on the engine's auto-combat button -> the engine finishes the battle -> `event battle`, monster removed
Scenario "menu" (bridge.py --menu, PLAN 4.5; frames godot/out/bridge/20-29 *.jpg): the game starts at its main
menu, streamed (phase menu) -> clicks: New Game -> Single scenario -> list of scenarios -> size S -> The Newcomers ->
Begin -> the intro message (still menu) closed with Enter -> phase adventure + snapshot of the new map (36x36x2), an
engine path on it -> system_menu (systemOptions window) -> Main menu -> confirm -> phase menu -> Load Game -> Single ->
first save (GODOT_TEST.GM1) -> Load -> phase adventure + snapshot (1233 objects) -> system_menu -> Quit -> confirm ->
phase exited (the game process ended). Coordinates: 800x600 menus of HotA 1.8.0 (Russian), from the frames.
Scenario "mirror" (bridge.py --visible --debug, PLAN 4.6; frames 30-36 *.jpg): the game window is a normal window
(style checked); a client move is drawn by the engine (frame before/after) and presented to the window; the player's
input in the game window (window messages: clicks on map tiles, E, the hero portrait, the system button, OK) -> a
native hero move comes as hero_move external + state why external; a chest dialog opened there is mirrored and
answered from the client; end turn there, its question answered there, AI turns, day 2; the hero screen opened there
is closed from the client (Esc); the kingdom overview opened from the client is closed there while a client `path`
waits for it; main menu chosen there -> phase menu; the client loads a save by clicks; quit there -> phase exited.
Engine frames: godot/out/bridge/0*.jpg (debug_shot) and 08-14 *.jpg (window_frame as received). Click coordinates are
those of this save's Necropolis town screen and the 800x600 battle screen (found from the frames).
Exit code 0 if every required check passed; godot/out/bridge/test_report.json has the details.
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, '..', 'out', 'bridge'))
RES = ['wood', 'mercury', 'ore', 'sulfur', 'crystal', 'gems', 'gold']


class Client:
    def __init__(self, ws, log, traffic='test_traffic.jsonl'):
        self.ws, self.log = ws, log
        self.msgs = []
        self.cond = asyncio.Condition()
        self.dialog_policy = {}      # engine_style or 'choose'/'yesno'/'ok' -> answer
        self.dialogs = []
        self.traffic = open(os.path.join(OUT, traffic), 'w', encoding='utf-8')
        self.snapshot = None
        self.state = {}
        self.frame = None            # newest window_frame
        self.frames = 0
        self.hold_native = set()     # engine_styles of dialogs shown in the game window (visible mode, native: true)
                                     # that are not auto-answered: the scenario answers them itself

    async def reader(self):
        async for raw in self.ws:
            m = json.loads(raw)
            t = m.get('t')
            if t == 'window_frame':        # keep the newest frame; the log and the message list get no pixels
                self.frame = m
                self.frames += 1
                m = {k: (len(v) if k == 'data' else v) for k, v in m.items()}
            elif t in ('window_open', 'window_close'):
                self.log(f"   {t} {json.dumps({k: v for k, v in m.items() if k != 't'}, ensure_ascii=False)}")
            self.traffic.write(json.dumps({'ts': round(time.time(), 3), 'dir': 'in', 'msg': m}, ensure_ascii=False) + '\n')
            if t == 'snapshot':
                self.snapshot = m
                self.state = {k: m[k] for k in ('heroes', 'towns', 'players', 'date', 'current')}
            elif t == 'state':
                self.state = {k: m[k] for k in ('heroes', 'towns', 'players', 'date', 'current')}
            elif t == 'dialog':
                self.dialogs.append(m)
                self.log(f"   dialog {m['id']} [{m['style']}/{m.get('engine_style')}] {m['text'][:90]!r} "
                         f"options={[o.get('text') for o in m.get('options', [])]}" + (f" auto={m['auto']}" if 'auto' in m else ''))
                if 'auto' not in m and not (m.get('native') and m.get('engine_style') in self.hold_native):
                    ans = self.dialog_policy.get(m.get('engine_style'), self.dialog_policy.get(m['style'], 0))
                    self.log(f'   -> answer {ans}')
                    await self.send({'t': 'dialog_answer', 'id': m['id'], 'answer': ans})
            elif t == 'event':
                if m['kind'] == 'hero_move':
                    self.log(f"   event hero_move hero {m['hero']}: {m['cells']}")
                else:
                    self.log(f"   event {m['kind']}: {json.dumps({k: v for k, v in m.items() if k not in ('t', 'kind')}, ensure_ascii=False)[:300]}")
            elif t == 'error':
                self.log(f"   ERROR {m.get('msg')}")
            async with self.cond:
                self.msgs.append(m)
                self.cond.notify_all()

    async def send(self, m):
        self.traffic.write(json.dumps({'ts': round(time.time(), 3), 'dir': 'out', 'msg': m}, ensure_ascii=False) + '\n')
        await self.ws.send(json.dumps(m, ensure_ascii=False))

    async def wait(self, pred, timeout=60, start=0):
        end = time.time() + timeout
        async with self.cond:
            while True:
                for m in self.msgs[start:]:
                    if pred(m):
                        return m
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError('no matching message')
                try:
                    await asyncio.wait_for(self.cond.wait(), left)
                except asyncio.TimeoutError:
                    pass

    def mark(self):
        return len(self.msgs)

    async def request(self, m, pred, timeout=60):
        k = self.mark()
        await self.send(m)
        return await self.wait(lambda x: pred(x) or x.get('t') == 'error', timeout, k)

    async def cmd(self, m, timeout=300):
        r = await self.request(m, lambda x: x.get('t') == 'cmd_done' and x.get('cmd') == m['cmd'], timeout)
        if r.get('t') == 'error':
            raise RuntimeError(r['msg'])
        return r

    async def path(self, hero, to):
        r = await self.request({'t': 'path', 'hero': hero, 'to': to}, lambda x: x.get('t') == 'path', 60)
        if r.get('t') == 'error':
            raise RuntimeError(r['msg'])
        return r

    async def shot(self, name):
        p = os.path.join(OUT, name)
        r = await self.request({'t': 'debug_shot', 'path': p}, lambda x: x.get('t') == 'debug' and x.get('what') == 'shot', 30)
        self.log(f'   shot {p}: {r.get("ok")}')
        return p

    def me(self):
        return self.snapshot['me']

    # ---- window passthrough ----
    async def mouse(self, x, y, action, button='left'):
        await self.send({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y, 'button': button, 'action': action})

    async def click(self, x, y, pause=0.6):
        """Left click at engine coordinates (800x600), as a player: move there, press, release."""
        await self.mouse(x, y, 'move')
        await self.mouse(x, y, 'down')
        await asyncio.sleep(0.1)
        await self.mouse(x, y, 'up')
        await asyncio.sleep(pause)

    async def key(self, name, pause=0.6):
        await self.send({'t': 'window_input', 'kind': 'key', 'key': name, 'action': 'press'})
        await asyncio.sleep(pause)

    async def fresh_frame(self, timeout=10):
        """The next window_frame after now (the window may be animated, so any newer one)."""
        seq = self.frame['seq'] if self.frame else -1
        k = self.mark()
        await self.send({'t': 'window_refresh'})
        return await self.wait(lambda m: m.get('t') == 'window_frame' and m['seq'] > seq, timeout, k)

    def save_frame(self, name):
        p = os.path.join(OUT, name)
        with open(p, 'wb') as f:
            f.write(base64.b64decode(self.frame['data']))
        self.log(f"   frame #{self.frame['seq']} {self.frame.get('name')} -> {p}")
        return p

    async def eval(self, src, timeout=30):
        """bridge.py --debug: evaluate JS in the game agent"""
        r = await self.request({'t': 'debug_eval', 'src': src}, lambda m: m.get('t') == 'debug' and m.get('what') == 'eval',
                               timeout)
        if r.get('t') == 'error':
            raise RuntimeError(r['msg'])
        return json.loads(r['result'])

    async def queue(self):
        r = await self.request({'t': 'debug_queue'}, lambda m: m.get('t') == 'debug' and m.get('what') == 'queue', 30)
        return r['queue']

    def player(self):
        return self.state['players'][self.me()]

    def hero(self, hid):
        return next((h for h in self.state['heroes'] if h['id'] == hid), None)


class GameWindow:
    """The real game window (Win32), for the mirror scenario: its style, and input as the player would give it - window
    messages posted to it (AppWndProc turns them into engine input exactly as for real mouse/keyboard; SendInput
    does not reach a window in Windows session 0)."""
    WS_VISIBLE, WS_CAPTION, WS_MINIMIZE, WS_EX_TOOLWINDOW = 0x10000000, 0x00C00000, 0x20000000, 0x80

    def __init__(self, hwnd):
        import ctypes
        self.u = ctypes.windll.user32
        self.ct = ctypes
        self.hwnd = hwnd

    def info(self):
        from ctypes import wintypes
        r, c = wintypes.RECT(), wintypes.RECT()
        self.u.GetWindowRect(self.hwnd, self.ct.byref(r))
        self.u.GetClientRect(self.hwnd, self.ct.byref(c))
        t = self.ct.create_unicode_buffer(256)
        self.u.GetWindowTextW(self.hwnd, t, 256)
        style = self.u.GetWindowLongW(self.hwnd, -16) & 0xffffffff
        ex = self.u.GetWindowLongW(self.hwnd, -20) & 0xffffffff
        return {'title': t.value, 'style': hex(style), 'exstyle': hex(ex), 'ws_visible': bool(style & self.WS_VISIBLE),
                'caption': (style & self.WS_CAPTION) == self.WS_CAPTION, 'minimized': bool(self.u.IsIconic(self.hwnd)),
                'toolwindow': bool(ex & self.WS_EX_TOOLWINDOW), 'rect': [r.left, r.top, r.right, r.bottom],
                'client': [c.right - c.left, c.bottom - c.top],
                # always 0 in session 0 (the window station itself is not visible); 1 on a real desktop
                'IsWindowVisible': bool(self.u.IsWindowVisible(self.hwnd))}

    def _post(self, msg, wp, lp):
        self.u.PostMessageW(self.hwnd, msg, wp, lp)

    async def click(self, x, y, pause=0.6, right=False):
        lp = (y << 16) | x
        self._post(0x200, 0, lp)
        await asyncio.sleep(0.08)
        self._post(0x204 if right else 0x201, 2 if right else 1, lp)
        await asyncio.sleep(0.1)
        self._post(0x205 if right else 0x202, 0, lp)
        await asyncio.sleep(pause)

    async def move(self, x, y, pause=0.3):
        self._post(0x200, 0, (y << 16) | x)
        await asyncio.sleep(pause)

    async def key(self, vk, pause=0.6):
        sc = self.u.MapVirtualKeyW(vk, 0)
        self._post(0x100, vk, 1 | (sc << 16))
        await asyncio.sleep(0.06)
        self._post(0x101, vk, 1 | (sc << 16) | 0xC0000000)
        await asyncio.sleep(pause)


def dist(h, o):
    return max(abs(h['x'] - o['x']), abs(h['y'] - o['y']))


def creatures_at(c, hero, town_id):
    """creature id -> count in the town garrison plus the hero's army"""
    out = {}
    t = next(t for t in c.state['towns'] if t['id'] == town_id)
    h = c.hero(hero) or {}
    for slot in list(t.get('garrison') or []) + list(h.get('army') or []):
        if slot != -1 and slot:
            out[slot[0]] = out.get(slot[0], 0) + slot[1]
    return out


async def find_target(c, hero, objs, today=True, limit=12):
    """Ask the engine for paths to the nearest candidates; first one reachable (today) wins."""
    h = c.hero(hero)
    cands = sorted((o for o in objs if o['z'] == h['z']), key=lambda o: dist(h, o))[:limit]
    for o in cands:
        to = o.get('trigger') or [o['x'], o['y'], o['z']]
        p = await c.path(hero, to)
        if p['cells'] and (not today or p['reachable'] == len(p['cells'])):
            return o, to, p
        c.log(f"   candidate {o['def']} #{o['id']} {to}: {len(p['cells'])} cells, reachable today {p['reachable']}")
    return None, None, None


async def scenario(url, log):
    from websockets.asyncio.client import connect
    report = {'checks': {}, 'notes': []}

    def check(name, ok, info=''):
        report['checks'][name] = {'ok': bool(ok), 'info': info}
        log(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")

    async with connect(url, max_size=2 ** 24) as ws:
        c = Client(ws, log)
        rt = asyncio.create_task(c.reader())
        hello = await c.wait(lambda m: m.get('t') == 'hello', 30)
        snap = await c.wait(lambda m: m.get('t') == 'snapshot', 60)
        await c.send({'t': 'hello', 'client': 'test_client', 'caps': ['windows']})   # we show engine windows
        check('hello+snapshot', hello.get('source') == 'engine' and snap['map']['size'] > 0,
              f"source={hello.get('source')} map {snap['map']['size']}x{snap['map']['levels']} objects {len(snap['objects'])} "
              f"heroes {len(snap['heroes'])} date {snap['date']} current {snap['current']} me {snap['me']}")
        q = await c.queue()
        wi = GameWindow(q['hwnd']).info()
        check('hidden mode (default): the game window has no WS_VISIBLE', not wi['ws_visible'] and
              hello.get('visible') is False and 'mirror' not in hello.get('caps', []), json.dumps(wi, ensure_ascii=False))
        await c.shot('01_start.jpg')
        me = c.me()
        mine = [h for h in c.state['heroes'] if h['owner'] == me]
        hero = next((h['id'] for h in mine if h['name'].startswith('Клавий')), mine[0]['id'])
        log(f'hero {hero} {c.hero(hero)["name"]} at {[c.hero(hero)[k] for k in "xyz"]}, mp {c.hero(hero)["mp"]}')

        # --- 2. resource pile ---
        await c.cmd({'t': 'cmd', 'cmd': 'select_hero', 'hero': hero})
        piles = [o for o in snap['objects'] if o['type'] == 79]
        o, to, p = await find_target(c, hero, piles)
        if o is None:
            check('resource: path', False, 'no resource pile reachable today')
        else:
            check('resource: engine path', len(p['cells']) > 0 and p['reachable'] == len(p['cells']),
                  f"{o['def']} #{o['id']} at {to}: {len(p['cells'])} cells, reachable {p['reachable']}, cells {p['cells']}")
            res0 = list(c.player()['res'])
            k = c.mark()
            await c.cmd({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': to})
            st = await c.wait(lambda m: m.get('t') == 'state', 30, k)
            mv = next((m for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'hero_move'), None)
            res1 = c.player()['res']
            gained = {RES[i]: res1[i] - res0[i] for i in range(7) if res1[i] != res0[i]}
            check('resource: picked up', o['id'] in st['objects_removed'] and gained,
                  f"gained {gained}, removed {st['objects_removed']}, hero_move {mv and mv['cells']}")
            await c.shot('02_resource.jpg')

        # --- 3. treasure chest (choose dialog) ---
        chests = [x for x in snap['objects'] if x['type'] == 101]
        o, to, p = await find_target(c, hero, chests, today=True)
        if o is None:
            o, to, p = await find_target(c, hero, chests, today=False)
        if o is None:
            check('chest: path', False, 'no chest reachable')
        else:
            log(f"chest #{o['id']} at {to}: {len(p['cells'])} cells, reachable today {p['reachable']}")
            c.dialog_policy = {7: 1, 'choose': 1, 'yesno': 0, 'ok': 0}     # experience
            h0 = dict(c.hero(hero))
            res0 = list(c.player()['res'])
            k = c.mark()
            await c.cmd({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': to})
            d = next((m for m in c.msgs[k:] if m.get('t') == 'dialog' and m.get('style') == 'choose'), None)
            h1 = c.hero(hero)
            res1 = c.player()['res']
            if d is None and p['reachable'] < len(p['cells']):
                check('chest: dialog', False, 'chest not reached today (moved part of the way)')
            else:
                check('chest: choose dialog answered', d is not None,
                      f"text {d and d['text'][:60]!r} options {d and [x['text'] for x in d['options']]}; exp {h0['exp']} -> "
                      f"{h1['exp']}, level {h0['level']} -> {h1['level']}, gold {res0[6]} -> {res1[6]}")
                check('chest: engine applied the answer', h1['exp'] > h0['exp'] or res1[6] > res0[6])
                lvl = [m for m in c.msgs[k:] if m.get('t') == 'dialog' and m.get('engine_style') == 'levelup']
                if lvl:
                    report['notes'].append(f"level-up dialog: {lvl[0]['options']}")
            await c.shot('03_chest.jpg')

        # --- 4. window passthrough: own town -> engine town screen streamed, recruit by clicks, exit ---
        town = next((t for t in c.state['towns'] if t['owner'] == me and t['z'] == c.hero(hero)['z']), None)
        if town:
            gate = [town['x'], town['y'], town['z']]
            p = await c.path(hero, gate)
            if p['cells'] and p['reachable'] == len(p['cells']):
                c.dialog_policy = {'yesno': 0, 'ok': 0, 'choose': 0}
                res0 = list(c.player()['res'])
                army0 = creatures_at(c, hero, town['id'])
                k = c.mark()
                # move_hero finishes (cmd_done) only when the town screen is closed
                await c.send({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': gate})
                wo = await c.wait(lambda m: m.get('t') == 'window_open', 60, k)
                mv = next((m for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'hero_move'), None)
                await c.wait(lambda m: m.get('t') == 'window_frame', 30, k)
                await asyncio.sleep(1.0)
                await c.fresh_frame()
                c.save_frame('08_town_window.jpg')
                check('passthrough: town screen open, frames stream',
                      wo.get('name') == 'townScreen' and wo.get('town') == town['id'] and c.frames > 0,
                      f"{wo}; hero_move before it {mv and mv['cells']}; frames so far {c.frames}")
                # recruit: the skeleton dwelling in the town view ("Скелеты. Нанять" in the status bar) -> the recruit
                # window: "max", "recruit" (the window closes, the creatures go to the town garrison)
                await c.click(170, 265, 1.5)
                await c.fresh_frame()
                c.save_frame('09_recruit_window.jpg')
                await c.click(309, 345)          # max
                await c.click(387, 345, 1.5)     # recruit (the window closes)
                await c.fresh_frame()
                c.save_frame('10_town_recruited.jpg')
                k2 = c.mark()
                await c.click(771, 563)          # exit the town
                wc = await c.wait(lambda m: m.get('t') == 'window_close', 30, k2)
                await c.wait(lambda m: m.get('t') == 'state', 30, k2)
                await c.wait(lambda m: m.get('t') == 'cmd_done' and m.get('cmd') == 'move_hero', 60, k)
                res1 = c.player()['res']
                army1 = creatures_at(c, hero, town['id'])
                gained = {cr: n - army0.get(cr, 0) for cr, n in army1.items() if n != army0.get(cr, 0)}
                spent = {RES[i]: res0[i] - res1[i] for i in range(7) if res1[i] != res0[i]}
                check('passthrough: recruited by clicks, state after window_close shows it',
                      wc.get('name') == 'townScreen' and spent.get('gold', 0) > 0 and bool(gained) and
                      all(v > 0 for v in gained.values()),
                      f"spent {spent}, creatures gained {gained} (town garrison + hero), {c.frames} frames, "
                      f"close {wc}")
                await c.shot('04_town.jpg')
            else:
                report['notes'].append(f"own town {town['name']} not reachable today: {len(p['cells'])} cells")

        # --- 4b. hero screen (open_hero) closed with Esc; kingdom overview (adv_key K) closed by its OK button ---
        k = c.mark()
        await c.send({'t': 'cmd', 'cmd': 'open_hero', 'hero': hero})
        wo = await c.wait(lambda m: m.get('t') in ('window_open', 'error'), 60, k)
        if wo.get('t') == 'window_open':
            await c.fresh_frame()
            c.save_frame('11_hero_window.jpg')
            k2 = c.mark()
            await c.key('Esc')
            wc = await c.wait(lambda m: m.get('t') == 'window_close', 30, k2)
            await c.wait(lambda m: m.get('t') == 'cmd_done' and m.get('cmd') == 'open_hero', 30, k)
            check('passthrough: open_hero -> hero screen, Esc closes it', wo.get('name') == 'heroScreen',
                  f'{wo} / {wc}')
        else:
            check('passthrough: open_hero', False, str(wo))
        k = c.mark()
        await c.send({'t': 'cmd', 'cmd': 'adv_key', 'key': 'K'})
        wo = await c.wait(lambda m: m.get('t') in ('window_open', 'error'), 30, k)
        if wo.get('t') == 'window_open':
            await c.fresh_frame()
            c.save_frame('12_kingdom_window.jpg')
            k2 = c.mark()
            await c.click(771, 563)
            wc = await c.wait(lambda m: m.get('t') == 'window_close', 30, k2)
            check('passthrough: adv_key K -> kingdom overview, OK closes it', wo.get('name') == 'kingdomOverview',
                  f'{wo} / {wc}')
        else:
            check('passthrough: adv_key K', False, str(wo))

        # --- 5. end turn, AI turns, back to red ---
        c.dialog_policy = {'yesno': 0, 'ok': 0, 'choose': 0}
        date0 = dict(c.state['date'])
        k = c.mark()
        await c.cmd({'t': 'cmd', 'cmd': 'end_turn'}, timeout=600)
        back = await c.wait(lambda m: m.get('t') == 'state' and m.get('current') == me and m.get('date') != date0, 600, k)
        turns = [m['current'] for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'turn']
        check('end_turn: AI played, red again, date advanced', back['date'] != date0,
              f"date {date0} -> {back['date']}, turn order {turns}")
        await asyncio.sleep(0.5)
        await c.shot('05_next_day.jpg')

        q = await c.request({'t': 'debug_queue'}, lambda m: m.get('t') == 'debug' and m.get('what') == 'queue', 30)
        check('engine idle on the adventure map', q['queue']['can'] and q['queue']['depth'] == 0, str(q['queue']))
        report['dialogs'] = [{k2: d[k2] for k2 in ('id', 'style', 'engine_style', 'text') if k2 in d} for d in c.dialogs]
        rt.cancel()
    return report


async def scenario_battle(url, log, report):
    """GODOT_BATTLE.GM1 (Too Many Monsters): a monster stack two tiles from the hero; quick combat by the engine."""
    from websockets.asyncio.client import connect

    def check(name, ok, info=''):
        report['checks'][name] = {'ok': bool(ok), 'info': info}
        log(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")

    async with connect(url, max_size=2 ** 24) as ws:
        c = Client(ws, log, 'test_traffic_battle.jsonl')
        rt = asyncio.create_task(c.reader())
        await c.wait(lambda m: m.get('t') == 'hello', 30)
        snap = await c.wait(lambda m: m.get('t') == 'snapshot', 60)
        await c.send({'t': 'hello', 'client': 'test_client', 'caps': ['windows']})
        me = c.me()
        hero = next(h['id'] for h in c.state['heroes'] if h['owner'] == me)
        h0 = dict(c.hero(hero))
        log(f"battle save: hero {hero} {h0['name']} at {[h0[k] for k in 'xyz']} army {h0['army']}")
        await c.shot('06_battle_start.jpg')
        mons = [x for x in snap['objects'] if x['type'] == 54]
        o, to, p = await find_target(c, hero, mons, today=True, limit=8)
        if o is None:
            check('battle: monster reachable', False, 'no monster reachable today')
        else:
            c.dialog_policy = {'yesno': 0, 'ok': 0, 'choose': 0}
            k = c.mark()
            log(f"attack {o['def']} (creature {o['subtype']}) x{o.get('count')} at {to}, path {p['cells']}")
            # bridge --battles manual: the engine's battle screen is a passthrough window
            await c.send({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': to})
            wo = await c.wait(lambda m: m.get('t') == 'window_open', 60, k)
            await c.wait(lambda m: m.get('t') == 'window_frame', 30, k)
            await asyncio.sleep(1.5)
            await c.fresh_frame()
            c.save_frame('13_battle_window.jpg')
            check('manual battle: battle screen streams', wo.get('name') == 'combat' and c.frames > 0, str(wo))
            # the engine's auto-combat button (4th in the bottom bar). The faster enemy stacks act first, and input
            # queued before the player's turn is dropped by the engine (as a real click would be): wait, click,
            # and click again only if the battle did not end within a minute (a second click on a working
            # auto-combat would switch it off)
            wc = None
            for attempt in range(4):
                await asyncio.sleep(6.0)
                await c.click(167, 577, 2.0)
                await c.fresh_frame()
                c.save_frame('14_battle_autocombat.jpg')
                try:
                    wc = await c.wait(lambda m: m.get('t') == 'window_close' and m.get('name') == 'combat', 60, k)
                    break
                except TimeoutError:
                    log(f'   battle still running after auto-combat click {attempt + 1}')
            if wc is None:
                raise TimeoutError('battle screen did not close')
            await c.wait(lambda m: m.get('t') == 'cmd_done' and m.get('cmd') == 'move_hero', 120, k)
            b = next((m for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'battle'), None)
            removed = [x for m in c.msgs[k:] if m.get('t') == 'state' for x in m['objects_removed']]
            h1 = c.hero(hero)
            check('battle: event from the engine', b is not None and b.get('winner') in ('attacker', 'defender'),
                  json.dumps(b, ensure_ascii=False)[:500] if b else 'no battle event')
            check('battle: result applied (monster gone or hero lost)', o['id'] in removed or h1 is None,
                  f"window {wc}; hero exp {h0['exp']} -> {h1 and h1['exp']}, army {h1 and h1['army']}")
            await c.shot('07_battle_after.jpg')
        q = await c.request({'t': 'debug_queue'}, lambda m: m.get('t') == 'debug' and m.get('what') == 'queue', 30)
        check('battle: engine idle on the adventure map (no result window left)',
              q['queue']['can'] and q['queue']['depth'] == 0, str(q['queue']))
        rt.cancel()
    return report


async def scenario_menu(url, log, report):
    """bridge.py --menu: play from the game's own main menu (streamed), enter a map, back to the menu, load, quit."""
    from websockets.asyncio.client import connect

    def check(name, ok, info=''):
        report['checks'][name] = {'ok': bool(ok), 'info': info}
        log(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")

    def phase_after(k, ph):
        return lambda m: m.get('t') == 'phase' and m.get('phase') == ph

    async def steps(c, *acts):
        for a in acts:
            if isinstance(a, (int, float)):
                await asyncio.sleep(a)
            else:
                await c.click(*a, pause=0.4)

    async with connect(url, max_size=2 ** 24) as ws:
        c = Client(ws, log, 'test_traffic_menu.jsonl')
        c.dialog_policy = {'yesno': 0, 'ok': 0, 'choose': 0}
        rt = asyncio.create_task(c.reader())
        hello = await c.wait(lambda m: m.get('t') == 'hello', 30)
        ph = await c.wait(lambda m: m.get('t') == 'phase', 30)
        await c.send({'t': 'hello', 'client': 'test_client', 'caps': ['windows']})
        await c.wait(lambda m: m.get('t') == 'window_frame', 30)
        await asyncio.sleep(1.0)
        await c.fresh_frame()
        c.save_frame('20_menu_main.jpg')
        check('menu: starts at the main menu, streamed', ph.get('phase') == 'menu' and 'phase' in hello.get('caps', [])
              and c.frame.get('name') == 'menu', f"hello caps {hello.get('caps')}, {ph}, frame {c.frame.get('name')}")

        # New Game -> Single scenario -> list -> S -> The Newcomers -> Begin
        k = c.mark()
        await steps(c, (650, 82), 1.5, (650, 82), 6.0)
        await c.fresh_frame()
        c.save_frame('21_menu_scenario.jpg')
        await steps(c, (512, 89), 2.0, (41, 68), 1.5, (215, 359), 1.5)
        await c.fresh_frame()
        c.save_frame('22_menu_newcomers.jpg')
        await c.click(495, 553, 6.0)
        await c.fresh_frame()
        c.save_frame('23_menu_intro.jpg')
        adv = None
        for _ in range(20):       # the scenario intro (a message of the menu phase): Enter until the map runs
            try:
                adv = await c.wait(phase_after(k, 'adventure'), 3, k)
                break
            except TimeoutError:
                await c.key('enter', 0.5)
        if adv is None:
            raise TimeoutError('no phase adventure after Begin')
        snap = await c.wait(lambda m: m.get('t') == 'snapshot', 30, k)
        check('menu: new game -> phase adventure + snapshot of the new map',
              snap['map']['size'] == 36 and snap['map']['levels'] == 2 and snap.get('why') == 'adventure',
              f"{adv}; snapshot {snap['map']['size']}x{snap['map']['levels']}, {len(snap['objects'])} objects, "
              f"me {snap.get('me')}")
        await c.shot('24_menu_newgame_map.jpg')
        me = c.me()
        hero = next((h['id'] for h in c.state['heroes'] if h['owner'] == me), None)
        piles = [o for o in snap['objects'] if o['type'] in (79, 101, 12, 29)]
        o, to, p = (await find_target(c, hero, piles, today=False, limit=8)) if hero is not None else (None, None, None)
        check('menu: engine commands on the new map (path)', p is not None and len(p['cells']) > 0,
              f"hero {hero} -> {o and o['def']} {to}: {p and len(p['cells'])} cells")

        # system options -> Main menu -> confirm -> phase menu
        k = c.mark()
        await c.send({'t': 'cmd', 'cmd': 'system_menu'})
        wo = await c.wait(lambda m: m.get('t') == 'window_open', 30, k)
        await asyncio.sleep(1.0)
        await c.fresh_frame()
        c.save_frame('25_menu_system_options.jpg')
        await c.click(565, 433, 1.5)
        await c.wait(lambda m: m.get('t') == 'window_open' and m.get('depth') == 2, 15, k)
        await c.click(358, 349, 1.0)
        back = await c.wait(phase_after(k, 'menu'), 30, k)
        await c.wait(lambda m: m.get('t') == 'cmd_done' and m.get('cmd') == 'system_menu', 30, k)
        await asyncio.sleep(2.0)
        await c.fresh_frame()
        c.save_frame('26_menu_back_to_main.jpg')
        check('menu: system options -> main menu -> phase menu', wo.get('name') == 'systemOptions'
              and back.get('phase') == 'menu', f"window {wo.get('name')}; {back}")

        # Load Game -> Single -> first save (GODOT_TEST.GM1) -> Load
        k = c.mark()
        await steps(c, (650, 195), 1.5, (650, 82), 5.0, (215, 133), 1.0)
        await c.fresh_frame()
        c.save_frame('27_menu_load_list.jpg')
        await c.click(495, 553, 1.0)
        adv = await c.wait(phase_after(k, 'adventure'), 60, k)
        snap = await c.wait(lambda m: m.get('t') == 'snapshot', 30, k)
        check('menu: load game -> phase adventure + snapshot', adv.get('file') == 'GODOT_TEST' and
              len(snap['objects']) == 1233, f"{adv}; {len(snap['objects'])} objects")
        await c.shot('28_menu_loaded.jpg')

        # system options -> Quit -> confirm -> phase exited
        k = c.mark()
        await c.send({'t': 'cmd', 'cmd': 'system_menu'})
        await c.wait(lambda m: m.get('t') == 'window_open', 30, k)
        await asyncio.sleep(1.0)
        await c.click(460, 493, 1.5)
        await c.wait(lambda m: m.get('t') == 'window_open' and m.get('depth') == 2, 15, k)
        await c.fresh_frame()
        c.save_frame('29_menu_quit_confirm.jpg')
        await c.click(358, 349, 1.0)
        try:
            ex = await c.wait(phase_after(k, 'exited'), 60, k)
        except TimeoutError:
            ex = None
        check('menu: quit from the system options -> phase exited', ex is not None, str(ex))
        rt.cancel()
    return report


VK_E, VK_ESCAPE, VK_RETURN = 0x45, 0x1B, 0x0D


async def scenario_mirror(url, log, report):
    """bridge.py --visible --debug (PLAN 4.6): the game window is a real window, the player plays in it and in the
    client at the same time. The player's input in the game window is simulated with window messages (as the real
    mouse/keyboard would arrive); the client side is this WebSocket client."""
    from websockets.asyncio.client import connect

    def check(name, ok, info=''):
        report['checks'][name] = {'ok': bool(ok), 'info': info}
        log(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")

    async with connect(url, max_size=2 ** 24) as ws:
        c = Client(ws, log, 'test_traffic_mirror.jsonl')
        c.dialog_policy = {'yesno': 0, 'ok': 0, 'choose': 0}
        rt = asyncio.create_task(c.reader())
        hello = await c.wait(lambda m: m.get('t') == 'hello', 30)
        snap = await c.wait(lambda m: m.get('t') == 'snapshot', 60)
        await c.send({'t': 'hello', 'client': 'test_client', 'caps': ['windows']})
        q = await c.queue()
        win = GameWindow(q['hwnd'])
        wi = win.info()
        check('mirror: hello caps "mirror", visible', 'mirror' in hello.get('caps', []) and hello.get('visible') is True,
              f"caps {hello.get('caps')}, snapshot selected {snap.get('selected')}")
        check('mirror: game window is a normal visible window (WS_VISIBLE, caption, 800x600 client, not minimized)',
              wi['ws_visible'] and wi['caption'] and wi['client'] == [800, 600] and not wi['minimized']
              and not wi['toolwindow'], json.dumps(wi, ensure_ascii=False))
        report['window'] = wi
        me = c.me()
        hero = next((h['id'] for h in c.state['heroes'] if h['owner'] == me and h['name'].startswith('Клавий')),
                    next(h['id'] for h in c.state['heroes'] if h['owner'] == me))

        async def origin():
            o = await c.eval('point(adv().add(0xe4).readU32())')
            return [(o[0] ^ 512) - 512, (o[1] ^ 512) - 512, o[2]]     # 10-bit signed

        async def tile_xy(cell):
            """engine screen coordinates of a map tile in the current adventure view, None if not in view"""
            ox, oy, _ = await origin()
            col, row = cell[0] - ox, cell[1] - oy
            if 1 <= col <= 17 and 1 <= row <= 15:
                return col * 32 + 16, row * 32 + 16
            return None

        async def close_native_dialogs(timeout=20):
            """answer (from the client) the mirrored dialogs still open in the game window (day / week messages)"""
            closed = {m['id'] for m in c.msgs if m.get('t') == 'event' and m.get('kind') == 'dialog_closed'}
            for d in list(c.dialogs):
                if d.get('native') and d['id'] not in closed:
                    log(f"   closing the native dialog {d['id']} from the client")
                    await c.send({'t': 'dialog_answer', 'id': d['id'], 'answer': 0})
                    await c.wait(lambda m: m.get('t') == 'event' and m.get('kind') == 'dialog_closed' and
                                 m.get('id') == d['id'], timeout)

        async def native_walk(cell, pause=1.0):
            """first click on a tile shows the route, the second one walks (as the player does)"""
            xy = await tile_xy(cell)
            if xy is None:
                return False
            await win.move(*xy)
            await win.click(*xy, pause=pause)
            await win.click(*xy, pause=0.3)
            return True

        # --- 1. Godot -> original: a Godot move redraws the game window ---
        await c.cmd({'t': 'cmd', 'cmd': 'select_hero', 'hero': hero})
        p0 = (await c.queue())['mirror']['present']
        before = await c.shot('30_mirror_before.jpg')
        piles = [o for o in snap['objects'] if o['type'] == 79]
        o, to, p = await find_target(c, hero, piles)
        if o is None:
            check('godot -> original: move', False, 'no resource pile reachable')
        else:
            res0 = list(c.player()['res'])
            k = c.mark()
            await c.cmd({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': to})
            await c.wait(lambda m: m.get('t') == 'state', 30, k)
            await asyncio.sleep(0.5)
            after = await c.shot('31_mirror_godot_move.jpg')
            p1 = (await c.queue())['mirror']['present']
            with open(before, 'rb') as f1, open(after, 'rb') as f2:
                differs = f1.read() != f2.read()
            res1 = c.player()['res']
            check('godot -> original: the Godot move is drawn by the engine and presented to the window',
                  differs and p1['presents'] > p0['presents'] and res1 != res0,
                  f"engine frame changed {differs}; presents {p0['presents']} -> {p1['presents']} "
                  f"(GDI ok {p1['gdi_ok']}, failed {p1['gdi_failed']}: BitBlt to a window fails in session 0), "
                  f"last client rect {p1['last']}; res {res0} -> {res1}")

        # --- 2. original -> Godot: the player walks in the game window ---
        h = c.hero(hero)
        size = snap['map']['size']
        tfl = snap['map']['tflags']
        cands = []
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                cell = [h['x'] + dx, h['y'] + dy, h['z']]
                if not (0 <= cell[0] < size and 0 <= cell[1] < size) or tfl[cell[2]][cell[1] * size + cell[0]] & (8 | 16):
                    continue      # blocked or an object's entrance: walk to plain ground
                if max(abs(dx), abs(dy)) >= 2 and await tile_xy(cell):
                    cands.append(cell)
        cands.sort(key=lambda cl: -max(abs(cl[0] - h['x']), abs(cl[1] - h['y'])))
        target = None
        for cell in cands[:16]:
            pp = await c.path(hero, cell)
            if pp['cells'] and pp['reachable'] == len(pp['cells']) and len(pp['cells']) >= 2:
                target = cell
                break
        if target is None:
            check('original -> godot: native move', False, f'no free tile in view around {[h[k2] for k2 in "xyz"]}')
        else:
            k = c.mark()
            await native_walk(target)
            st = await c.wait(lambda m: m.get('t') == 'state' and m.get('why') == 'external' and
                              any(hh['id'] == hero and [hh['x'], hh['y'], hh['z']] == target for hh in m['heroes']), 30, k)
            mvs = [m for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'hero_move' and m.get('hero') == hero]
            cells = [cl for m in mvs for cl in m['cells']]
            check('original -> godot: hero walked in the game window -> hero_move (external) + state why external',
                  mvs and all(m.get('external') is True for m in mvs) and cells[-1] == target,
                  f"target {target}: {len(mvs)} hero_move events (the walk is animated), cells {cells}; "
                  f"states why external {sum(1 for m in c.msgs[k:] if m.get('why') == 'external')}, fog_open "
                  f"{sum(len(m['fog_open']) for m in c.msgs[k:] if m.get('t') == 'state')}, mp {c.hero(hero)['mp']}")
            await c.shot('32_mirror_native_move.jpg')

        # --- 2b. right-click info in the game window (held right button) stays a native popup there ---
        h = c.hero(hero)
        objs = sorted((x for x in snap['objects'] if x['z'] == h['z'] and x['type'] in (79, 101, 54, 17, 53)),
                      key=lambda x: dist(h, x))
        popup = None
        for x in objs[:8]:
            cell = x.get('trigger') or [x['x'], x['y'], x['z']]
            xy = await tile_xy(cell)
            if xy is None:
                continue
            await win.move(*xy)
            kp = c.mark()
            win._post(0x204, 2, (xy[1] << 16) | xy[0])            # right button down: the popup while it is held
            await asyncio.sleep(1.0)
            top = await c.eval('[windows().map(w => w.name), BR.nativeDlg]')
            win._post(0x205, 0, (xy[1] << 16) | xy[0])
            await asyncio.sleep(1.0)
            after = await c.eval('windows().map(w => w.name)')
            popup = {'object': x['def'], 'cell': cell, 'held': top, 'released': after,
                     'to_client': [m.get('t') for m in c.msgs[kp:] if m.get('t') in ('window_open', 'dialog')]}
            break
        check('right-click info in the game window: native popup while held (not sent to the client), gone after release',
              popup is not None and popup['held'][0][:1] == ['dialogBox'] and popup['held'][1] is None
              and popup['released'] == ['adventureMap'] and not popup['to_client'], json.dumps(popup, ensure_ascii=False))

        # --- 3. a dialog opened in the game window is answered from the client ---
        chests = [x for x in snap['objects'] if x['type'] == 101]
        o, to, p = await find_target(c, hero, chests, today=True)
        if o is None:
            check('dialog: chest reachable', False, 'no chest reachable today')
        else:
            if await tile_xy(to) is None:      # out of the game view: walk next to it with a client command first
                await c.cmd({'t': 'cmd', 'cmd': 'move_hero', 'hero': hero, 'to': p['cells'][-2][:3]})
            c.hold_native = {7}         # the chest's choice: answered below
            res0 = list(c.player()['res'])
            k = c.mark()
            walked = await native_walk(to)
            d = await c.wait(lambda m: m.get('t') == 'dialog', 60, k) if walked else None
            if d:
                await asyncio.sleep(1.0)
                await c.shot('33_mirror_chest_dialog.jpg')
                await c.send({'t': 'dialog_answer', 'id': d['id'], 'answer': 0})        # gold
                dc = await c.wait(lambda m: m.get('t') == 'event' and m.get('kind') == 'dialog_closed', 30, k)
                await c.wait(lambda m: m.get('t') == 'state' and m['players'][me]['res'][6] > res0[6], 30, k)
                check('dialog: opened in the game window, mirrored (native), answered from the client',
                      d.get('native') is True and d['style'] == 'choose' and dc.get('by') == 'client' and dc['id'] == d['id'],
                      f"dialog {d['id']} {d['text'][:50]!r} options {[x['text'] for x in d['options']]}; closed {dc}; "
                      f"gold {res0[6]} -> {c.player()['res'][6]}")
            else:
                check('dialog: chest', False, f'chest at {to} not reached natively')
            c.hold_native = set()

        # --- 4. end turn in the game window, its question answered there, AI turns, day 2 ---
        c.hold_native = {2}             # the end-turn question: answered in the game window below; the new day's
        date0 = dict(c.state['date'])   # message (inside NextPlayer) is answered by the client
        k = c.mark()
        await win.key(VK_E, 1.0)
        d = await c.wait(lambda m: m.get('t') == 'dialog', 30, k)
        await asyncio.sleep(0.5)
        yes = await c.eval('widgetCenter(30725)')
        await win.click(*yes, pause=0.5)
        dc = await c.wait(lambda m: m.get('t') == 'event' and m.get('kind') == 'dialog_closed', 30, k)
        back = await c.wait(lambda m: m.get('t') == 'state' and m.get('current') == me and m.get('date') != date0, 600, k)
        turns = [m['current'] for m in c.msgs[k:] if m.get('t') == 'event' and m.get('kind') == 'turn']
        check('end turn in the game window: reminder mirrored, answered there; AI turns; day 2 in the client',
              d.get('native') and d['style'] == 'yesno' and dc.get('by') == 'native' and back['date'] != date0,
              f"dialog {d['text'][:50]!r} closed {dc}; turns {turns}; date {date0} -> {back['date']}")
        await asyncio.sleep(2.0)
        c.hold_native = set()
        await close_native_dialogs()
        await asyncio.sleep(1.0)

        # --- 5. a window opened in the game window (hero screen), closed from the client ---
        st = await c.eval('[players()[me()].heroes, curPlayerData().add(4).readS32(), ptr(adv().add(0x44).readPointer()).isNull()]')
        heroes_l, sel, _ = st
        idx = heroes_l.index(sel) if sel in heroes_l else 0
        portrait = await c.eval(f'(()=>{{ const w = adv().add(0x44).readPointer(); for (let g = w.add(0x28).readPointer(), n = 0; '
                                f'!g.isNull() && n < 400; g = g.add(0xc).readPointer(), n++) if (g.add(0x10).readS16() === {0xf + idx}) '
                                'return [w.add(0x18).readS32() + g.add(0x18).readS16() + (g.add(0x1c).readS16() >> 1), '
                                'w.add(0x1c).readS32() + g.add(0x1a).readS16() + (g.add(0x1e).readS16() >> 1)]; return null; })()')
        k = c.mark()
        wo = {}
        for _ in range(2):      # a click on a portrait selects that hero, a click on the selected one opens his screen
            if not portrait:
                break
            await win.click(*portrait, pause=1.0)
            try:
                wo = await c.wait(lambda m: m.get('t') == 'window_open', 5, k)
                break
            except TimeoutError:
                pass
        if wo:
            await c.wait(lambda m: m.get('t') == 'window_frame', 20, k)
            await asyncio.sleep(1.0)
            await c.fresh_frame()
            c.save_frame('34_mirror_native_hero_window.jpg')
            k2 = c.mark()
            await c.key('Esc')
            wc = await c.wait(lambda m: m.get('t') == 'window_close', 30, k2)
            await c.wait(lambda m: m.get('t') == 'state', 30, k2)
        check('window opened in the game window (hero screen) -> window_open + frames; Esc from the client closes it',
              wo.get('name') == 'heroScreen', f"portrait {portrait} (hero {sel}); {wo}")

        # --- 6. a window opened from the client (kingdom overview), closed in the game window; a client command
        #        sent meanwhile waits for it (commands run on the game thread when the adventure map is idle) ---
        k = c.mark()
        await c.send({'t': 'cmd', 'cmd': 'adv_key', 'key': 'K'})
        wo = await c.wait(lambda m: m.get('t') == 'window_open', 30, k)
        await asyncio.sleep(1.0)
        await c.fresh_frame()
        c.save_frame('35_mirror_kingdom_window.jpg')
        h = c.hero(hero)
        await c.send({'t': 'path', 'hero': hero, 'to': [h['x'], h['y'] + 1, h['z']]})
        await asyncio.sleep(2.0)
        early = next((m for m in c.msgs[k:] if m.get('t') in ('path', 'error')), None)
        await win.click(771, 563, pause=1.0)
        wc = await c.wait(lambda m: m.get('t') == 'window_close', 30, k)
        pa = await c.wait(lambda m: m.get('t') in ('path', 'error'), 30, k)
        order = [m.get('t') for m in c.msgs[k:] if m.get('t') in ('window_close', 'path', 'error')]
        check('window opened from the client (K), closed in the game window; client path queued until then',
              wo.get('name') == 'kingdomOverview' and wc.get('name') == 'kingdomOverview' and early is None and
              order.index('window_close') < order.index(pa['t']), f"order {order}; path {pa.get('cells', pa.get('msg'))}")

        # --- 7. phase change made in the game window: system options -> main menu -> confirm ---
        sysbtn = await c.eval('(()=>{ const w = adv().add(0x44).readPointer(); for (let g = w.add(0x28).readPointer(), n = 0; '
                              '!g.isNull() && n < 400; g = g.add(0xc).readPointer(), n++) if (g.add(0x10).readS16() === 10) '
                              'return [w.add(0x18).readS32() + g.add(0x18).readS16() + (g.add(0x1c).readS16() >> 1), '
                              'w.add(0x1c).readS32() + g.add(0x1a).readS16() + (g.add(0x1e).readS16() >> 1)]; return null; })()')
        k = c.mark()
        await win.click(*sysbtn, pause=1.5)
        wo = await c.wait(lambda m: m.get('t') == 'window_open', 20, k)
        await win.click(565, 433, pause=1.5)
        await c.wait(lambda m: m.get('t') == 'window_open' and m.get('depth') == 2, 15, k)
        await win.click(358, 349, pause=1.0)
        ph = await c.wait(lambda m: m.get('t') == 'phase' and m.get('phase') == 'menu', 30, k)
        await c.wait(lambda m: m.get('t') == 'window_frame' and m.get('name') == 'menu', 30, k)
        await asyncio.sleep(1.5)
        await c.fresh_frame()
        c.save_frame('36_mirror_menu.jpg')
        check('game window: system options -> main menu -> client gets phase menu + menu frames',
              wo.get('name') == 'systemOptions' and ph.get('phase') == 'menu', f"system button {sysbtn}; {wo}; {ph}")

        # --- 8. the client loads the save in the streamed menu (clicks), 9. the player quits in the game window ---
        k = c.mark()
        for a in ((650, 195), 1.5, (650, 82), 5.0, (215, 133), 1.0, (495, 553), 1.0):
            if isinstance(a, float):
                await asyncio.sleep(a)
            else:
                await c.click(*a, pause=0.4)
        adv = await c.wait(lambda m: m.get('t') == 'phase' and m.get('phase') == 'adventure', 60, k)
        sn = await c.wait(lambda m: m.get('t') == 'snapshot', 30, k)
        await asyncio.sleep(1.0)
        k = c.mark()
        sysbtn = await c.eval('(()=>{ const w = adv().add(0x44).readPointer(); for (let g = w.add(0x28).readPointer(), n = 0; '
                              '!g.isNull() && n < 400; g = g.add(0xc).readPointer(), n++) if (g.add(0x10).readS16() === 10) '
                              'return [w.add(0x18).readS32() + g.add(0x18).readS16() + (g.add(0x1c).readS16() >> 1), '
                              'w.add(0x1c).readS32() + g.add(0x1a).readS16() + (g.add(0x1e).readS16() >> 1)]; return null; })()')
        await win.click(*sysbtn, pause=1.5)
        await c.wait(lambda m: m.get('t') == 'window_open', 20, k)
        await win.click(460, 493, pause=1.5)
        await c.wait(lambda m: m.get('t') == 'window_open' and m.get('depth') == 2, 15, k)
        await win.click(358, 349, pause=1.0)
        try:
            ex = await c.wait(lambda m: m.get('t') == 'phase' and m.get('phase') == 'exited', 60, k)
        except TimeoutError:
            ex = None
        check('client loads a save in the menu (adventure + snapshot); quit in the game window -> phase exited',
              adv.get('file') == 'GODOT_TEST' and len(sn['objects']) > 1000 and ex is not None, f"{adv}; {ex}")
        report['external_states'] = sum(1 for m in c.msgs if m.get('t') == 'state' and m.get('why') == 'external')
        report['dialogs'] = [{k2: d2[k2] for k2 in ('id', 'style', 'engine_style', 'native', 'text') if k2 in d2}
                             for d2 in c.dialogs]
        rt.cancel()
    return report


def start_bridge(port, load, log, tag, extra=()):
    blog = open(os.path.join(OUT, f'bridge_log_{tag}.txt'), 'w', encoding='utf-8')
    what = ['--menu'] if load is None else ['--load', load]
    proc = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'bridge.py'), '--port', str(port)] + what +
                            ['--record', os.path.join(OUT, 'rec_' + tag)] + list(extra), stdout=blog,
                            stderr=subprocess.STDOUT)
    log(f'bridge.py pid {proc.pid} ({load or "main menu"}), waiting for the game...')
    return proc


def stop_bridge(proc):
    if proc:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(['taskkill', '/F', '/IM', 'h3hota.exe'], capture_output=True)
        time.sleep(1)


async def wait_bridge(url, proc):
    from websockets.asyncio.client import connect
    for _ in range(120):     # the game needs ~10 s to load
        try:
            async with connect(url):
                return
        except OSError:
            await asyncio.sleep(1)
            if proc and proc.poll() is not None:
                raise RuntimeError('bridge.py exited, see godot/out/bridge/bridge_log_*.txt')
    raise TimeoutError('bridge did not come up')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', help='use a running bridge for the main scenario (then no battle phase)')
    ap.add_argument('--port', type=int, default=8799)
    ap.add_argument('--skip-battle', action='store_true')
    ap.add_argument('--only-battle', action='store_true', help='only phase 2 (GODOT_BATTLE.GM1, manual battle)')
    ap.add_argument('--scenario', choices=('main', 'menu', 'mirror'), default='main',
                    help='main: the two games above (default); menu: bridge.py --menu, the main menu scenario; '
                         'mirror: bridge.py --visible, play in the game window and in the client (PLAN 4.6)')
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, {'menu': 'test_log_menu.txt', 'mirror': 'test_log_mirror.txt'}.get(
        args.scenario, 'test_log.txt')), 'w', encoding='utf-8')

    def log(s):
        print(s, flush=True)
        logf.write(s + '\n')
        logf.flush()

    t0 = time.time()
    report = {'checks': {}, 'notes': []}
    if args.scenario in ('menu', 'mirror'):
        rpath = os.path.join(OUT, f'test_report_{args.scenario}.json')
        proc = None
        try:
            url = args.url
            if not url:
                url = f'ws://127.0.0.1:{args.port}'
                if args.scenario == 'menu':
                    proc = start_bridge(args.port, None, log, 'menu')
                else:
                    proc = start_bridge(args.port, 'GODOT_TEST.GM1', log, 'mirror', ('--visible', '--debug'))
            asyncio.run(wait_bridge(url, proc))
            asyncio.run((scenario_menu if args.scenario == 'menu' else scenario_mirror)(url, log, report))
        except Exception as e:
            report['checks']['no exception'] = {'ok': False, 'info': f'{type(e).__name__}: {e}'}
            log(f'[FAIL] {type(e).__name__}: {e}')
        finally:
            stop_bridge(proc)
        report['seconds'] = round(time.time() - t0, 1)
        report['ok'] = all(v['ok'] for v in report['checks'].values())
        with open(rpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log(f"{'ALL PASSED' if report['ok'] else 'FAILED'} in {report['seconds']} s; report {rpath}")
        sys.exit(0 if report['ok'] else 1)
    try:
        # phase 1: The Newcomers (GODOT_TEST.GM1)
        proc = None
        url = args.url
        if not url:
            url = f'ws://127.0.0.1:{args.port}'
            if not args.only_battle:
                proc = start_bridge(args.port, 'GODOT_TEST.GM1', log, 'main')
        if not args.only_battle:
            try:
                asyncio.run(wait_bridge(url, proc))
                report = asyncio.run(scenario(url, log))
            finally:
                stop_bridge(proc)
        # phase 2: battle (GODOT_BATTLE.GM1)
        if not args.url and not args.skip_battle:
            proc = start_bridge(args.port, 'GODOT_BATTLE.GM1', log, 'battle', ('--battles', 'manual'))
            try:
                asyncio.run(wait_bridge(url, proc))
                asyncio.run(scenario_battle(url, log, report))
            finally:
                stop_bridge(proc)
    except Exception as e:
        report['checks']['no exception'] = {'ok': False, 'info': f'{type(e).__name__}: {e}'}
        log(f'[FAIL] {type(e).__name__}: {e}')
    report['seconds'] = round(time.time() - t0, 1)
    ok = all(v['ok'] for v in report['checks'].values())
    report['ok'] = ok
    with open(os.path.join(OUT, 'test_report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log(f"{'ALL PASSED' if ok else 'FAILED'} in {report['seconds']} s; report {os.path.join(OUT, 'test_report.json')}")
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
