"""Scan RoE/AB/SoD .h3m maps: size, players, terrain mix, object variety (to pick a test map).

Usage: python -I h3m_scan.py <Maps dir> [--sort snow|defs] [--max-size 72]
HotA-format maps (version 0x20) are listed with header fields only.
"""
import argparse
import glob
import gzip
import os
import struct

TERRAINS = ['dirt', 'sand', 'grass', 'snow', 'swamp', 'rough', 'subterr', 'lava', 'water', 'rock']


class R:
    def __init__(self, data):
        self.d, self.p = data, 0

    def u8(self):
        self.p += 1
        return self.d[self.p - 1]

    def u16(self):
        self.p += 2
        return struct.unpack_from('<H', self.d, self.p - 2)[0]

    def u32(self):
        self.p += 4
        return struct.unpack_from('<I', self.d, self.p - 4)[0]

    def s(self):
        n = self.u32()
        self.p += n
        return self.d[self.p - n:self.p].decode('cp1251', 'replace')

    def skip(self, n):
        self.p += n


def parse(path):
    try:
        data = gzip.open(path).read()
    except OSError:
        data = open(path, 'rb').read()
    r = R(data)
    ver = r.u32()
    m = {'file': os.path.basename(path), 'ver': ver}
    if ver not in (0x0E, 0x15, 0x1C):
        if ver == 0x20:  # HotA: hotaVersion u32, then version-dependent extras before the common header
            hv = r.u32()
            m['hota'] = hv
        return m
    roe, ab, sod = ver == 0x0E, ver == 0x15, ver == 0x1C
    r.u8()
    m['size'] = r.u32()
    m['levels'] = 2 if r.u8() else 1
    m['name'] = r.s()
    m['desc'] = r.s()
    m['difficulty'] = r.u8()
    if not roe:
        r.u8()  # level limit
    players = []
    for i in range(8):
        human, ai = r.u8(), r.u8()
        if not (human or ai):
            r.skip(13 if sod else 12 if ab else 6)
            continue
        r.u8()  # ai tactic
        if sod:
            r.u8()
        r.u8()
        if not roe:
            r.u8()
        r.u8()  # faction random
        main_town = r.u8()
        town = None
        if main_town:
            if not roe:
                r.u8(); r.u8()
            town = (r.u8(), r.u8(), r.u8())
        r.u8()  # random hero
        hid = r.u8()
        if hid != 0xFF:
            r.u8(); r.s()
        if not roe:
            r.u8()
            n = r.u8()
            r.skip(3)
            for _ in range(n):
                r.u8(); r.s()
        players.append({'color': i, 'human': bool(human), 'ai': bool(ai), 'town': town})
    m['players'] = players
    vc = r.u8()
    m['victory'] = vc
    if vc != 0xFF:
        r.u8(); r.u8()
        if vc == 0:
            r.u8(); r.skip(0 if roe else 1)
        elif vc == 1:
            r.u8(); r.skip(0 if roe else 1); r.u32()
        elif vc == 2:
            r.u8(); r.u32()
        elif vc == 3:
            r.skip(3 + 2)
        elif vc in (4, 5, 6, 7):
            r.skip(3)
        elif vc == 10:
            r.u8(); r.skip(3)
    lc = r.u8()
    m['loss'] = lc
    if lc in (0, 1):
        r.skip(3)
    elif lc == 2:
        r.u16()
    teams = r.u8()
    if teams:
        r.skip(8)
    r.skip(16 if roe else 20)
    if not roe:
        r.skip(r.u32())
    if sod:
        for _ in range(r.u8()):
            r.u8(); r.u8(); r.s(); r.u8()
    r.skip(31)
    if not roe:
        r.skip(17 if ab else 18)
    if sod:
        r.skip(9 + 4)
    for _ in range(r.u32()):
        r.s(); r.s()
    if sod:
        for _ in range(156):
            if not r.u8():
                continue
            if r.u8():
                r.u32()
            if r.u8():
                r.skip(2 * r.u32())
            if r.u8():
                r.skip(19 * 2)
                r.skip(2 * r.u16())
            if r.u8():
                r.s()
            r.u8()
            if r.u8():
                r.skip(9)
            if r.u8():
                r.skip(4)
    size = m['size']
    terr = [[0] * 10 for _ in range(m['levels'])]
    for z in range(m['levels']):
        for _ in range(size * size):
            t = r.d[r.p]
            r.skip(7)
            if t < 10:
                terr[z][t] += 1
    m['terrain'] = terr
    ndefs = r.u32()
    defs = []
    for _ in range(ndefs):
        name = r.s()
        r.skip(6 + 6 + 2 + 2)
        oid, sub = r.u32(), r.u32()
        r.skip(1 + 1 + 16)
        defs.append((name, oid, sub))
    m['defs'] = defs
    m['nobj'] = r.u32()
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('maps')
    ap.add_argument('--sort', default='snow')
    ap.add_argument('--max-size', type=int, default=72)
    args = ap.parse_args()
    rows = []
    for f in sorted(glob.glob(os.path.join(args.maps, '*.h3m'))):
        try:
            m = parse(f)
        except Exception as e:  # noqa: BLE001
            print('ERR', os.path.basename(f), e)
            continue
        if 'terrain' not in m or m['size'] > args.max_size:
            continue
        t0 = m['terrain'][0]
        land = sum(t0) - t0[8] - t0[9]
        snow = t0[3] / max(1, land)
        kinds = len({oid for _, oid, _ in m['defs']})
        rows.append((m, snow, kinds))
    key = {'snow': lambda x: -x[1], 'defs': lambda x: -x[2]}[args.sort]
    for m, snow, kinds in sorted(rows, key=key):
        ph = sum(p['human'] for p in m['players'])
        print(f"{m['file'][:38]:38} v{m['ver']:02x} {m['size']:3}x{m['levels']} pl={len(m['players'])} hum={ph} "
              f"snow={snow:4.0%} kinds={kinds:3} defs={len(m['defs']):3} obj={m['nobj']:4} vc={m['victory']} lc={m['loss']}")


if __name__ == '__main__':
    main()
