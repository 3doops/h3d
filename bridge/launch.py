"""Start the original HotA (h3hota.exe, no HD mod) under Frida (hidden; --visible: on the desktop) and bring it to a
loaded adventure map without a human.

    python -I launch.py                        # load GODOT_TEST.GM1, print a summary, save a shot, kill the game
    python -I launch.py --keep                 # ... and keep the game running (and this session attached)
    python -I launch.py --load AUTOSAVE.GM1    # any save from game\\Heroes3_HotA1\\games
    python -I launch.py --new "The Newcomers.h3m" --save GODOT_TEST   # new game (first human slot), save it
    python -I launch.py --menu --keep          # stop at the main menu
    python -I launch.py --visible --keep       # the game window on the desktop (default: hidden), PLAN 4.6
    python -I launch.py --kill                 # kill every h3hota.exe
    python -I launch.py --game-dir D:/Games/HotA     # another game folder (default: env HOTA_GAME_DIR, else
                                               #   game/Heroes3_HotA1 of this repo)

As a module (bridge.py, tools):

    from launch import Game
    g = Game.start(load='GODOT_TEST.GM1')      # spawn + wait for the adventure map (raises on timeout)
    g.api.state(); g.api.players(); g.api.heroes(); g.shot('x.png')
    s = g.session.create_script(src); s.load()  # add your own agent to the same session
    g.kill()

    g = Game.attach()                           # attach to an already running game (e.g. launch.py --keep)

See README.md, section "Запуск игры".
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib

import frida

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_GAME_DIR = os.path.normpath(os.path.join(HERE, '..', '..', 'game', 'Heroes3_HotA1'))
# game folder: set_game_dir() (--game-dir of launch.py / bridge.py) > env HOTA_GAME_DIR > the repo's copy
GAME_DIR = os.path.normpath(os.environ.get('HOTA_GAME_DIR') or REPO_GAME_DIR)
EXE = 'h3hota.exe'
TEST_SAVE = 'GODOT_TEST.GM1'
REG_KEY = 'SOFTWARE\\WOW6432Node\\New World Computing\\Heroes of Might and Magic® III\\1.0'
# Registry values the plain exe reads at start (the HD exe keeps its own copy in _HD3_Data\Settings\hota.ini).
# Autosave off: never overwrite the user's AUTOSAVE.GM1.
REG_VALUES = {'Main Game Full Screen': 0, 'Show Intro': 0, 'First Time': 0, 'Music Volume': 0, 'Sound Volume': 0,
              'Walk Speed': 3, 'Computer Walk Speed': 3, 'Quick Combat': 1, 'Autosave': 0}
MENU_NEW, MENU_LOAD, GAME_TYPE_SINGLE = 0x65, 0x66, 100
BEGIN_BUTTON = (495, 553)   # "Начать" / "Загрузить" in the scenario dialog (800x600 layout)
SCAN_ENTER = 0x1C
ON_SPAWN = None             # callable(pid): told right after spawn (bridge.py --control-dir: status.json game_pid)
# visible mode (PLAN 4.6): the player plays in the game window too, so his own sound / walk speed settings stay
VISIBLE_KEEP = ('Music Volume', 'Sound Volume', 'Walk Speed', 'Computer Walk Speed')
IDLE_POLLS = 16             # sleep 1 ms per 16 empty input polls on the adventure map: ~75% -> ~10% of a core


def png_rgb565(data, w, h, pitch):
    """RGB565 rows -> PNG bytes (no third-party modules)."""
    out = bytearray()
    for y in range(h):
        px = struct.unpack_from('<%dH' % w, data, y * pitch)
        row = bytearray(w * 3)
        row[0::3] = bytes(((p >> 8) & 0xF8) | (p >> 13) for p in px)
        row[1::3] = bytes(((p >> 3) & 0xFC) | ((p >> 9) & 3) for p in px)
        row[2::3] = bytes(((p << 3) & 0xF8) | ((p >> 2) & 7) for p in px)
        out += b'\0' + row

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(bytes(out), 6)) + chunk(b'IEND', b''))


def set_game_dir(path):
    """Use another game folder (must contain h3hota.exe). Module functions read GAME_DIR at call time."""
    global GAME_DIR
    path = os.path.normpath(os.path.abspath(path))
    if not os.path.isfile(os.path.join(path, EXE)):
        raise FileNotFoundError(f'{EXE} not found in {path}')
    GAME_DIR = path
    return path


# Values changed by set_registry() and their previous contents ({name: (type, value) | None}), so that
# restore_registry() gives the player's own settings back (full screen, sound, autosave) when the bridge ends.
_REG_BACKUP = {}
_REG_ROOT = None


def _reg_open():
    """HKLM, as the game reads it. Without admin rights HKLM is read-only; h3hota.exe has no UAC manifest, so
    Windows virtualizes its HKLM access to HKCU\\Software\\Classes\\VirtualStore\\MACHINE\\... - write there then."""
    import winreg
    try:
        return (winreg.HKEY_LOCAL_MACHINE, REG_KEY,
                winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, REG_KEY, 0, winreg.KEY_ALL_ACCESS))
    except PermissionError:
        vkey = 'Software\\Classes\\VirtualStore\\MACHINE\\' + REG_KEY
        return (winreg.HKEY_CURRENT_USER, vkey,
                winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, vkey, 0, winreg.KEY_ALL_ACCESS))


def set_registry(visible=False):
    import winreg
    global _REG_ROOT
    root, path, key = _reg_open()
    values = dict(REG_VALUES, AppPath=GAME_DIR + '\\')
    if visible:
        for name in VISIBLE_KEEP:
            values.pop(name, None)
    if _REG_ROOT is None:
        _REG_ROOT = (root, path)
        for name in values:
            try:
                v, t = winreg.QueryValueEx(key, name)
                _REG_BACKUP[name] = (t, v)
            except FileNotFoundError:
                _REG_BACKUP[name] = None
    for name, value in values.items():
        if isinstance(value, str):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        else:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
    winreg.CloseKey(key)
    if root != winreg.HKEY_LOCAL_MACHINE:
        print('[launch] no admin rights: game settings written to the HKCU VirtualStore', flush=True)


def restore_registry():
    """Put back what set_registry() changed (call after the game exited; no-op if nothing was changed)."""
    global _REG_ROOT
    if _REG_ROOT is None:
        return
    import winreg
    root, path = _REG_ROOT
    try:
        key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    for name, old in _REG_BACKUP.items():
        try:
            if old is None:
                winreg.DeleteValue(key, name)
            else:
                winreg.SetValueEx(key, name, 0, old[0], old[1])
        except OSError:
            pass
    winreg.CloseKey(key)
    _REG_ROOT = None
    _REG_BACKUP.clear()


def game_pids():
    return [p.pid for p in frida.get_local_device().enumerate_processes() if p.name.lower() == EXE]


def kill_pid(pid):
    """taskkill one process (also works inside Wine)."""
    return subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True).returncode == 0


def kill_all():
    r = subprocess.run(['taskkill', '/F', '/IM', EXE], capture_output=True, text=True)
    return r.returncode == 0


class Game:
    def __init__(self, device, pid, session, script, verbose=True):
        self.device, self.pid, self.session, self.script = device, pid, session, script
        self.api = script.exports_sync
        self.verbose = verbose
        self._shots = []
        self.on_payload = None   # callable(payload, data) -> bool; True = consumed (bridge.py)

    # ---- process ----
    @classmethod
    def spawn(cls, verbose=True, extra=(), visible=False):
        """Spawn suspended with the agent loaded; call resume() after configure(). visible: the game window is a
        normal window on the desktop (PLAN 4.6), otherwise it is created hidden."""
        set_registry(visible)
        device = frida.get_local_device()
        pid = device.spawn([os.path.join(GAME_DIR, EXE)], cwd=GAME_DIR)
        if ON_SPAWN is not None:
            ON_SPAWN(pid)
        return cls._attach(device, pid, verbose, mode='spawn', extra=extra, visible=visible)

    @classmethod
    def attach(cls, pid=None, verbose=True, extra=(), visible=False):
        """Attach to a running game (its startup fixes are already in place if it was started by launch.py)."""
        device = frida.get_local_device()
        if pid is None:
            pids = game_pids()
            if not pids:
                raise RuntimeError(EXE + ' is not running')
            pid = pids[0]
        return cls._attach(device, pid, verbose, extra=extra, visible=visible)

    @classmethod
    def _attach(cls, device, pid, verbose, mode='attach', extra=(), visible=False):
        """extra: more agent files appended to game_agent.js in one script (bridge.py: 'bridge_agent.js').
        visible: AGENT_VISIBLE for the agent (visible game window, PLAN 4.6)."""
        session = device.attach(pid)
        src = f"var AGENT_MODE = '{mode}';\nvar AGENT_VISIBLE = {'true' if visible else 'false'};\n"
        for name in ('game_agent.js',) + tuple(extra):
            with open(os.path.join(HERE, name), encoding='utf-8') as f:
                src += f.read() + '\n'
        script = session.create_script(src)
        g = cls(device, pid, session, script, verbose)
        script.on('message', g._on_message)
        script.load()
        return g

    @classmethod
    def start(cls, load=TEST_SAVE, new=None, save=None, timeout=60, verbose=True, extra=(), visible=False):
        """Spawn the game and bring it to the adventure map: load a save (GAMES\\<load>) or start a new game on
        Maps\\<new> (and optionally save it as GAMES\\<save>.GM1)."""
        g = cls.spawn(verbose, extra, visible)
        try:
            g.api.configure({'menu': MENU_NEW if new else MENU_LOAD, 'gameType': GAME_TYPE_SINGLE,
                             'fileFilter': new or load})
            g.resume()
            g.wait_phase('select-scenario', timeout)
            time.sleep(0.5)
            g.api.click(*BEGIN_BUTTON)
            g.wait_phase(('adventure', 'adventure-dialog'), timeout)
            g.api.configure({'menu': None, 'gameType': None, 'fileFilter': None})
            if new:
                g.close_dialogs(timeout=10)
                if save:
                    g.save(save)
            else:
                g.wait_phase('adventure', 10)
            g.api.idle_polls(IDLE_POLLS)
            return g
        except Exception:
            g.kill()
            raise

    def resume(self):
        self.device.resume(self.pid)

    def alive(self):
        try:
            return self.pid in game_pids()
        except frida.InvalidOperationError:
            return False

    def kill(self):
        try:
            self.device.kill(self.pid)
        except (frida.ProcessNotFoundError, frida.InvalidOperationError):
            pass

    # ---- engine ----
    def _on_message(self, msg, data):
        if msg['type'] == 'error':
            print('[agent error]', msg.get('stack') or msg.get('description'), flush=True)
            return
        p = msg['payload']
        if self.on_payload is not None and self.on_payload(p, data):
            return
        if isinstance(p, dict) and p.get('t') == 'shot':
            self._shots.append((p, data))
        elif isinstance(p, dict) and p.get('t') == 'log':
            if self.verbose:
                print('[game]', p['msg'], flush=True)
        elif self.verbose:
            print('[agent]', p, flush=True)

    def wait_phase(self, phases, timeout=30):
        phases = (phases,) if isinstance(phases, str) else tuple(phases)
        end = time.time() + timeout
        st = None
        while time.time() < end:
            if not self.alive():
                raise RuntimeError('game process exited')
            st = self.api.state()
            if st['phase'] in phases:
                return st
            time.sleep(0.25)
        raise TimeoutError(f'phase {phases} not reached in {timeout}s, state: {st}')

    def close_dialogs(self, timeout=10):
        """Press Enter while a modal dialog is on top of the adventure map (scenario intro, day-1 events)."""
        end = time.time() + timeout
        while time.time() < end:
            if self.api.state()['phase'] == 'adventure':
                return True
            self.api.key(SCAN_ENTER)
            time.sleep(1)
        return False

    def run_job(self, job_id, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            r = self.api.job(job_id)
            if r and r['done']:
                if r.get('error'):
                    raise RuntimeError(r['error'])
                return r.get('result')
            time.sleep(0.1)
        raise TimeoutError(f'job {job_id}')

    def save(self, name):
        """Save via game::SaveGame to GAMES\\<name>.GM1 (on the game thread)."""
        self.run_job(self.api.save(name))
        return os.path.join(GAME_DIR, 'games', name + '.GM1')

    def shot(self, path):
        """Save the engine's screen bitmap (what the game would show) as PNG."""
        self._shots.clear()
        if self.api.shot() is None:
            return False
        for _ in range(100):
            if self._shots:
                break
            time.sleep(0.05)
        p, data = self._shots.pop()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(png_rgb565(data, p['w'], p['h'], p['pitch']))
        return True

    def summary(self):
        st = self.api.state()
        players = self.api.players()
        heroes = self.api.heroes()
        return {'state': st, 'addresses': self.api.addresses(), 'terrain': self.api.terrain_stats(),
                'players': [p for p in players if p['alive'] and (p['numHeroes'] or p['numTowns'])],
                'heroes': heroes}


def print_summary(s):
    st = s['state']
    d = st['date']
    print(f"phase={st['phase']} file={st['file']!r} map={st['mapSize']}x{st['mapSize']}x{st['levels']} "
          f"date: month {d['month']} week {d['week']} day {d['day']}")
    print('addresses:', s['addresses'])
    names = ['dirt', 'sand', 'grass', 'snow', 'swamp', 'rough', 'subterranean', 'lava', 'water', 'rock']
    for z, hist in enumerate(s['terrain']):
        print(f'  terrain z={z}:', ', '.join(f'{names[int(t)] if int(t) < len(names) else t} {n}'
                                            for t, n in sorted(hist.items(), key=lambda kv: int(kv[0]))))
    colors = ['red', 'blue', 'tan', 'green', 'orange', 'purple', 'teal', 'pink']
    for p in s['players']:
        print(f"  player {p['id']} {colors[p['id']]:6} {'HUMAN' if p['human'] else 'AI   '} heroes={p['heroes']} "
              f"towns={p['numTowns']} res={p['res']}")
        for h in s['heroes']:
            if h['owner'] == p['id']:
                print(f"      hero {h['id']:3} {h['name']:12} at ({h['x']},{h['y']},{h['z']}) lvl {h['level']} "
                      f"mp {h['mp']}/{h['mp_max']}")


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--load', default=TEST_SAVE, help='save file in games\\ (default %(default)s)')
    ap.add_argument('--new', help='start a new game on Maps\\<file> instead of loading')
    ap.add_argument('--save', help='after --new: save as games\\<name>.GM1 and copy to testdata\\')
    ap.add_argument('--menu', action='store_true', help='only start and stop at the main menu')
    ap.add_argument('--visible', action='store_true',
                    help='the game window on the desktop (default: created hidden); you can play in it')
    ap.add_argument('--shot', default=os.path.join(HERE, 'out', 'launch.png'), help='PNG of the engine screen')
    ap.add_argument('--json', help='write the summary as JSON')
    ap.add_argument('--keep', action='store_true', help='keep the game running until Ctrl+C')
    ap.add_argument('--kill', action='store_true', help='kill all running h3hota.exe and exit')
    ap.add_argument('--timeout', type=float, default=60)
    ap.add_argument('--game-dir', help='game folder with h3hota.exe (default: env HOTA_GAME_DIR, else %s)'
                    % REPO_GAME_DIR)
    args = ap.parse_args()
    if args.game_dir:
        set_game_dir(args.game_dir)
    if args.kill:
        print('killed' if kill_all() else 'no game process')
        return
    t0 = time.time()
    if args.menu:
        g = Game.spawn(visible=args.visible)
        g.resume()
        g.wait_phase('main-menu', args.timeout)
        time.sleep(1)
    else:
        g = Game.start(load=args.load, new=args.new, save=args.save, timeout=args.timeout, visible=args.visible)
    try:
        print(f'pid {g.pid}, ready in {time.time() - t0:.1f}s')
        if not args.menu:
            s = g.summary()
            print_summary(s)
            if args.json:
                with open(args.json, 'w', encoding='utf-8') as f:
                    json.dump(s, f, ensure_ascii=False, indent=1)
        if args.save and args.new:
            src = os.path.join(GAME_DIR, 'games', args.save + '.GM1')
            dst = os.path.join(HERE, 'testdata', args.save + '.GM1')
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print('saved', src, '->', dst)
        if args.shot:
            print('shot', args.shot, g.shot(args.shot))
        if args.keep:
            print('running; Ctrl+C or "launch.py --kill" to stop', flush=True)
            while g.alive():
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        g.kill()
        time.sleep(0.5)
        restore_registry()


if __name__ == '__main__':
    main()
