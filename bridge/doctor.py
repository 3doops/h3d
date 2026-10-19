"""HotA 3D local mode: find the original game, check everything the bridge needs, print what is missing and how to
fix it, and (for the client) say how to start the bridge. Standard library only; runs on Windows and Linux.

    python doctor.py                         # checklist for this machine (game folder searched automatically)
    python doctor.py --game-dir "D:/Games/HotA3"
    python doctor.py --json [--out FILE]     # machine-readable (the Godot client runs this), ASCII-only JSON
    python doctor.py --find                  # only list game folder candidates
    python doctor.py --visible on|off|auto   # the original game window on the desktop (PLAN 4.6; default auto:
                                             # visible when there is a desktop) -> bridge.py --visible

Windows: the bridge runs under this Python (needs frida + websockets), the game starts natively.
Linux:   the game is a Windows program, so it runs under Wine; the bridge (Frida instruments the Windows process,
         and bridge.py itself uses Windows APIs: winreg, GDI+, MapVirtualKey) runs under a *Windows* Python with
         frida + websockets installed into the same WINEPREFIX:  wine python.exe bridge.py --game-dir Z:\\...
         The WebSocket port is a normal host port, the Linux client connects to ws://127.0.0.1:PORT.
         This path is implemented but was not tried on a real Linux machine (see godot/README.md).

Exit code: 0 - nothing failed (warnings allowed), 1 - something must be fixed first.
"""
import argparse
import glob
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_GAME_DIR = os.path.normpath(os.path.join(HERE, '..', '..', 'game', 'Heroes3_HotA1'))
APP_NAME = 'HotA 3D Client'        # Godot project name -> user:// folder of the client
DEFAULT_PORT = 8765
ENV_GAME = 'HOTA_GAME_DIR'
ENV_WINE_PY = 'HOTA3D_WINE_PYTHON'
# the client packages ship a portable Windows Python (python.org embeddable + frida + websockets + Pillow) next to
# bridge/: <package>/python/python.exe. Windows: used when there is no system Python with the modules; Linux: run
# under Wine (wine python.exe bridge.py) in the client's own prefix, see linux_checks().
BUNDLED_PY = os.path.normpath(os.path.join(HERE, '..', 'python', 'python.exe'))
OWN_PREFIX = 'HotA3D/wine'          # under $XDG_DATA_HOME (~/.local/share): the prefix for the bundled Python

# what a usable game folder must contain (HotA 1.8 on top of Heroes 3 Complete / SoD)
REQUIRED = ['h3hota.exe', 'HotA.dll', 'Data/H3bitmap.lod', 'Data/H3sprite.lod', 'Data/HotA.lod']
RECOMMENDED = ['Data/HotA_lng.lod', 'Maps']
# the bridge's addresses were taken from this build (HotA 1.8.0)
KNOWN_SHA256 = {
    'h3hota.exe': 'b5f2f793af0986050fb41df7209c25d861ae0f837af52bb3bd6864ba4de84f41',
    'HotA.dll': '0a1daa1d8f29870b5cb72ebba54a88c43a366473530b908bd23fafc7968223a7',
}
TESTED_VERSION = '1.8.0'
BRIDGE_FILES = ['bridge.py', 'launch.py', 'frames.py', 'snapinfo.py', 'game_agent.js', 'bridge_agent.js',
                'bridge_runtime.js']
MIN_WEBSOCKETS = (13, 0)             # websockets.asyncio.server

# folder names a Heroes 3 / HotA install usually has, relative to a drive root or drive_c
GAME_SUBDIRS = [
    'GOG Games/HoMM 3 Complete', 'GOG Games/Heroes of Might and Magic 3 Complete',
    'Program Files (x86)/GOG Galaxy/Games/HoMM 3 Complete', 'Program Files/GOG Galaxy/Games/HoMM 3 Complete',
    'Program Files (x86)/GOG.com/HoMM 3 Complete', 'Program Files (x86)/3DO/Heroes 3', 'Program Files/3DO/Heroes 3',
    'Program Files (x86)/Heroes of Might and Magic III', 'Program Files (x86)/HotA', 'Games/HoMM 3 Complete',
    'Games/Heroes3', 'Games/Heroes 3', 'Games/HotA', 'Games/Heroes3_HotA', 'Heroes3', 'Heroes 3', 'HotA', 'HoMM3',
]
# parents whose direct subfolders are scanned for h3hota.exe (one level, cheap)
SCAN_PARENTS = ['', 'Games', 'GOG Games', 'Program Files (x86)', 'Program Files', 'Program Files (x86)/GOG Galaxy/Games',
                'Program Files/GOG Galaxy/Games', 'Program Files (x86)/Steam/steamapps/common']
STEAM_ROOTS = ['.steam/steam', '.steam/root', '.local/share/Steam', '.var/app/com.valvesoftware.Steam/.local/share/Steam']
GOG_ID = '1207658787'                # Heroes of Might and Magic 3 Complete on GOG


def check(cid, status, title, detail='', fix=''):
    return {'id': cid, 'status': status, 'title': title, 'detail': detail, 'fix': fix}


class Host:
    """Everything doctor asks the machine: overridable in tests (fake home/env/commands, see test_doctor.py)."""

    def __init__(self, system=None, env=None, home=None):
        self.system = system or platform.system()          # 'Windows' | 'Linux' | 'Darwin'
        self.env = dict(os.environ if env is None else env)
        self.home = home or os.path.expanduser('~')

    def which(self, name):
        return shutil.which(name, path=self.env.get('PATH'))

    def run(self, cmd, timeout=60, env=None):
        """-> (returncode, stdout+stderr text); returncode None = could not run / timed out."""
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env or self.env,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout + r.stderr).decode('utf-8', 'replace')
            return r.returncode, out
        except (OSError, subprocess.SubprocessError) as e:
            return None, f'{type(e).__name__}: {e}'

    def port_free(self, port):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def os_release(self):
        out = {}
        try:
            with open('/etc/os-release', encoding='utf-8') as f:
                for line in f:
                    k, _, v = line.strip().partition('=')
                    out[k] = v.strip('"')
        except OSError:
            pass
        return out

    def registry_paths(self):
        """Windows registry: game folders recorded by the installers / by launch.py."""
        if self.system != 'Windows':
            return []
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\WOW6432Node\\New World Computing\\Heroes of Might and Magic® III\\1.0', 'AppPath'),
                (winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\New World Computing\\Heroes of Might and Magic® III\\1.0', 'AppPath'),
                (winreg.HKEY_CURRENT_USER, 'Software\\Classes\\VirtualStore\\MACHINE\\SOFTWARE\\WOW6432Node\\New World Computing\\Heroes of Might and Magic® III\\1.0', 'AppPath'),
                (winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\WOW6432Node\\GOG.com\\Games\\' + GOG_ID, 'path'),
                (winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\GOG.com\\Games\\' + GOG_ID, 'path')]
        out = []
        for root, path, name in keys:
            try:
                with winreg.OpenKey(root, path) as k:
                    v, _ = winreg.QueryValueEx(k, name)
                    if v:
                        out.append(str(v))
            except OSError:
                pass
        return out

    def registry_writable(self):
        import winreg
        try:
            k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE,
                                   'SOFTWARE\\WOW6432Node\\New World Computing\\Heroes of Might and Magic® III\\1.0',
                                   0, winreg.KEY_SET_VALUE)
            winreg.CloseKey(k)
            return True
        except OSError:
            return False

    def interactive_desktop(self):
        """-> (bool, why): can a game window be seen here? Windows: the process window station is the interactive
        one (WSF_VISIBLE; Windows session 0 / services: no). Linux: an X11 or Wayland display for Wine."""
        if self.system == 'Windows':
            if platform.system() != 'Windows':      # simulated Windows host (tests)
                return True, 'simulated'
            try:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                ws = u.GetProcessWindowStation()

                class Flags(ctypes.Structure):
                    _fields_ = [('fInherit', wintypes.BOOL), ('fReserved', wintypes.BOOL), ('dwFlags', wintypes.DWORD)]
                f, n = Flags(), wintypes.DWORD()
                if not u.GetUserObjectInformationW(ws, 1, ctypes.byref(f), ctypes.sizeof(f), ctypes.byref(n)):
                    return True, 'window station unknown'
                if f.dwFlags & 1:
                    return True, 'interactive desktop'
                return False, 'no interactive desktop (Windows session 0 / service)'
            except Exception as e:
                return True, f'{type(e).__name__}'
        if self.env.get('DISPLAY') or self.env.get('WAYLAND_DISPLAY'):
            return True, 'DISPLAY ' + (self.env.get('DISPLAY') or self.env.get('WAYLAND_DISPLAY'))
        return False, 'no DISPLAY / WAYLAND_DISPLAY'

    def game_running(self):
        if self.system == 'Windows':
            rc, out = self.run(['tasklist', '/FI', 'IMAGENAME eq h3hota.exe', '/NH'], timeout=15)
            return rc == 0 and 'h3hota.exe' in out.lower()
        rc, out = self.run(['pgrep', '-af', 'h3hota'], timeout=5)
        return rc == 0 and 'h3hota.exe' in out.lower()


# ------------------------------------------------------------------------------------------------ game folder

def missing_files(d):
    """Required files missing in d (case-insensitive, as on Windows / under Wine). [] = valid."""
    if not d or not os.path.isdir(d):
        return list(REQUIRED)
    return [r for r in REQUIRED if not _ci_exists(d, r)]


def _ci_exists(base, rel):
    cur = base
    for part in rel.split('/'):
        p = os.path.join(cur, part)
        if os.path.exists(p):
            cur = p
            continue
        try:
            low = {n.lower(): n for n in os.listdir(cur)}
        except OSError:
            return False
        if part.lower() not in low:
            return False
        cur = os.path.join(cur, low[part.lower()])
    return True


def saved_game_dir(host):
    """The folder the client remembered (user://local.cfg, section [game], key dir)."""
    if host.system == 'Windows':
        base = host.env.get('APPDATA') or os.path.join(host.home, 'AppData', 'Roaming')
        cfg = os.path.join(base, 'Godot', 'app_userdata', APP_NAME, 'local.cfg')
    else:
        base = host.env.get('XDG_DATA_HOME') or os.path.join(host.home, '.local', 'share')
        cfg = os.path.join(base, 'godot', 'app_userdata', APP_NAME, 'local.cfg')
    try:
        with open(cfg, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r'^\[game\][^\[]*?^dir="((?:[^"\\]|\\.)*)"', text, re.M | re.S)
    return m.group(1).replace('\\"', '"').replace('\\\\', '\\') if m else None


def wine_prefixes(host):
    """Existing Wine prefixes (folders with drive_c): WINEPREFIX, ~/.wine, wineprefixes, Lutris, Bottles, Proton."""
    h = host.home
    pats = [host.env.get('WINEPREFIX') or '', os.path.join(h, '.wine'),
            os.path.join(h, '.local/share/wineprefixes/*'), os.path.join(h, 'Games/*'),
            os.path.join(h, '.local/share/lutris/prefixes/*'),
            os.path.join(h, '.local/share/bottles/bottles/*'),
            os.path.join(h, '.var/app/com.usebottles.bottles/data/bottles/bottles/*'),
            os.path.join(h, '.var/app/net.lutris.Lutris/data/lutris/prefixes/*'),
            os.path.join(h, '.cxoffice/*')]
    pats += [os.path.join(h, s, 'steamapps/compatdata/*/pfx') for s in STEAM_ROOTS]
    out = []
    for p in pats:
        if not p:
            continue
        for d in sorted(glob.glob(p)) if '*' in p else [p]:
            d = os.path.realpath(d)
            if os.path.isdir(os.path.join(d, 'drive_c')) and d not in out:
                out.append(d)
    return out


def prefix_of(path):
    """Linux path inside a Wine prefix (.../drive_c/...) -> the prefix, else None."""
    parts = os.path.normpath(path).split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower() == 'drive_c':
            return os.sep.join(parts[:i]) or os.sep
    return None


def _fs_root():
    return os.path.abspath(os.sep)          # '/' on Linux (what Wine's Z: points to by default)


def to_wine_path(path, prefix):
    """Linux path -> Windows path as programs in `prefix` see it: drive_c -> C:, other dosdevices drives, Z: = /."""
    drives = {}
    dd = os.path.join(prefix, 'dosdevices')
    try:
        for n in os.listdir(dd):
            if len(n) == 2 and n[1] == ':':
                drives[n[0].upper()] = os.path.realpath(os.path.join(dd, n))
    except OSError:
        pass
    drives.setdefault('C', os.path.realpath(os.path.join(prefix, 'drive_c')))
    drives.setdefault('Z', _fs_root())
    rp = os.path.realpath(os.path.abspath(path))
    best = None
    for letter, target in drives.items():
        try:
            rel = os.path.relpath(rp, target)
        except ValueError:                  # other drive (Windows test host)
            continue
        if rel == os.curdir:
            rel = ''
        if rel.startswith(os.pardir):
            continue
        if best is None or len(target) > len(best[1]):
            best = (letter, target, rel)
    if best is None:
        return None
    return best[0] + ':\\' + best[2].replace(os.sep, '\\')


def from_wine_path(wpath, prefix):
    """Windows path in a prefix (C:\\GOG Games\\...) -> Linux path."""
    m = re.match(r'^([A-Za-z]):[\\/]?(.*)$', wpath.strip())
    if not m:
        return None
    letter, rest = m.group(1).lower(), m.group(2).replace('\\', '/').strip('/')
    base = os.path.join(prefix, 'dosdevices', letter + ':')
    if not os.path.exists(base):
        base = os.path.join(prefix, 'drive_c') if letter == 'c' else (_fs_root() if letter == 'z' else None)
    if base is None:
        return None
    return os.path.normpath(os.path.join(base, *rest.split('/'))) if rest else os.path.normpath(base)


def wine_registry_paths(prefix):
    """AppPath / GOG path values of Heroes 3 in the prefix's text registry (system.reg)."""
    out = []
    try:
        with open(os.path.join(prefix, 'system.reg'), encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return out
    for sec in re.split(r'\n(?=\[)', text):
        head = sec.split('\n', 1)[0]
        if 'New World Computing' in head or ('GOG.com' in head and GOG_ID in head):
            for m in re.finditer(r'^"(?:AppPath|path)"="((?:[^"\\]|\\.)*)"', sec, re.M | re.I):
                lp = from_wine_path(m.group(1).replace('\\\\', '\\'), prefix)
                if lp:
                    out.append(lp)
    return out


def _scan(base, out):
    for sub in GAME_SUBDIRS:
        out.append(os.path.join(base, *sub.split('/')))
    for par in SCAN_PARENTS:
        p = os.path.join(base, *par.split('/')) if par else base
        try:
            names = sorted(os.listdir(p))
        except OSError:
            continue
        for n in names:
            c = os.path.join(p, n)
            if os.path.isdir(c) and _ci_exists(c, 'h3hota.exe'):
                out.append(c)


def candidate_dirs(host, explicit=None):
    """[(path, source)] in priority order: --game-dir, saved, env, repo copy, registry, common locations."""
    cands = []
    if explicit:
        cands.append((explicit, 'указана'))
    s = saved_game_dir(host)
    if s:
        cands.append((s, 'сохранена клиентом'))
    if host.env.get(ENV_GAME):
        cands.append((host.env[ENV_GAME], ENV_GAME))
    cands.append((REPO_GAME_DIR, 'папка репозитория'))
    scanned = []
    if host.system == 'Windows':
        for p in host.registry_paths():
            cands.append((p, 'реестр'))
        for letter in 'CDEFGH':
            root = letter + ':\\'
            if os.path.isdir(root):
                _scan(root, scanned)
        for s in (os.path.join(host.env.get('ProgramFiles(x86)', 'C:\\Program Files (x86)'), 'Steam', 'steamapps', 'common'),):
            _scan_common(s, scanned)
    else:
        for pfx in wine_prefixes(host):
            for p in wine_registry_paths(pfx):
                cands.append((p, 'реестр Wine ' + pfx))
            _scan(os.path.join(pfx, 'drive_c'), scanned)
        for s in STEAM_ROOTS:
            _scan_common(os.path.join(host.home, s, 'steamapps', 'common'), scanned)
        _scan(os.path.join(host.home, 'Games'), scanned)
        _scan(host.home, scanned)
    cands += [(p, 'поиск') for p in scanned]
    out, seen = [], set()
    for p, src in cands:
        key = os.path.normcase(os.path.realpath(p)) if p else ''
        if p and key not in seen:
            seen.add(key)
            out.append((os.path.normpath(p), src))
    return out


def _scan_common(common, out):
    try:
        for n in sorted(os.listdir(common)):
            c = os.path.join(common, n)
            if _ci_exists(c, 'h3hota.exe'):
                out.append(c)
    except OSError:
        pass


def find_game_dir(host, explicit=None):
    """-> (valid_dir | None, source, partial) where partial = [(dir, missing)] of folders with h3hota.exe that
    lack other files. An explicit folder is never replaced by a found one (the user chose it)."""
    partial = []
    for p, src in candidate_dirs(host, explicit):
        miss = missing_files(p)
        if not miss:
            return p, src, partial
        if explicit and p == os.path.normpath(explicit):
            return None, src, [(p, miss)]
        if os.path.isdir(p) and 'h3hota.exe' not in miss:
            partial.append((p, miss))
    return None, '', partial


def game_version(d):
    try:
        with open(os.path.join(d, 'HotA_Setup.ini'), encoding='latin-1') as f:
            m = re.search(r'^Main Version=(\S+)', f.read(), re.M)
            return m.group(1) if m else None
    except OSError:
        return None


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ checks

def game_checks(host, explicit):
    d, src, partial = find_game_dir(host, explicit)
    checks = []
    if d is None:
        if partial:
            p, miss = partial[0]
            checks.append(check('game_dir', 'fail', 'Папка игры: не хватает файлов',
                                f'{p}: нет {", ".join(miss)}',
                                'Укажите папку, где лежат h3hota.exe, HotA.dll и Data\\*.lod (Heroes 3 Complete + HotA 1.8): '
                                'кнопка «Папка игры…» в клиенте или --game-dir'))
        else:
            checks.append(check('game_dir', 'fail', 'Папка игры не найдена',
                                'искал: ' + ('указанную папку' if explicit else
                                             'сохранённую, %s, реестр и обычные места установки' % ENV_GAME),
                                'Укажите папку HotA (с h3hota.exe): кнопка «Папка игры…» в клиенте, '
                                '--game-dir ПАПКА или переменная %s' % ENV_GAME))
        return None, checks
    checks.append(check('game_dir', 'ok', 'Папка игры', f'{d} ({src})'))
    ver = game_version(d)
    hashes = {}
    for n in KNOWN_SHA256:
        try:
            hashes[n] = sha256(os.path.join(d, n))
        except OSError:
            hashes[n] = None
    same = all(hashes[n] == KNOWN_SHA256[n] for n in KNOWN_SHA256)
    if same:
        checks.append(check('game_version', 'ok', 'Версия игры', f'HotA {ver or TESTED_VERSION}, h3hota.exe и HotA.dll — '
                                                                   'те, что проверены с мостом'))
    else:
        diff = [n for n in KNOWN_SHA256 if hashes[n] != KNOWN_SHA256[n]]
        checks.append(check('game_version', 'warn', 'Версия игры отличается от проверенной',
                            f'HotA {ver or "?"}; другие файлы: {", ".join(diff)} (мост сделан для HotA {TESTED_VERSION})',
                            f'Если мост не стартует или падает — поставьте HotA {TESTED_VERSION} (адреса движка зависят '
                            'от сборки h3hota.exe)'))
    rec = [r for r in RECOMMENDED if not _ci_exists(d, r)]
    if rec:
        checks.append(check('game_extra', 'warn', 'Необязательные файлы игры', 'нет: ' + ', '.join(rec), ''))
    games = os.path.join(d, 'games')
    if not os.access(d, os.W_OK):
        checks.append(check('game_writable', 'warn', 'Папка игры только для чтения', d,
                            'Мост кладёт тестовое сохранение в games\\; дайте права на запись или скопируйте игру'))
    elif not os.path.isdir(games):
        checks.append(check('game_writable', 'info', 'Папки games\\ нет', 'мост создаст её', ''))
    return d, checks


def bridge_checks():
    miss = [f for f in BRIDGE_FILES if not os.path.isfile(os.path.join(HERE, f))]
    if miss:
        return [check('bridge_files', 'fail', 'Файлы моста', f'{HERE}: нет {", ".join(miss)}',
                      'Распакуйте пакет клиента целиком (папка bridge\\ рядом с HotA3D)')]
    return [check('bridge_files', 'ok', 'Файлы моста', HERE)]


def port_check(host, port):
    if host.port_free(port):
        return port, check('port', 'ok', f'Порт {port}', 'свободен')
    for p in range(port + 1, port + 50):
        if host.port_free(p):
            return p, check('port', 'warn', f'Порт {port} занят', f'мост будет на {p}',
                            f'Занят другим процессом (старый мост? SSH-туннель?); закройте его или оставьте {p}')
    return None, check('port', 'fail', f'Порт {port} занят', 'и следующие 50 тоже', 'Освободите порт или задайте --port')


def _pip_hint(py_cmd):
    return f'{py_cmd} -m pip install "frida>=17" "websockets>=13"'


def python_import_probe():
    """Code run in the bridge's interpreter: versions of python / frida / websockets as JSON."""
    return ('import json,sys,struct\n'
            'r={"python":sys.version.split()[0],"bits":struct.calcsize("P")*8,"exe":sys.executable}\n'
            'for m in ("frida","websockets"):\n'
            '    try:\n'
            '        r[m]=__import__(m).__version__\n'
            '    except Exception as e:\n'
            '        r[m+"_error"]="%s: %s"%(type(e).__name__,e)\n'
            'print("PROBE"+json.dumps(r))\n')


def parse_probe(out):
    m = re.search(r'PROBE(\{.*\})', out or '')
    return json.loads(m.group(1)) if m else None


def _ver(s):
    return tuple(int(x) for x in re.findall(r'\d+', s or '0')[:2]) or (0,)


def module_checks(probe, py_cmd, where):
    """frida / websockets from a probe dict."""
    out = []
    if probe.get('frida'):
        out.append(check('frida', 'ok', 'frida', f'{probe["frida"]} ({where})'))
    else:
        out.append(check('frida', 'fail', f'frida не установлен ({where})', probe.get('frida_error', ''),
                         _pip_hint(py_cmd)))
    wv = probe.get('websockets')
    if wv and _ver(wv) >= MIN_WEBSOCKETS:
        out.append(check('websockets', 'ok', 'websockets', f'{wv} ({where})'))
    elif wv:
        out.append(check('websockets', 'fail', f'websockets {wv} слишком старый (нужен ≥ 13)', where,
                         f'{py_cmd} -m pip install -U "websockets>=13"'))
    else:
        out.append(check('websockets', 'fail', f'websockets не установлен ({where})', probe.get('websockets_error', ''),
                         _pip_hint(py_cmd)))
    return out


def _has_modules():
    try:
        import frida, websockets  # noqa: F401
        return True
    except Exception:
        return False


def windows_checks(host, python_exe):
    checks = []
    py = python_exe or sys.executable
    q = f'"{py}"'
    if python_exe and os.path.normcase(python_exe) != os.path.normcase(sys.executable):
        rc, out = host.run([python_exe, '-c', python_import_probe()], timeout=60)
        probe = parse_probe(out)
        if probe is None:
            checks.append(check('python', 'fail', 'Python для моста не запускается', f'{python_exe}: {out.strip()[:200]}',
                                'Поставьте Python 3.10+ (64-бит) с python.org'))
            return checks
    else:
        probe = {'python': sys.version.split()[0], 'bits': 64 if sys.maxsize > 2 ** 32 else 32, 'exe': sys.executable}
        for m in ('frida', 'websockets'):
            try:
                probe[m] = __import__(m).__version__
            except Exception as e:
                probe[m + '_error'] = f'{type(e).__name__}: {e}'
    pv = _ver(probe['python'])
    if pv < (3, 8):
        checks.append(check('python', 'fail', f'Python {probe["python"]} слишком старый', probe['exe'],
                            'Поставьте Python 3.10+ (64-бит) с python.org'))
    else:
        checks.append(check('python', 'ok', 'Python', f'{probe["python"]} {probe["bits"]}-бит: {probe["exe"]}'))
    checks += module_checks(probe, q, probe['exe'])
    if not host.registry_writable():
        checks.append(check('registry', 'warn', 'Нет прав на HKLM (не администратор)',
                            'настройки окна/звука игры уйдут в HKCU VirtualStore (виртуализация реестра для h3hota.exe)',
                            'Если игра открывается в полный экран или виснет — запустите клиент от администратора'))
    else:
        checks.append(check('registry', 'ok', 'Реестр', 'настройки игры можно записать (после выхода вернутся)'))
    return checks


def find_wine_python(host, prefix):
    """Windows python.exe inside the prefix (highest version), or HOTA3D_WINE_PYTHON."""
    if host.env.get(ENV_WINE_PY):
        return host.env[ENV_WINE_PY]
    c = os.path.join(prefix, 'drive_c')
    pats = ['users/*/AppData/Local/Programs/Python/Python3*/python.exe',
            'users/*/Local Settings/Application Data/Programs/Python/Python3*/python.exe',
            'Program Files/Python3*/python.exe', 'Program Files (x86)/Python3*/python.exe', 'Python3*/python.exe']
    found = []
    for p in pats:
        found += glob.glob(os.path.join(c, p))
    found.sort(key=lambda p: _ver(re.sub(r'.*Python(3)(\d+).*', r'\1.\2', p)), reverse=True)
    return found[0] if found else None


def own_prefix(host):
    base = host.env.get('XDG_DATA_HOME') or os.path.join(host.home, '.local', 'share')
    return os.path.join(base, *OWN_PREFIX.split('/'))


def prefix_arch(prefix):
    try:
        with open(os.path.join(prefix, 'system.reg'), encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'#arch=(\w+)', line)
                if m:
                    return m.group(1)
                if not line.startswith(('#', 'WINE', ';')) and line.strip():
                    break
    except OSError:
        return None
    return None


def linux_install_hint(host, what):
    osr = host.os_release()
    ids = (osr.get('ID', '') + ' ' + osr.get('ID_LIKE', '')).lower()
    if what == 'wine':
        if 'debian' in ids or 'ubuntu' in ids:
            return 'sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install wine wine32 wine64'
        if 'fedora' in ids or 'rhel' in ids:
            return 'sudo dnf install wine'
        if 'arch' in ids:
            return 'включите [multilib] в /etc/pacman.conf, затем sudo pacman -S wine'
        if 'suse' in ids:
            return 'sudo zypper install wine'
        return 'установите Wine 8+ с поддержкой 32-битных программ (пакеты wine + wine32)'
    if what == 'wine32':
        if 'debian' in ids or 'ubuntu' in ids:
            return 'sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install wine32'
        if 'arch' in ids:
            return 'sudo pacman -S lib32-gnutls lib32-alsa-plugins (multilib) — или Wine 9+ в режиме WoW64'
        return 'поставьте 32-битную часть Wine (wine32 / i386) или Wine 9+ в режиме WoW64'
    if what == 'python3':
        if 'debian' in ids or 'ubuntu' in ids:
            return 'sudo apt install python3'
        if 'fedora' in ids:
            return 'sudo dnf install python3'
        if 'arch' in ids:
            return 'sudo pacman -S python'
        return 'установите python3'
    return ''


WINE_PY_INSTALL = ('скачайте python-3.12.10-amd64.exe с python.org и выполните: '
                   'WINEPREFIX="{p}" wine python-3.12.10-amd64.exe /quiet InstallAllUsers=0 PrependPath=0 Include_test=0')


def linux_checks(host, game_dir, run_wine=True):
    """-> (checks, ctx) ctx: wine, prefix, wine_python, wine_env for the launch command."""
    checks, ctx = [], {}
    wine = host.env.get('WINE') or host.which('wine')
    if not wine:
        checks.append(check('wine', 'fail', 'Wine не найден в PATH', '', linux_install_hint(host, 'wine')))
        return checks, ctx
    ctx['wine'] = wine
    rc, out = host.run([wine, '--version'], timeout=30) if run_wine else (0, 'wine-?')
    ver = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
    if not ver:
        checks.append(check('wine', 'fail', 'Wine не запускается', f'{wine}: {(out or "").strip()[:200]}',
                            linux_install_hint(host, 'wine')))
        return checks, ctx
    wv = _ver(ver)
    checks.append(check('wine', 'ok' if wv >= (7,) else 'warn', 'Wine', f'{ver} ({wine})',
                        '' if wv >= (7,) else 'Wine старше 7: обновите до 8+ (WineHQ stable)'))
    # Python: HOTA3D_WINE_PYTHON (user's choice) > the bundled one (package/python) > one installed in the prefix.
    # Prefix: WINEPREFIX if set; with the bundled Python - the client's own prefix (~/.local/share/HotA3D/wine,
    # created here on first run), even when the game lies inside another prefix: the game folder is portable (HotA
    # needs no installer registry, the bridge sets its window/sound settings itself) and is reached through Z:, the
    # user's prefix is not touched and its Wine/Proton version does not matter. Without a bundled Python the old
    # rule: the prefix the game lives in > ~/.wine (Python + frida must be installed there).
    gp = prefix_of(game_dir) if game_dir else None
    envp = host.env.get('WINEPREFIX')
    user_py = host.env.get(ENV_WINE_PY)
    bundled = BUNDLED_PY if os.path.isfile(BUNDLED_PY) else None
    own = None
    if envp:
        prefix, src = envp, 'WINEPREFIX'
    elif bundled and not user_py:
        own = prefix = own_prefix(host)
        src = 'свой префикс клиента'
    elif gp:
        prefix, src = gp, 'префикс игры'
    else:
        prefix, src = os.path.join(host.home, '.wine'), '~/.wine'
    ctx['prefix'] = prefix
    if envp and gp and os.path.realpath(envp) != os.path.realpath(gp) and not bundled:
        checks.append(check('wineprefix', 'warn', 'WINEPREFIX не тот, где лежит игра',
                            f'WINEPREFIX={envp}, игра в {gp}',
                            f'Обычно нужен префикс игры: export WINEPREFIX="{gp}" (Python и frida — тоже в нём)'))
    if own and run_wine and not os.path.isfile(os.path.join(prefix, 'system.reg')):
        os.makedirs(prefix, exist_ok=True)
        # first run: create it (win64 prefix runs 32-bit programs through WoW64); no Mono/Gecko install dialogs
        env0 = dict(host.env, WINEPREFIX=prefix, WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree=d;mshtml=d')
        rc, out = host.run([wine, 'wineboot', '-i'], timeout=600, env=env0)
        if not os.path.isfile(os.path.join(prefix, 'system.reg')):
            checks.append(check('wineprefix', 'fail', 'Не удалось создать префикс Wine', f'{prefix}: {(out or "").strip()[-200:]}',
                                f'WINEPREFIX="{prefix}" wineboot -u  — и посмотрите, что пишет Wine'))
            return checks, ctx
        checks.append(check('wineprefix_new', 'info', 'Создан префикс Wine', prefix))
    if not os.path.isfile(os.path.join(prefix, 'system.reg')):
        checks.append(check('wineprefix', 'fail', 'Префикс Wine не создан', prefix,
                            f'WINEPREFIX="{prefix}" wineboot -u   (или export WINEPREFIX=<ваш префикс>)'))
        return checks, ctx
    arch = prefix_arch(prefix) or '?'
    checks.append(check('wineprefix', 'ok', 'Префикс Wine', f'{prefix} ({src}, {arch})'))
    if own and gp:
        checks.append(check('wineprefix_game', 'info', 'Игра лежит в другом префиксе',
                            f'{gp}: игра запускается из него как есть (через Z:), но в префиксе клиента',
                            ''))
    if 'compatdata' in prefix:
        checks.append(check('proton', 'warn', 'Префикс Steam/Proton',
                            'системный wine может быть другой версии, чем Proton этого префикса',
                            'Если не стартует — поставьте игру в отдельный префикс обычного Wine'))
    env = dict(host.env, WINEPREFIX=prefix, WINEDEBUG=host.env.get('WINEDEBUG', '-all'))
    ctx['env'] = {'WINEPREFIX': prefix, 'WINEDEBUG': env['WINEDEBUG']}
    # 32-bit: h3hota.exe is a 32-bit program
    if run_wine:
        cmd32 = 'C:\\windows\\syswow64\\cmd.exe' if arch == 'win64' else 'C:\\windows\\system32\\cmd.exe'
        rc, out = host.run([wine, cmd32, '/c', 'echo', 'hota32ok'], timeout=120, env=env)
        if rc == 0 and 'hota32ok' in out:
            checks.append(check('wine32', 'ok', '32-битные программы в Wine', 'запускаются'))
        else:
            checks.append(check('wine32', 'fail', 'Wine не запускает 32-битные программы (h3hota.exe — 32-бит)',
                                (out or '').strip()[-200:], linux_install_hint(host, 'wine32')))
    if not (host.env.get('DISPLAY') or host.env.get('WAYLAND_DISPLAY')):
        checks.append(check('display', 'warn', 'Нет DISPLAY / WAYLAND_DISPLAY',
                            'игре под Wine нужен графический сеанс (окно игры 800×600)',
                            'Запускайте из графического сеанса; без экрана — xvfb-run'))
    # Windows Python + frida + websockets in the same prefix
    wpy = user_py or bundled or find_wine_python(host, prefix)
    wsrc = ENV_WINE_PY if user_py else ('из пакета клиента' if wpy == bundled else 'в префиксе')
    if not wpy:
        checks.append(check('wine_python', 'fail', 'Нет Windows-Python в префиксе Wine',
                            f'искал в {prefix}/drive_c (users/*/AppData/Local/Programs/Python, Program Files, C:\\Python3*)',
                            WINE_PY_INSTALL.format(p=prefix) + f'; или укажите путь: export {ENV_WINE_PY}=/путь/python.exe'))
        return checks, ctx
    ctx['wine_python'] = wpy
    qpy = f'WINEPREFIX="{prefix}" wine "{wpy}"'
    if not run_wine:
        checks.append(check('wine_python', 'ok', 'Windows-Python в Wine', wpy))
        return checks, ctx
    rc, out = host.run([wine, wpy, '-c', python_import_probe()], timeout=180, env=env)
    probe = parse_probe(out)
    if probe is None:
        checks.append(check('wine_python', 'fail', 'Windows-Python под Wine не запускается',
                            f'{wpy}: {(out or "").strip()[-200:]}', WINE_PY_INSTALL.format(p=prefix)))
        return checks, ctx
    checks.append(check('wine_python', 'ok' if _ver(probe['python']) >= (3, 8) else 'fail',
                        'Windows-Python в Wine', f'{probe["python"]} {probe["bits"]}-бит, {wsrc}: {wpy}',
                        '' if _ver(probe['python']) >= (3, 8) else WINE_PY_INSTALL.format(p=prefix)))
    checks += module_checks(probe, qpy, 'Python в Wine')
    return checks, ctx


def resolve_visible(host, visible='auto'):
    """--visible on|off|auto -> (bool, why). auto: visible when the machine has a desktop the player sees."""
    v = str(visible or 'auto').lower()
    if v in ('on', '1', 'yes', 'true'):
        return True, 'on'
    if v in ('off', '0', 'no', 'false'):
        return False, 'off'
    ok, why = host.interactive_desktop()
    return ok, 'auto: ' + why


def diagnose(host=None, game_dir=None, port=DEFAULT_PORT, python_exe=None, control_dir=None, run_wine=True,
             load=None, visible='auto'):
    """Full checklist + how to launch. -> {'os', 'game_dir', 'ok', 'checks': [...], 'launch': {...} | None,
    'visible': bool}"""
    host = host or Host()
    rel = ''
    if host.system == 'Linux':
        osr = host.os_release()
        rel = osr.get('PRETTY_NAME', '')
    elif host.system == 'Windows':
        rel = platform.platform()
    checks = [check('os', 'ok' if host.system in ('Windows', 'Linux') else 'fail', 'ОС',
                    f'{host.system} {rel}'.strip(),
                    '' if host.system in ('Windows', 'Linux') else 'Поддерживаются Windows и Linux (Wine)')]
    gd, gc = game_checks(host, game_dir)
    checks += gc
    checks += bridge_checks()
    ctx = {}
    if host.system == 'Windows' and not python_exe and os.path.isfile(BUNDLED_PY) and not _has_modules():
        python_exe = BUNDLED_PY        # this interpreter lacks frida/websockets: the package's own Python has them
    if host.system == 'Windows':
        checks += windows_checks(host, python_exe)
    elif host.system == 'Linux':
        lc, ctx = linux_checks(host, gd, run_wine=run_wine)
        checks += lc
    use_port, pc = port_check(host, port)
    checks.append(pc)
    if host.game_running():
        checks.append(check('game_running', 'warn', 'h3hota.exe уже запущен',
                            'мост запускает свою копию игры; две сразу мешают друг другу',
                            'Закройте игру (Windows: taskkill /F /IM h3hota.exe; Linux: wineserver -k в её префиксе)'))
    vis, vwhy = resolve_visible(host, visible)
    checks.append(check('game_window', 'info', 'Окно оригинальной игры',
                        ('на экране: играть можно и в нём, и в клиенте' if vis else 'скрыто') + f' ({vwhy})'))
    ok = not any(c['status'] == 'fail' for c in checks)
    launch = None
    if ok and gd and use_port:
        launch = launch_command(host, gd, use_port, python_exe, control_dir, ctx, load, vis)
        if launch is None:
            checks.append(check('wine_path', 'fail', 'Путь не виден из Wine', gd,
                                'Положите игру и клиент внутрь префикса или на диск Z: (по умолчанию Z: = /)'))
            ok = False
    return {'os': host.system, 'game_dir': gd, 'port': use_port, 'ok': ok, 'checks': checks, 'launch': launch,
            'visible': vis}


def launch_command(host, game_dir, port, python_exe, control_dir, ctx, load=None, visible=False):
    """argv + extra env to start the bridge in local mode (127.0.0.1 only)."""
    bridge = os.path.join(HERE, 'bridge.py')
    tail = ['--port', str(port)]
    # a given save is loaded right away (tests, --load); otherwise the game starts at its main menu and the player
    # chooses the mode there (PLAN 4.5)
    tail += ['--load', load] if load else ['--menu']
    if visible:
        tail.append('--visible')       # the game window on the desktop as well (PLAN 4.6)
    if host.system == 'Windows':
        args = [python_exe or sys.executable, '-I', bridge, '--game-dir', game_dir] + tail
        if control_dir:
            args += ['--control-dir', control_dir, '--log', os.path.join(control_dir, 'bridge.log')]
        return {'cmd': args, 'env': {}, 'url': f'ws://127.0.0.1:{port}', 'wine': False}
    prefix = ctx['prefix']
    conv = [to_wine_path(p, prefix) for p in (bridge, game_dir) + ((control_dir,) if control_dir else ())]
    if None in conv:
        return None
    args = [ctx['wine'], ctx['wine_python'], '-I', conv[0], '--game-dir', conv[1]] + tail
    if control_dir:
        args += ['--control-dir', conv[2], '--log', conv[2] + '\\bridge.log']
    return {'cmd': args, 'env': ctx.get('env', {}), 'url': f'ws://127.0.0.1:{port}', 'wine': True,
            'kill': [ctx['wine'], 'taskkill', '/F', '/PID']}


MARK = {'ok': '[ OK ]', 'warn': '[WARN]', 'fail': '[FAIL]', 'info': '[INFO]'}


def print_report(r):
    for c in r['checks']:
        line = f'{MARK[c["status"]]} {c["title"]}'
        if c['detail']:
            line += f': {c["detail"]}'
        print(line)
        if c['fix'] and c['status'] in ('fail', 'warn'):
            print(f'        -> {c["fix"]}')
    print()
    if r['ok']:
        print('Всё нужное есть. Запуск моста:')
        if r['launch']:
            env = ' '.join(f'{k}="{v}"' for k, v in r['launch']['env'].items())
            print('  ' + (env + ' ' if env else '') + ' '.join(f'"{a}"' if ' ' in a else a for a in r['launch']['cmd']))
            print(f'  клиент: HotA3D --url {r["launch"]["url"]}  (или просто HotA3D: локальный режим сам запустит мост)')
    else:
        print('Исправьте пункты [FAIL] и запустите проверку снова.')


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--game-dir', help='check this folder instead of searching')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--python', help='Windows: the interpreter the bridge will run under (default: this one)')
    ap.add_argument('--control-dir', help='client: control folder for the launch command')
    ap.add_argument('--load', help='save to load (bridge.py --load); default: the game starts at its main menu (--menu)')
    ap.add_argument('--json', action='store_true', help='JSON result (ASCII) to stdout or --out')
    ap.add_argument('--out', help='with --json: write here instead of stdout')
    ap.add_argument('--find', action='store_true', help='only list game folder candidates')
    ap.add_argument('--no-wine-run', action='store_true', help='Linux: do not start wine (quick check)')
    ap.add_argument('--visible', default='auto', choices=('auto', 'on', 'off'),
                    help='the original game window on the desktop (bridge.py --visible); auto: when there is a '
                         'desktop (default)')
    a = ap.parse_args()
    host = Host()
    if a.find:
        for p, src in candidate_dirs(host, a.game_dir):
            miss = missing_files(p)
            if os.path.isdir(p):
                print(('[ OK ] ' if not miss else '[ -- ] ') + p + f'  ({src})' + (f'  нет: {", ".join(miss)}' if miss else ''))
        return 0
    r = diagnose(host, a.game_dir, a.port, a.python, a.control_dir, run_wine=not a.no_wine_run, load=a.load,
                 visible=a.visible)
    if a.json:
        text = json.dumps(r, ensure_ascii=True, indent=1)
        if a.out:
            with open(a.out, 'w', encoding='ascii') as f:
                f.write(text)
        else:
            print(text)
    else:
        print_report(r)
    return 0 if r['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
