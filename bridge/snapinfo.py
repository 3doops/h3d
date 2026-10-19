"""Helpers for recorded snapshots: object-type table (testdata/objects_*.tsv), snapshot diff for `state`.

    python -I snapinfo.py testdata/snapshot_newcomers.json testdata/objects_newcomers.tsv
"""
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
INDEX_TSV = os.path.join(ROOT, 'assets', 'h3-original', 'index.tsv')
OBJNAMES = os.path.join(ROOT, 'assets', 'h3-original', 'text', 'ObjNames.txt')
CREATURES_TSV = os.path.join(ROOT, 'assets', 'h3-original', 'creatures.tsv')
FACTIONS = ['Castle', 'Rampart', 'Tower', 'Inferno', 'Necropolis', 'Dungeon', 'Stronghold', 'Fortress', 'Conflux',
            'Cove', 'Factory', 'Bulwark']

# HotA classes missing from the SoD ObjNames.txt (VCMI hota mod, ENGINE_API.md section 2)
HOTA_CLASSES = {139: 'HotA decoration', 140: 'HotA decoration', 141: 'HotA magic terrain', 142: 'Warehouse',
                144: 'HotA visitable', 145: 'HotA pickup', 146: 'HotA visitable (2)', 212: 'Border gate / grave'}


def def_names():
    """def name (lower case, no extension) -> English description from objects.txt (assets/h3-original/index.tsv)."""
    out = {}
    if not os.path.exists(INDEX_TSV):
        return out
    with open(INDEX_TSV, encoding='utf-8') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            p = row.get('path') or ''
            if p.lower().endswith('.def') and row.get('description'):
                out.setdefault(os.path.basename(p)[:-4].lower(), row['description'])
    return out


def class_names():
    out = dict(HOTA_CLASSES)
    if os.path.exists(OBJNAMES):
        with open(OBJNAMES, encoding='cp1251') as f:
            for i, line in enumerate(f):
                name = line.strip()
                if name and i not in out:
                    out[i] = name
    return out


def creature_names():
    out = {}
    if os.path.exists(CREATURES_TSV):
        with open(CREATURES_TSV, encoding='utf-8') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                out[int(row['index'])] = row['name']
    return out


def write_objects_tsv(snapshot, path):
    """One row per (type, subtype, def): how many objects of it are on the map, its name."""
    dn, cn, cr = def_names(), class_names(), creature_names()

    def name(t, s, d):
        n = dn.get(d.lower(), '')
        if n:
            return n
        if t == 54:
            return 'Monster: ' + cr.get(s, f'creature {s} (HotA)')
        if t == 98 and 0 <= s < len(FACTIONS):
            return 'Town: ' + FACTIONS[s]
        return ''

    cnt = Counter((o['type'], o['subtype'], o['def']) for o in snapshot['objects'])
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['type', 'subtype', 'def', 'count', 'class_name', 'name'])
        for (t, s, d), n in sorted(cnt.items()):
            w.writerow([t, s, d, n, cn.get(t, f'class {t}'), name(t, s, d)])
    return len(cnt)


# ---------- state diff (bridge.py) ----------
OBJ_KEYS = ('type', 'subtype', 'def', 'x', 'y', 'z', 'owner', 'count')


def diff_objects(old, new):
    """old/new: {id: object}. -> (changed list, removed ids)"""
    changed = [o for i, o in new.items() if i not in old or any(old[i].get(k) != o.get(k) for k in OBJ_KEYS)]
    removed = [i for i in old if i not in new]
    return changed, removed


def fog_opened(old, new, size):
    out = []
    for z, (a, b) in enumerate(zip(old, new)):
        for i, (u, v) in enumerate(zip(a, b)):
            if v and not u:
                out.append([i % size, i // size, z])
    return out


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'testdata', 'snapshot_newcomers.json')
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, 'testdata', 'objects_newcomers.tsv')
    with open(src, encoding='utf-8') as f:
        snap = json.load(f)
    print(write_objects_tsv(snap, dst), 'object kinds ->', dst)
