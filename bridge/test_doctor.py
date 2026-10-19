"""Unit tests of doctor.py without a real Linux/Wine: fake home folders (Wine prefixes, Lutris, Steam/Proton,
saved client setting) and mocked commands (wine --version, 32-bit cmd.exe, Windows python probe).
Runs on Windows and Linux:  python -I godot/bridge/test_doctor.py  [-v]"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doctor  # noqa: E402

GOOD_PROBE = 'PROBE' + json.dumps({'python': '3.12.10', 'bits': 64, 'exe': 'C:\\py\\python.exe',
                                   'frida': '17.2.1', 'websockets': '13.1'})


def make_game(d, files=doctor.REQUIRED, case=False):
    for f in files:
        p = os.path.join(d, *(f.lower() if case else f).split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(b'x')
    os.makedirs(os.path.join(d, 'Maps'), exist_ok=True)


def make_prefix(p, arch='win64', python=True, user='u'):
    os.makedirs(os.path.join(p, 'drive_c', 'windows'), exist_ok=True)
    with open(os.path.join(p, 'system.reg'), 'w', encoding='utf-8') as f:
        f.write('WINE REGISTRY Version 2\n;; All keys relative to \\\\Machine\n\n#arch=%s\n\n' % arch)
    if python:
        d = os.path.join(p, 'drive_c', 'users', user, 'AppData', 'Local', 'Programs', 'Python', 'Python312')
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, 'python.exe'), 'wb').close()
        d11 = d.replace('Python312', 'Python311')
        os.makedirs(d11, exist_ok=True)
        open(os.path.join(d11, 'python.exe'), 'wb').close()


class FakeHost(doctor.Host):
    def __init__(self, home, env=None, wine=True, wine32=True, probe=GOOD_PROBE, busy=(), distro='ubuntu'):
        super().__init__('Linux', dict({'PATH': '/usr/bin', 'DISPLAY': ':0'}, **(env or {})), home)
        self.wine, self.wine32, self.probe, self.busy, self.distro = wine, wine32, probe, set(busy), distro
        self.calls = []

    def which(self, name):
        return '/usr/bin/wine' if name == 'wine' and self.wine else None

    def run(self, cmd, timeout=60, env=None):
        self.calls.append((cmd, (env or {}).get('WINEPREFIX')))
        if cmd[1:] == ['--version']:
            return 0, 'wine-9.0\n'
        if cmd[1].lower().endswith('cmd.exe'):
            return (0, 'hota32ok\r\n') if self.wine32 else (1, 'wine: could not load kernel32.dll, status c0000135\n')
        if cmd[1].endswith('python.exe'):
            return (0, self.probe) if self.probe else (1, 'wine: cannot find python.exe')
        if cmd[1] == 'wineboot':                       # first run of the client's own prefix
            make_prefix(env['WINEPREFIX'], python=False)
            return 0, ''
        if cmd[0] == 'pgrep':
            return 1, ''
        return None, 'unexpected ' + ' '.join(cmd)

    def port_free(self, port):
        return port not in self.busy

    def os_release(self):
        return {'ID': self.distro, 'PRETTY_NAME': self.distro.title() + ' 24.04'}


def by_id(r):
    return {c['id']: c for c in r['checks']}


class DoctorLinux(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='doctor_home_')
        self._repo = doctor.REPO_GAME_DIR
        doctor.REPO_GAME_DIR = os.path.join(self.home, 'no-repo-game')    # the VM's own game must not be found

    def tearDown(self):
        doctor.REPO_GAME_DIR = self._repo
        shutil.rmtree(self.home, ignore_errors=True)

    def h(self, *p):
        return os.path.join(self.home, *p)

    def test_nothing_installed(self):
        r = doctor.diagnose(FakeHost(self.home, wine=False))
        c = by_id(r)
        self.assertFalse(r['ok'])
        self.assertEqual(c['game_dir']['status'], 'fail')
        self.assertEqual(c['wine']['status'], 'fail')
        self.assertIn('apt install wine wine32', c['wine']['fix'])
        self.assertIsNone(r['launch'])

    def test_wine_hint_per_distro(self):
        for distro, want in (('fedora', 'dnf install wine'), ('arch', 'pacman -S wine')):
            c = by_id(doctor.diagnose(FakeHost(self.home, wine=False, distro=distro)))
            self.assertIn(want, c['wine']['fix'])

    def test_game_in_default_prefix_no_python(self):
        pfx = self.h('.wine')
        make_prefix(pfx, python=False)
        make_game(os.path.join(pfx, 'drive_c', 'GOG Games', 'HoMM 3 Complete'), case=True)   # data/h3bitmap.lod
        host = FakeHost(self.home)
        r = doctor.diagnose(host)
        c = by_id(r)
        self.assertEqual(c['game_dir']['status'], 'ok', c['game_dir'])
        self.assertIn('HoMM 3 Complete', r['game_dir'])
        self.assertEqual(c['wine']['status'], 'ok')
        self.assertIn('wine-9.0', c['wine']['detail'])
        self.assertEqual(c['wineprefix']['status'], 'ok')
        self.assertIn('win64', c['wineprefix']['detail'])
        self.assertEqual(c['wine32']['status'], 'ok')
        self.assertEqual(c['wine_python']['status'], 'fail')
        self.assertIn('python-3.12', c['wine_python']['fix'])
        self.assertIn(pfx, c['wine_python']['fix'])
        self.assertFalse(r['ok'])
        # wine commands ran in the game's prefix, 32-bit test through syswow64
        self.assertTrue(any('syswow64' in cmd[1] and p == pfx for cmd, p in host.calls))

    def test_frida_missing_in_wine_python(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        make_game(os.path.join(pfx, 'drive_c', 'Games', 'HotA'))
        probe = 'PROBE' + json.dumps({'python': '3.12.10', 'bits': 64, 'exe': 'C:\\p\\python.exe',
                                      'frida_error': "ModuleNotFoundError: No module named 'frida'",
                                      'websockets': '12.0'})
        c = by_id(doctor.diagnose(FakeHost(self.home, probe=probe)))
        self.assertEqual(c['frida']['status'], 'fail')
        self.assertIn('wine', c['frida']['fix'])
        self.assertIn('-m pip install', c['frida']['fix'])
        self.assertIn('Python312', c['frida']['fix'])                    # highest version chosen
        self.assertEqual(c['websockets']['status'], 'fail')              # 12 < 13
        self.assertIn('websockets>=13', c['websockets']['fix'])

    def test_all_good_launch_command(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        game = os.path.join(pfx, 'drive_c', 'GOG Games', 'HoMM 3 Complete')
        make_game(game)
        ctl = self.h('ctl')
        os.makedirs(ctl)
        r = doctor.diagnose(FakeHost(self.home), control_dir=ctl, load='MY.GM1')
        self.assertTrue(r['ok'], [c for c in r['checks'] if c['status'] == 'fail'])
        L = r['launch']
        self.assertTrue(L['wine'])
        self.assertEqual(L['cmd'][0], '/usr/bin/wine')
        self.assertTrue(L['cmd'][1].endswith('python.exe'))
        self.assertEqual(L['cmd'][L['cmd'].index('--game-dir') + 1], 'C:\\GOG Games\\HoMM 3 Complete')
        self.assertTrue(L['cmd'][3].startswith('Z:\\') and L['cmd'][3].endswith('bridge.py'), L['cmd'][3])
        self.assertTrue(L['cmd'][L['cmd'].index('--control-dir') + 1].startswith('Z:\\'))
        self.assertIn('--load', L['cmd'])
        self.assertNotIn('--menu', L['cmd'])
        self.assertEqual(L['env']['WINEPREFIX'], pfx)
        self.assertEqual(L['url'], 'ws://127.0.0.1:8765')
        # no save given: the game starts at its main menu (PLAN 4.5)
        L2 = doctor.diagnose(FakeHost(self.home), control_dir=ctl)['launch']
        self.assertIn('--menu', L2['cmd'])
        self.assertNotIn('--load', L2['cmd'])

    def test_bundled_python_own_prefix(self):
        # package layout: <pkg>/python/python.exe next to bridge/ -> own prefix ~/.local/share/HotA3D/wine, created
        # on first run, even though the game lies in ~/.wine; HOTA3D_WINE_PYTHON still wins
        gpfx = self.h('.wine')
        make_prefix(gpfx, python=False)
        game = os.path.join(gpfx, 'drive_c', 'GOG Games', 'HoMM 3 Complete')
        make_game(game)
        bpy = self.h('pkg', 'python', 'python.exe')
        os.makedirs(os.path.dirname(bpy))
        open(bpy, 'wb').close()
        old, doctor.BUNDLED_PY = doctor.BUNDLED_PY, bpy
        try:
            host = FakeHost(self.home)
            r = doctor.diagnose(host)
            c = by_id(r)
            own = self.h('.local', 'share', 'HotA3D', 'wine')
            self.assertTrue(r['ok'], [x for x in r['checks'] if x['status'] == 'fail'])
            self.assertEqual(c['wineprefix_new']['detail'], own)
            self.assertIn(own, c['wineprefix']['detail'])
            self.assertIn('wineprefix_game', c)
            self.assertIn('из пакета', c['wine_python']['detail'])
            self.assertEqual(r['launch']['cmd'][1], bpy)
            self.assertEqual(r['launch']['env']['WINEPREFIX'], own)
            self.assertTrue(r['launch']['cmd'][r['launch']['cmd'].index('--game-dir') + 1].startswith('Z:\\'))
            self.assertTrue(any(cmd[1] == 'wineboot' and p == own for cmd, p in host.calls))
            r2 = doctor.diagnose(FakeHost(self.home, env={'HOTA3D_WINE_PYTHON': '/opt/py/python.exe'}))
            self.assertEqual(r2['launch']['cmd'][1], '/opt/py/python.exe')
            self.assertEqual(r2['launch']['env']['WINEPREFIX'], gpfx)          # user's Python: the game's prefix
        finally:
            doctor.BUNDLED_PY = old

    def test_explicit_dir_outside_prefix_and_port_busy(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        game = self.h('games', 'hota')
        make_game(game)
        r = doctor.diagnose(FakeHost(self.home, busy={8765, 8766}), game_dir=game)
        c = by_id(r)
        self.assertEqual(c['port']['status'], 'warn')
        self.assertEqual(r['port'], 8767)
        self.assertEqual(c['wineprefix']['status'], 'ok')                # ~/.wine (game not in a prefix)
        gd = r['launch']['cmd'][r['launch']['cmd'].index('--game-dir') + 1]
        self.assertTrue(gd.startswith('Z:\\') and gd.endswith('games\\hota'), gd)

    def test_lutris_prefix_vs_wineprefix(self):
        lut = self.h('Games', 'heroes-of-might-and-magic-3')
        make_prefix(lut)
        make_game(os.path.join(lut, 'drive_c', 'GOG Games', 'HoMM 3 Complete'))
        other = self.h('.wine')
        make_prefix(other)
        r = doctor.diagnose(FakeHost(self.home, env={'WINEPREFIX': other}))
        c = by_id(r)
        self.assertIn('heroes-of-might-and-magic-3', r['game_dir'])
        warn = [x for x in r['checks'] if x['id'] == 'wineprefix' and x['status'] == 'warn']
        self.assertTrue(warn and lut in warn[0]['fix'], r['checks'])
        # without WINEPREFIX the game's own prefix is used
        r2 = doctor.diagnose(FakeHost(self.home))
        self.assertEqual(r2['launch']['env']['WINEPREFIX'], lut)

    def test_registry_apppath(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        game = os.path.join(pfx, 'drive_c', 'Weird', 'Place', 'H3')
        make_game(game)
        with open(os.path.join(pfx, 'system.reg'), 'a', encoding='utf-8') as f:
            f.write('[Software\\\\Wow6432Node\\\\New World Computing\\\\Heroes of Might and Magic\\xae III\\\\1.0] 1700000000\n'
                    '"AppPath"="C:\\\\Weird\\\\Place\\\\H3\\\\"\n"Quick Combat"=dword:00000001\n\n')
        cands = doctor.candidate_dirs(FakeHost(self.home))
        hit = [(p, s) for p, s in cands if p == os.path.normpath(game)]
        self.assertTrue(hit and hit[0][1].startswith('реестр Wine'), cands)
        self.assertEqual(doctor.find_game_dir(FakeHost(self.home))[0], os.path.normpath(game))

    def test_wine32_missing(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        make_game(os.path.join(pfx, 'drive_c', 'HotA'))
        c = by_id(doctor.diagnose(FakeHost(self.home, wine32=False)))
        self.assertEqual(c['wine32']['status'], 'fail')
        self.assertIn('wine32', c['wine32']['fix'])

    def test_partial_folder(self):
        game = self.h('Games', 'HotA')
        make_game(game, files=['h3hota.exe', 'Data/H3bitmap.lod'])
        c = by_id(doctor.diagnose(FakeHost(self.home, wine=False)))
        self.assertEqual(c['game_dir']['status'], 'fail')
        self.assertIn('HotA.dll', c['game_dir']['detail'])
        self.assertIn('Data/HotA.lod', c['game_dir']['detail'])

    def test_explicit_invalid_dir_not_replaced(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        make_game(os.path.join(pfx, 'drive_c', 'HotA'))                  # a valid one exists elsewhere
        empty = self.h('empty')
        os.makedirs(empty)
        r = doctor.diagnose(FakeHost(self.home), game_dir=empty)
        self.assertIsNone(r['game_dir'])
        self.assertEqual(by_id(r)['game_dir']['status'], 'fail')

    def test_saved_setting_from_client(self):
        game = self.h('somewhere', 'H3')
        make_game(game)
        cfgdir = self.h('xdg', 'godot', 'app_userdata', doctor.APP_NAME)
        os.makedirs(cfgdir)
        with open(os.path.join(cfgdir, 'local.cfg'), 'w', encoding='utf-8') as f:
            f.write('[local]\n\nmode="local"\n\n[game]\n\ndir="%s"\n' % game.replace('\\', '\\\\'))
        host = FakeHost(self.home, env={'XDG_DATA_HOME': self.h('xdg')}, wine=False)
        d, src, _ = doctor.find_game_dir(host)
        self.assertEqual(d, os.path.normpath(game))
        self.assertEqual(src, 'сохранена клиентом')

    def test_env_var(self):
        game = self.h('env', 'H3')
        make_game(game)
        d, src, _ = doctor.find_game_dir(FakeHost(self.home, env={'HOTA_GAME_DIR': game}))
        self.assertEqual((d, src), (os.path.normpath(game), 'HOTA_GAME_DIR'))

    def test_proton_prefix(self):
        pfx = self.h('.local', 'share', 'Steam', 'steamapps', 'compatdata', '297000', 'pfx')
        make_prefix(pfx)
        make_game(os.path.join(pfx, 'drive_c', 'HotA'))
        r = doctor.diagnose(FakeHost(self.home))
        self.assertEqual(by_id(r)['proton']['status'], 'warn')
        self.assertEqual(r['launch']['env']['WINEPREFIX'], os.path.realpath(pfx))

    def test_no_display_and_prefix_missing(self):
        make_game(self.h('Games', 'HotA'))
        r = doctor.diagnose(FakeHost(self.home, env={'DISPLAY': ''}))
        c = by_id(r)
        self.assertEqual(c['wineprefix']['status'], 'fail')              # ~/.wine does not exist
        self.assertIn('wineboot', c['wineprefix']['fix'])

    def test_wine_path_roundtrip(self):
        pfx = self.h('.wine')
        make_prefix(pfx)
        p = os.path.join(pfx, 'drive_c', 'GOG Games', 'X')
        os.makedirs(p)
        w = doctor.to_wine_path(p, pfx)
        self.assertEqual(w, 'C:\\GOG Games\\X')
        self.assertEqual(doctor.from_wine_path(w, pfx), os.path.normpath(p))
        self.assertEqual(doctor.prefix_of(p), pfx)
        self.assertIsNone(doctor.prefix_of(self.h('x', 'y')))

    def test_json_ascii(self):
        r = doctor.diagnose(FakeHost(self.home, wine=False))
        json.dumps(r, ensure_ascii=True).encode('ascii')

    def test_visible_game_window(self):
        # PLAN 4.6: auto -> visible with a display (bridge.py --visible), hidden without; on/off override
        pfx = self.h('.wine')
        make_prefix(pfx)
        make_game(os.path.join(pfx, 'drive_c', 'GOG Games', 'HoMM 3 Complete'))
        r = doctor.diagnose(FakeHost(self.home))
        self.assertTrue(r['visible'])
        self.assertIn('--visible', r['launch']['cmd'])
        self.assertEqual(by_id(r)['game_window']['status'], 'info')
        r = doctor.diagnose(FakeHost(self.home, env={'DISPLAY': '', 'WAYLAND_DISPLAY': 'wayland-0'}))
        self.assertTrue(r['visible'])
        r = doctor.diagnose(FakeHost(self.home), visible='off')
        self.assertFalse(r['visible'])
        self.assertNotIn('--visible', r['launch']['cmd'])
        host = FakeHost(self.home, env={'DISPLAY': ''})
        self.assertEqual(doctor.resolve_visible(host, 'auto')[0], False)
        self.assertEqual(doctor.resolve_visible(host, 'on')[0], True)


class DoctorWindows(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows only')
    def test_this_machine(self):
        r = doctor.diagnose(doctor.Host(), port=8765)
        ids = by_id(r)
        for k in ('os', 'game_dir', 'bridge_files', 'python', 'frida', 'websockets', 'port'):
            self.assertIn(k, ids)
        if r['ok']:
            self.assertEqual(r['launch']['cmd'][0], sys.executable)
            self.assertIn('--game-dir', r['launch']['cmd'])
            # the game window: auto follows the window station (session 0 here: hidden), on forces --visible
            vis, _ = doctor.Host().interactive_desktop()
            self.assertEqual(r['visible'], vis)
            self.assertEqual('--visible' in r['launch']['cmd'], vis)
            r2 = doctor.diagnose(doctor.Host(), port=8765, visible='on')
            self.assertIn('--visible', r2['launch']['cmd'])


if __name__ == '__main__':
    unittest.main()
