"""Portable Windows Python for the bridge (bundled into the client packages, also used under Wine on Linux).

    python -I godot/tools/make_bridge_python.py OUT_DIR [--src _dl/bridge-python]

OUT_DIR/ = the official python.org embeddable zip (python-3.12.10-embed-amd64.zip) + frida, websockets, Pillow
unpacked from their win_amd64 wheels into OUT_DIR/Lib/site-packages, and python312._pth:
    python312.zip / . / Lib\\site-packages / ..\\bridge
(..\\bridge: the embeddable runs in ._pth mode, where the script's own folder is NOT put on sys.path, so the
package's bridge/ folder is listed explicitly; the bridge's sibling imports - launch, frames, snapinfo - need it).
Downloads (made once, see godot/README.md):
    curl -O https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip
    python -m pip download --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all:
        -d wheels "frida==17.23.3" "websockets==13.1" pillow
Standard library only.
"""
import argparse
import glob
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PRJ = os.path.normpath(os.path.join(HERE, '..', '..'))
EMBED = 'python-3.12.10-embed-amd64.zip'
PTH = 'python312._pth'
NEED = ('frida', 'websockets', 'PIL')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out')
    ap.add_argument('--src', default=os.path.join(PRJ, '_dl', 'bridge-python'))
    a = ap.parse_args()
    emb = os.path.join(a.src, EMBED)
    wheels = sorted(glob.glob(os.path.join(a.src, 'wheels', '*.whl')))
    if not os.path.isfile(emb) or not wheels:
        sys.exit(f'missing {emb} or {a.src}/wheels/*.whl (see --help)')
    if os.path.exists(a.out):
        shutil.rmtree(a.out)
    os.makedirs(a.out)
    with zipfile.ZipFile(emb) as z:
        z.extractall(a.out)
    site = os.path.join(a.out, 'Lib', 'site-packages')
    os.makedirs(site)
    for w in wheels:
        with zipfile.ZipFile(w) as z:
            for n in z.namelist():
                if n.endswith('/') or '..' in n.split('/'):
                    continue
                top = n.split('/')[0]
                if top.endswith('.data'):          # purelib/platlib payload of a wheel -> site-packages
                    parts = n.split('/')
                    if len(parts) < 3 or parts[1] not in ('purelib', 'platlib'):
                        continue
                    n2 = '/'.join(parts[2:])
                else:
                    n2 = n
                dst = os.path.join(site, *n2.split('/'))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(n) as s, open(dst, 'wb') as d:
                    shutil.copyfileobj(s, d)
    with open(os.path.join(a.out, PTH), 'w', encoding='ascii', newline='\r\n') as f:
        f.write('python312.zip\n.\nLib\\site-packages\n..\\bridge\n# no "import site": nothing outside these paths\n')
    miss = [m for m in NEED if not os.path.isdir(os.path.join(site, m))]
    if miss:
        sys.exit(f'not unpacked: {miss}')
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(a.out) for f in fs)
    print(f'   python/: {EMBED} + {", ".join(os.path.basename(w).split("-")[0] + " " + os.path.basename(w).split("-")[1] for w in wheels)}'
          f' ({size / 1e6:.0f} MB)')


if __name__ == '__main__':
    main()
