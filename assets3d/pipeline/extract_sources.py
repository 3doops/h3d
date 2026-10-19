"""Extract source data for the 3D asset catalog of the HoMM3 SoD + HotA adventure map.

Writes TSV tables (UTF-8, tab separated, header row) to --out (default godot/assets3d/pipeline/sources/):
  defs.tsv         one row per adventure-map object template (def, engine class:subtype, footprint, masks, ...)
  creatures.tsv    wandering-monster creatures (SoD 0..149 + HotA ids from the HotA h3m format)
  heroclasses.tsv  hero classes with their adventure-map hero DEFs (male/female variants)
  factions.tsv     towns with their adventure-map DEFs per fort level
and to --work (default _work/assets3d_src/):
  hota_cfg/<submod>/...     JSON configs extracted from the VCMI hota mod zips
  hota_defs/<submod>/...    referenced HotA DEF / JSON+PNG sprites extracted from the zips
  hota_png/<def>.png        first frame of every referenced HotA sprite (hero/boat DEFs: facing east)
  sheets/*.png, index.tsv   preview contact sheets (visitable, decorations, other, creatures, heroes, towns)

Sources (read as data, nothing from them is executed):
  SoD : assets/h3-original/interface/common/Objects.txt (+ index.tsv, creatures.tsv, text/*.txt of the same pack),
        vcmi/config (creatures, factions, heroClasses, objects) for ids, levels and VCMI object bases.
  HotA: vcmi/userdata/Mods/hota (mod.json of every submod + content.zip); engine (h3m) ids from
        mods/mapSupport/mod.json settings.mapFormat.hornOfTheAbyss (objects: by object name / type name;
        templates of core:* objects without an entry: VCMI core index = SoD class:subtype).
  HotA maps: the .h3m/.h3c shipped in the mod (mods/maps) list the templates the HotA editor wrote
        (DEF, masks, class, subtype) -> class MapTemplates. Used as ground truth for ids: unmapped templates take
        the id from the maps, VCMI-derived ids that the maps contradict are replaced (towns excepted), creature
        map DEFs follow the maps (VCMI swaps Stormbird/Ayssid), templates only seen in >= 3 map uses are added
        (source 'hota:maps'). Masks are NOT replaced; a differing HotA-maps mask is written to notes.

Conventions (verified on the data, see verify_sod_masks()):
  * OBJECTS.TXT masks: 48 chars, char i = row i//8 counted from the BOTTOM, column i%8 counted from the RIGHT
    (the anchor = bottom-right cell is char 0; same as VCMI ObjectTemplate::readTxt usedTiles[i][j]).
    Passability '0' = blocked, action '1' = visitable.
  * VCMI JSON masks: list of rows top->bottom, each row right-aligned on the anchor column; chars
    B/H = blocked, A/T = blocked+visitable, V/0/space = free (lib/mapObjects/ObjectTemplate.cpp readJson).
  * Output block/visit: rows top->bottom joined with '/', 8 chars each (left->right), '1' = blocked/visitable,
    at least 6x8, bottom-right aligned (anchor = last char of the last row). w/h = bounding box of blocked|visitable.
  * land     : raw OBJECTS.TXT "landscape" bits (column 4; char 8-i = terrain i), empty for HotA.
    terrains : readable terrain set - OBJECTS.TXT column 5 (VCMI allowedTerrains, i.e. terrain-specific graphics)
               or HotA allowedTerrains; 'any' = every land terrain.
  * group    : visitable (has a visitable cell) | decoration (no visitable cell, some blocked cell) |
               other (no blocked/visitable cell - ground overlays, river deltas - or editor-only placeholders).
  * Heroes (class 34) are not in defs.tsv (see heroclasses.tsv); HotA creatures/artifacts/towns are.

usage (from the repo root):
  uv run --no-project --python 3.12 --with pillow --with numpy python -I godot/assets3d/pipeline/extract_sources.py
      [--repo DIR] [--hota DIR] [--vcmi-config DIR] [--h3 DIR] [--work DIR] [--out DIR] [--no-sheets]
"""
import argparse
import collections
import copy
import csv
import gzip
import io
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[3]

BLOCK, VISIT = 1, 2
VCMI_MASK_CHARS = {'B': BLOCK, 'H': BLOCK, 'A': BLOCK | VISIT, 'T': BLOCK | VISIT, 'V': 0, '0': 0, ' ': 0}

# OBJECTS.TXT landscape bit order (char 8-i = terrain i), as in VCMI ObjectTemplate::readTxt
TERRAINS = ['dirt', 'sand', 'grass', 'snow', 'swamp', 'rough', 'subterra', 'lava', 'water']
TERRAIN_ORDER = TERRAINS + ['rock', 'highlands', 'wasteland']

# Engine classes without a name in OBJNAMES.TXT (HotA container classes; names as used by the HotA editor).
HOTA_CLASS_NAMES = {40: 'HotA Decoration (bluebottle)', 139: 'HotA Decoration 1', 140: 'HotA Decoration 2',
                    141: 'HotA Ground Overlay', 142: 'Warehouse', 144: 'HotA Visitable 1', 145: 'HotA Pickable',
                    146: 'HotA Visitable 2'}
# Editor-only placeholders (replaced at map start / invisible): grouped as "other".
PLACEHOLDER_CLASSES = {26, 36, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 162, 163, 164, 214, 216, 217, 218}

KEY_COLORS = ['Light Blue', 'Green', 'Red', 'Dark Blue', 'Brown', 'Purple', 'White', 'Black']
SOD_FACTIONS = ['castle', 'rampart', 'tower', 'inferno', 'necropolis', 'dungeon', 'stronghold', 'fortress', 'conflux']
TOWN_LEVELS = ['village', 'fort', 'citadel', 'castle', 'capitol']
HERO_DEF_TYPE, OBJECT_DEF_TYPE = 0x44, 0x43


# --------------------------------------------------------------------------- small helpers

def load_tools(repo):
    """Reuse the project's DEF/JSON readers from _tools/unpack_h3_original.py."""
    sys.path.insert(0, str(repo / '_tools'))
    import unpack_h3_original
    return unpack_h3_original


def stem(path):
    """'hota/trees/AVLswn01.def' -> 'avlswn01'"""
    name = str(path).replace('\\', '/').rsplit('/', 1)[-1]
    if ':' in name:                      # 'AvWattak.def:0:0' frame selectors
        name = name.split(':', 1)[0]
    if '.' in name and name.rsplit('.', 1)[1].lower() in ('def', 'json', 'png'):
        name = name.rsplit('.', 1)[0]
    return name.lower()


def split_scope(key):
    return tuple(key.split(':', 1)) if ':' in key else ('', key)


def merge(dst, src):
    """JsonUtils::merge: values of src override dst, structs are merged recursively (returns a new object)."""
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return copy.deepcopy(src)
    out = dict(dst)
    for k, v in src.items():
        out[k] = merge(out[k], v) if k in out and isinstance(out[k], dict) and isinstance(v, dict) else copy.deepcopy(v)
    return out


def inherit(node, base):
    """JsonUtils::inherit: node keeps its values, missing ones come from base."""
    if not isinstance(base, dict):
        return node
    return merge(base, node if isinstance(node, dict) else {})


def humanize(key):
    out = ''
    for i, ch in enumerate(key):
        if ch.isupper() and i and (key[i - 1].islower() or key[i - 1].isdigit()):
            out += ' '
        out += ch
    return out[:1].upper() + out[1:]


def text_rows(path, skip=0):
    text = Path(path).read_bytes().decode('cp1252').replace('\r\n', '\n').replace('\r', '\n')
    return list(csv.reader(io.StringIO(text), delimiter='\t', quotechar='"', strict=False))[skip:]


def first_col(path, skip=0, keep_empty=False):
    rows = [(r[0].strip() if r else '') for r in text_rows(path, skip)]
    return rows if keep_empty else [r for r in rows if r]


def write_tsv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write('\t'.join(header) + '\n')
        for r in rows:
            f.write('\t'.join(str(r.get(h, '')).replace('\t', ' ').replace('\n', ' ') for h in header) + '\n')


def rel(path, repo):
    return Path(path).resolve().relative_to(repo.resolve()).as_posix()


# --------------------------------------------------------------------------- masks

class Mask:
    """Footprint grid, rows top->bottom, columns left->right, anchor = bottom-right cell."""

    def __init__(self, rows=6, cols=8):
        self.cells = [[0] * cols for _ in range(rows)]

    @classmethod
    def from_txt(cls, block, visit):
        m = cls()
        for i in range(48):
            r, c = 5 - i // 8, 7 - i % 8          # char i: row i//8 from the bottom, column i%8 from the right
            m.cells[r][c] = (BLOCK if block[i] == '0' else 0) | (VISIT if visit[i] == '1' else 0)
        return m

    @classmethod
    def from_vcmi(cls, lines, bad_chars):
        lines = [str(x) for x in lines]
        h, w = len(lines), max((len(x) for x in lines), default=0)
        m = cls(max(6, h), max(8, w))
        rows, cols = len(m.cells), len(m.cells[0])
        for i, line in enumerate(lines):
            for j, ch in enumerate(line):
                if ch not in VCMI_MASK_CHARS:
                    bad_chars[ch] += 1
                m.cells[rows - h + i][cols - len(line) + j] = VCMI_MASK_CHARS.get(ch, 0)
        return m

    @classmethod
    def from_h3m(cls, block, visit):
        """h3m template masks: byte i = row i from the top, bit j = column j from the left (VCMI readMap)."""
        m = cls()
        for i in range(6):
            for j in range(8):
                m.cells[i][j] = (0 if block[i] >> j & 1 else BLOCK) | (VISIT if visit[i] >> j & 1 else 0)
        return m

    def string(self, bit):
        return '/'.join(''.join('1' if v & bit else '0' for v in row) for row in self.cells)

    def footprint(self):
        used = [(r, c) for r, row in enumerate(self.cells) for c, v in enumerate(row) if v]
        if not used:
            return 0, 0
        rs, cs = [u[0] for u in used], [u[1] for u in used]
        return max(cs) - min(cs) + 1, max(rs) - min(rs) + 1

    def has(self, bit):
        return any(v & bit for row in self.cells for v in row)


def terrains_from_bits(bits):
    names = [TERRAINS[i] for i in range(9) if bits[8 - i] == '1']
    if set(TERRAINS[:8]) <= set(names) and 'water' not in names:
        return 'any'
    return ','.join(names)


def or_bits(a, b):
    return ''.join('1' if '1' in (x, y) else '0' for x, y in zip(a, b))


# --------------------------------------------------------------------------- HotA mod file system

class ModFS:
    """All submods of the VCMI hota mod: mod.json + content.zip, case-insensitive lookup like VCMI."""

    def __init__(self, root, work, u):
        self.root, self.work, self.u = Path(root), Path(work), u
        self.submods = []                       # (name, dir, mod.json data)
        self.files = collections.defaultdict(list)   # lower member path -> [(submod, zip path, member)]
        for mj in sorted(self.root.rglob('mod.json')):
            if '.github' in mj.parts:
                continue
            d = mj.parent
            name = d.relative_to(self.root).as_posix()
            name = 'hota' if name == '.' else name.removeprefix('mods/')
            self.submods.append((name, d, u.load_json(mj)))
            z = d / 'content.zip'
            if z.exists():
                with zipfile.ZipFile(z) as zf:
                    for member in sorted(zf.namelist()):
                        if not member.endswith('/'):
                            self.files[member.lower()].append((name, z, member))
        self.collisions = sum(1 for v in self.files.values() if len(v) > 1)

    def find(self, path, prefer=None):
        hits = self.files.get(path.lower().replace('\\', '/'), [])
        for h in hits:
            if h[0] == prefer:
                return h
        return hits[0] if hits else None

    def read(self, hit):
        with zipfile.ZipFile(hit[1]) as zf:
            return zf.read(hit[2])

    def extract(self, hit, subdir):
        dst = self.work / subdir / hit[0] / hit[2]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(self.read(hit))
        return dst

    def config_files(self, kind):
        """[(submod, config path as listed, extracted Path)] for one content type ('objects', 'creatures', ...)."""
        if not hasattr(self, '_cfg_cache'):
            self._cfg_cache = {}
        if kind not in self._cfg_cache:
            self._cfg_cache[kind] = self._config_files(kind)
        return self._cfg_cache[kind]

    def _config_files(self, kind):
        out = []
        for name, _d, mod in self.submods:
            for entry in mod.get(kind, []) if isinstance(mod.get(kind), list) else []:
                p = entry if entry.lower().endswith('.json') else entry + '.json'
                hit = self.find(p, name)
                if hit is None:
                    print(f'  ! {name}: config {p} not found in content.zip')
                    continue
                out.append((name, p, self.extract(hit, 'hota_cfg')))
        return out

    def load(self, path):
        return self.u.load_json(path)

    def find_sprite_by_stem(self, d):
        """First sprites/**/<d>.def|json in the zips (sorted by path)."""
        for m in sorted(self.files):
            if m.startswith('sprites/') and m.endswith(('.def', '.json')) and stem(m) == d:
                return self.files[m][0]
        return None

    def find_sprite(self, ref, prefer=None):
        """Animation reference -> zip hit of the DEF or JSON animation ('Sprites/' prefix, any extension)."""
        ref = ref.replace('\\', '/').lstrip('/').split(':', 1)[0]
        base = ref.rsplit('.', 1)[0] if ref.lower().endswith(('.def', '.json')) else ref
        for ext in ('.def', '.json'):
            for p in ('sprites/' + base + ext, base + ext):
                hit = self.find(p, prefer)
                if hit:
                    return hit
        return None


class MapTemplates:
    """Object templates stored in the HotA maps / campaigns shipped with the mod (mods/maps/...).

    Every h3m lists the templates it uses: string DEF name, 6+6 mask bytes, u16 landscape, u16 editor group,
    u32 class, u32 subtype, u8 group, u8 priority, 16 zero bytes. These are written by the HotA editor, so they
    give authoritative HotA class:subtype ids and masks for every DEF that occurs in those maps. The records are
    found by pattern (length-prefixed '*.def' string) in the gunzipped data - no full h3m parser needed."""
    PAT = re.compile(rb'([\x05-\x3f])\x00\x00\x00([A-Za-z0-9_\-]{1,60}\.[dD][eE][fF])')

    def __init__(self, fs):
        self.t = collections.defaultdict(collections.Counter)   # def -> Counter((cls, sub, block, visit))
        self.maps = 0
        for member in sorted(fs.files):
            if not member.endswith(('.h3m', '.h3c')):
                continue
            try:
                data = gzip.decompress(fs.read(fs.files[member][0]))
            except (OSError, EOFError):
                continue
            self.maps += 1
            for m in self.PAT.finditer(data):
                name, p = m.group(2), m.end()
                if len(name) != m.group(1)[0] or p + 42 > len(data) or any(data[p + 26:p + 42]):
                    continue
                cls, sub = struct.unpack_from('<II', data, p + 16)
                if cls < 256 and sub < 4096:
                    self.t[stem(name.decode())][(cls, sub, data[p:p + 6], data[p + 6:p + 12])] += 1

    def ids(self, d):
        """-> [(class, subtype)] by frequency (heroes standing on boats etc. ignored)."""
        c = collections.Counter()
        for (cls, sub, _b, _v), n in self.t.get(d, {}).items():
            if cls != 34 or d.startswith('ah'):
                c[(cls, sub)] += n
        return [k for k, _n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]

    def creature_def(self, cid):
        """Most frequent DEF of monster templates (class 54) with subtype = creature id."""
        if not hasattr(self, '_cr'):
            self._cr = collections.defaultdict(collections.Counter)
            for d, c in self.t.items():
                for (cls, sub, _b, _v), n in c.items():
                    if cls == 54:
                        self._cr[sub][d] += n
        c = self._cr.get(cid)
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if c else None

    def count(self, d, cls, sub):
        return sum(n for (c_, s_, _b, _v), n in self.t.get(d, {}).items() if (c_, s_) == (cls, sub))

    def mask(self, d, cls, sub):
        c = [(n, b, v) for (c_, s_, b, v), n in self.t.get(d, {}).items() if (c_, s_) == (cls, sub)]
        if not c:
            return None
        _n, b, v = sorted(c, key=lambda x: (-x[0], x[1], x[2]))[0]
        return Mask.from_h3m(b, v)


# --------------------------------------------------------------------------- sprite -> PNG

class SpriteConverter:
    """First frame of a DEF (project reader) or VCMI JSON animation -> RGBA PNG in hota_png/<def>.png."""

    def __init__(self, fs, out_dir, repo):
        self.fs, self.out, self.repo = fs, Path(out_dir), repo
        self.done = {}            # (submod, member) -> png repo path
        self.names = {}           # png stem -> (submod, member)
        self.errors = []

    def convert(self, hit):
        if hit in self.done:
            return self.done[hit]
        name = stem(hit[2])
        png_name = name
        n = 1
        while png_name in self.names and self.names[png_name] != hit:
            n += 1
            png_name = f'{name}__{n}'
        self.names[png_name] = hit
        dst = self.out / f'{png_name}.png'
        try:
            src = self.fs.extract(hit, 'hota_defs')
            if src.suffix.lower() == '.json':
                self._json_frame(src, hit, dst)
            else:
                self._def_frame(src.read_bytes(), dst)
            self.done[hit] = rel(dst, self.repo)
        except Exception as e:  # keep going, report at the end
            self.errors.append(f'{hit[0]}:{hit[2]}: {type(e).__name__}: {e}')
            self.done[hit] = ''
        return self.done[hit]

    def _def_frame(self, data, dst):
        import numpy as np
        u = self.fs.u
        dtype, _w, _h, ngroups = struct.unpack_from('<4I', data, 0)
        pal = np.frombuffer(data, np.uint8, 768, 16).reshape(256, 3)
        rgba_pal = np.concatenate([pal, np.full((256, 1), 255, np.uint8)], axis=1)
        for i in u._SLOTS.get(dtype, (0,)):
            rgba_pal[i] = u._SPECIAL[i]
        if dtype in (OBJECT_DEF_TYPE, HERO_DEF_TYPE):
            rgba_pal[5] = u.NEUTRAL_FLAG
        groups, pos = [], 16 + 768
        for _ in range(ngroups):
            gid, count = struct.unpack_from('<2I', data, pos)
            pos += 16 + 13 * count
            offs = struct.unpack_from(f'<{count}I', data, pos)
            pos += 4 * count
            if offs:
                groups.append((gid, offs))
        if not groups:
            raise ValueError('DEF without frames')
        # heroes / boats: group 2 = standing, facing east (the usual adventure-map look)
        gid, offs = next((g for g in groups if dtype == HERO_DEF_TYPE and g[0] == 2), groups[0])
        u.write_png(dst, rgba_pal[u.def_frame(data, offs[0])])

    def _json_frame(self, src, hit, dst):
        from PIL import Image
        anim = self.fs.load(src)
        images = [im for im in anim.get('images', []) if isinstance(im, dict) and im.get('file')]
        if not images:
            raise ValueError('JSON animation without images')
        hero_like = any(im.get('group', 0) == 2 for im in images) and stem(hit[2]).startswith('ah')
        images.sort(key=lambda im: (0 if hero_like and im.get('group', 0) == 2 else 1, im.get('group', 0), im.get('frame', 0)))
        im = images[0]
        f = (anim.get('basepath', '') + im['file']).replace('\\', '/')
        cands = [f] if '.' in f.rsplit('/', 1)[-1] else [f + '.png', f]
        frame = None
        for c in cands:
            frame = self.fs.find('sprites/' + c, hit[0]) or self.fs.find(c, hit[0])
            if frame:
                break
        if frame is None:
            raise FileNotFoundError(f'frame {f}')
        path = self.fs.extract(frame, 'hota_defs')
        Image.open(path).convert('RGBA').save(dst)


# --------------------------------------------------------------------------- SoD

class SoD:
    def __init__(self, h3, vcmi_cfg, u, repo):
        self.h3, self.cfg, self.u, self.repo = Path(h3), Path(vcmi_cfg), u, repo
        t = self.h3 / 'text'
        self.obj_names = first_col(t / 'ObjNames.txt', keep_empty=True)
        self.art_names = first_col(t / 'artraits.txt', 2)
        self.crgen1 = first_col(t / 'CrGen1.txt', keep_empty=True)
        self.crgen4 = first_col(t / 'CrGen4.txt', keep_empty=True)
        self.mines = first_col(t / 'MineName.txt', keep_empty=True)
        self.restypes = first_col(t / 'RESTYPES.TXT', keep_empty=True)
        self.towns = first_col(t / 'TownType.txt', keep_empty=True)
        self.cr_names = first_col(t / 'CRTRAITS.TXT', 2)
        self.class_names = first_col(t / 'HCTRAITS.TXT', 2)
        self.banks = [r[0].strip() for r in text_rows(t / 'CrBanks.txt', 2) if r and r[0].strip()]
        # preview PNG of every DEF of the pack (case-insensitive by name)
        self.png = {}
        self.def_path = {}
        for row in csv.DictReader(open(self.h3 / 'index.tsv', encoding='utf-8'), delimiter='\t'):
            if row['path'].lower().endswith('.def'):
                s = stem(row['path'])
                self.def_path.setdefault(s, row['path'])
                if row['png']:
                    self.png.setdefault(s, rel(self.h3 / row['png'], repo))
        # VCMI core object classes: index -> object json, name -> object json
        self.core_objects = {}
        for f in sorted((self.cfg / 'objects').glob('*.json')):
            for k, v in u.load_json(f).items():
                if isinstance(v, dict):
                    self.core_objects[k] = v
        self.core_by_index = {v['index']: (k, v) for k, v in self.core_objects.items() if 'index' in v}

    def class_name(self, cls):
        name = self.obj_names[cls] if 0 <= cls < len(self.obj_names) else ''
        return name or self.u.OBJ_NAMES_EXTRA.get(cls, '') or HOTA_CLASS_NAMES.get(cls, '')

    def subtype_name(self, cls, sub):
        def pick(table):
            return table[sub] if 0 <= sub < len(table) else ''
        table = {5: self.art_names, 17: self.crgen1, 20: self.crgen4, 53: self.mines, 79: self.restypes,
                 54: self.cr_names, 98: self.towns, 16: self.banks, 34: self.class_names}.get(cls)
        if table is not None and pick(table):
            return pick(table)
        if cls == 53 and sub == 7:
            return 'Abandoned Mine'
        if cls in (9, 10, 212) and 0 <= sub < len(KEY_COLORS):
            return KEY_COLORS[sub]
        core = self.core_by_index.get(cls)
        if core:
            for tkey, t in core[1].get('types', {}).items():
                if isinstance(t, dict) and t.get('index') == sub:
                    if isinstance(t.get('name'), str):
                        return t['name']
                    return '' if tkey == 'object' else humanize(tkey)
        return ''

    def core_template_base(self, cls, tkey=None):
        """Template defaults of a VCMI core object (object base.base and type base)."""
        core = self.core_by_index.get(cls)
        if not core:
            return {}
        obj = core[1]
        t = inherit(obj.get('types', {}).get(tkey, {}) if tkey else {}, obj.get('base', {}))
        return t.get('base', {}) if isinstance(t.get('base'), dict) else {}

    def objects_txt(self):
        rows = {}
        lines = (self.h3 / 'interface' / 'common' / 'Objects.txt').read_bytes().decode('cp1252').splitlines()[1:]
        for line in lines:
            p = line.split()
            if len(p) < 9:
                continue
            d, cls, sub = stem(p[0]), int(p[5]), int(p[6])
            if d == 'default':
                continue
            key = (d, cls, sub)
            if key in rows:      # same DEF listed for several terrain groups: merge the terrain bits
                r = rows[key]
                if (r['_block'], r['_visit']) != (p[1], p[2]):
                    r['notes'] = 'differing masks in OBJECTS.TXT (first kept)'
                r['land'], r['_terr'] = or_bits(r['land'], p[3]), or_bits(r['_terr'], p[4])
                continue
            m = Mask.from_txt(p[1], p[2])
            rows[key] = dict(_mask=m, _block=p[1], _visit=p[2], _terr=p[4], land=p[3], **{'def': d}, cls=cls, sub=sub,
                             source='objects.txt', png=self.png.get(d, ''), notes='')
        out = []
        for r in rows.values():
            r['terrains'] = terrains_from_bits(r.pop('_terr'))
            del r['_block'], r['_visit']
            out.append(r)
        return out

    def creature_config(self):
        """VCMI core creature json: key -> data (with index)."""
        out = {}
        for f in sorted((self.cfg / 'creatures').glob('*.json')):
            for k, v in self.u.load_json(f).items():
                if isinstance(v, dict) and 'index' in v:
                    v = dict(v)
                    v.setdefault('faction', f.stem)
                    out[k] = v
        return out


def verify_sod_masks(rows):
    """Check the OBJECTS.TXT orientation on objects with a known shape; returns printable lines."""
    by_def = {r['def']: r for r in rows}
    checks = []

    def check(d, want_wh, want_visit_cell, what):
        r = by_def.get(d)
        if not r:
            checks.append(f'  ? {d}: not in OBJECTS.TXT')
            return
        m = r['_mask']
        wh = m.footprint()
        vis = [(rr, c) for rr, row in enumerate(m.cells) for c, v in enumerate(row) if v & VISIT]
        ok = wh == want_wh and (want_visit_cell is None or vis == [want_visit_cell])
        checks.append(f'  {"ok" if ok else "MISMATCH"} {d}: {what}; footprint {wh[0]}x{wh[1]}, visit {vis}')
    check('avtgold0', (1, 1), (5, 7), 'gold pile 1x1, visitable anchor (row 5 col 7)')
    check('avccasx0', (5, 3), (5, 5), 'Castle town 5x3, gate in the bottom row centre (col 5 of cols 3..7)')
    check('avmgold0', (3, 1), (5, 6), 'gold mine: 3 blocked bottom cells, entrance in the middle (top row is only visible)')
    check('avxprsn0', (2, 1), (5, 7), 'prison: 2 cells, visitable anchor')
    return checks


# --------------------------------------------------------------------------- HotA

class HotA:
    def __init__(self, fs, sod, conv, maps):
        self.fs, self.sod, self.conv, self.maps = fs, sod, conv, maps
        self.via_maps = []           # templates whose id comes from the HotA maps (no mapSupport entry)
        self.id_overrides = []       # (def, VCMI-derived id, id used in the HotA maps)
        self.creature_fixes = []     # (creature id, name, DEF the HotA maps use)
        ms = next(m for n, _d, m in fs.submods if n == 'mapSupport')
        self.fmt = ms['settings']['mapFormat']['hornOfTheAbyss']
        self.obj_map = self.fmt.get('objects', {})
        self.tmpl_names = {stem(k): stem(v) for k, v in self.fmt.get('templates', {}).items()}
        self.bad_mask_chars = collections.Counter()
        self.unmapped = []           # (object, type, def, file)
        self.missing_sprites = []
        self.used_sprites = set()

    # --- sprite helpers

    def sprite(self, ref, prefer):
        """-> (def name, png path or '', origin) for an animation reference."""
        hit = self.fs.find_sprite(ref, prefer)
        d = stem(ref)
        if hit:
            self.used_sprites.add(hit)
            return d, self.conv.convert(hit), 'hota'
        if d in self.sod.png:
            return d, self.sod.png[d], 'sod'
        self.missing_sprites.append(f'{prefer}: {ref}')
        return d, '', 'missing'

    # --- objects

    def h3m_id(self, oname, tkey, ntypes, is_core, core_obj):
        m = self.obj_map.get(oname)
        if isinstance(m, dict) and tkey in m:
            return tuple(m[tkey]), ''
        if isinstance(m, list) and tkey == oname:
            return tuple(m), ''
        if is_core and core_obj:
            t = core_obj.get('types', {}).get(tkey)
            if isinstance(t, dict) and 'index' in t and 'index' in core_obj:
                return (core_obj['index'], t['index']), ''
        if isinstance(m, list) and ntypes == 1:
            return tuple(m), f'mapSupport "{oname}" applied to its only type "{tkey}"'
        return None, ''

    def objects(self):
        """All templates added by the mod's object configs -> defs rows."""
        groups = collections.OrderedDict()     # (is_core, name) -> {'obj': merged json, 'origin': {(type, tmpl): file}}
        for sub, cfg, path in self.fs.config_files('objects'):
            for okey, obj in self.fs.load(path).items():
                if not isinstance(obj, dict):
                    continue
                scope, name = split_scope(okey)
                gk = (scope == 'core', name)
                if gk not in groups:
                    base = copy.deepcopy(self.sod.core_objects.get(name, {})) if scope == 'core' else {}
                    groups[gk] = {'obj': base, 'origin': {}}
                g = groups[gk]
                g['obj'] = merge(g['obj'], obj)
                for tkey, t in (obj.get('types') or {}).items():
                    for tmkey in ((t or {}).get('templates') or {}) if isinstance(t, dict) else {}:
                        g['origin'][(tkey, tmkey)] = (sub, cfg)
        rows = []
        for (is_core, name), g in groups.items():
            obj = g['obj']
            core_obj = self.sod.core_objects.get(name) if is_core else None
            types = obj.get('types') or {}
            for tkey, t in types.items():
                if not isinstance(t, dict):
                    continue
                t_eff = inherit(t, obj.get('base', {}))
                core_type = isinstance(core_obj, dict) and 'index' in (core_obj.get('types', {}).get(tkey) or {})
                for tmkey, tm in (t_eff.get('templates') or {}).items():
                    origin = g['origin'].get((tkey, tmkey))
                    if origin is None or not isinstance(tm, dict) or not tm.get('animation'):
                        continue
                    src = f'hota:{origin[0]}/{origin[1]}'
                    tm_eff = inherit(tm, t_eff.get('base', {}))
                    ids, note = self.h3m_id(name, tkey, len(types), is_core, core_obj)
                    label = ('core:' if is_core else '') + name
                    if ids is None and self.maps.ids(stem(tm['animation'])):
                        ids, note = self.maps.ids(stem(tm['animation']))[0], 'id from the HotA maps (no mapSupport entry)'
                        self.via_maps.append((label, tkey, stem(tm['animation']), ids))
                    if ids is None:
                        self.unmapped.append((label, tkey, stem(tm['animation']), src))
                        continue
                    cls, sub = int(ids[0]), int(ids[1])
                    if 'mask' not in tm_eff:
                        tm_eff = inherit(tm_eff, self.sod.core_template_base(cls, tkey if is_core else None))
                        note = (note + '; ' if note else '') + 'mask from VCMI core object base'
                    if core_type:      # existing SoD subtype: name comes from the SoD text tables
                        sub_name = ''
                    else:
                        tname = t_eff.get('name') if isinstance(t_eff.get('name'), str) else ''
                        oname = obj.get('name') if isinstance(obj.get('name'), str) else humanize(name)
                        sub_name = tname or (humanize(tkey) if tkey not in ('object', name) else oname)
                    rows.append(self.row(tm_eff, cls, sub, src, origin[0], sub_name, note))
        return rows

    def row(self, tm, cls, sub, source, prefer, sub_name='', note=''):
        d, png, origin = self.sprite(tm['animation'], prefer)
        d = self.tmpl_names.get(d, d)
        mask = Mask.from_vcmi(tm.get('mask') or [], self.bad_mask_chars)
        if not tm.get('mask'):
            note = (note + '; ' if note else '') + 'no mask'
        if origin == 'sod':
            note = (note + '; ' if note else '') + 'SoD sprite'
        terr = tm.get('allowedTerrains')
        terr = ','.join(sorted(terr, key=lambda x: TERRAIN_ORDER.index(x) if x in TERRAIN_ORDER else 99)) if terr else ''
        return {'def': d, 'cls': cls, 'sub': sub, '_mask': mask, 'land': '', 'terrains': terr, 'source': source,
                'png': png, 'subtype_name': sub_name, 'notes': note}

    # --- creatures, artifacts, hero classes, factions

    def artifact_rows(self):
        rows = []
        amap = self.fmt.get('artifacts', {})
        for sub, cfg, path in self.fs.config_files('artifacts'):
            for k, v in self.fs.load(path).items():
                _scope, name = split_scope(k)
                if not isinstance(v, dict) or name not in amap:
                    continue
                g = v.get('graphics', {})
                if not isinstance(g, dict) or not g.get('map'):
                    continue
                base = self.sod.core_template_base(5)
                tm = inherit({'animation': g['map']}, base)
                nm = v.get('name') if isinstance(v.get('name'), str) else humanize(name)
                rows.append(self.row(tm, 5, amap[name], f'hota:{sub}/{cfg}', sub, nm, 'mask from VCMI core object base'))
        return rows

    def merged_entries(self, kind, field=None):
        """Config entries of one kind merged by unscoped key (all submods, core:* overrides included).

        -> key -> (merged json, [(submod, cfg)] of all definitions, is_core, (submod, cfg) of the last
        definition that contains `field` (a '/'-separated path) or None)."""
        out = collections.OrderedDict()
        for sub, cfg, path in self.fs.config_files(kind):
            for k, v in self.fs.load(path).items():
                if not isinstance(v, dict):
                    continue
                scope, name = split_scope(k)
                cur = out.get(name, ({}, [], False, None))
                node = v
                for part in (field.split('/') if field else []):
                    node = node.get(part) if isinstance(node, dict) else None
                has = (sub, cfg) if field and node else cur[3]
                out[name] = (merge(cur[0], v), cur[1] + [(sub, cfg)], cur[2] or scope == 'core', has)
        return out


# --------------------------------------------------------------------------- tables

def add_note(r, text):
    r['notes'] = (r['notes'] + '; ' if r.get('notes') else '') + text


def build_defs(sod, hota, creature_rows_hota, town_rows):
    """SoD OBJECTS.TXT rows + every HotA template (objects, artifacts, creatures, towns); cross-checked against
    the templates found in the HotA maps (ids and masks; the VCMI / OBJECTS.TXT mask is kept, differences go
    to notes; a template without any mask takes the one from the maps)."""
    rows = sod.objects_txt()
    keys = {(r['def'], r['cls'], r['sub']) for r in rows}
    hota_rows = hota.objects() + hota.artifact_rows() + creature_rows_hota + town_rows
    stats = collections.Counter()
    for r in hota_rows:
        # ids derived from VCMI (core index / mapSupport) lose against what the HotA editor wrote into the maps;
        # towns are kept (map makers reuse town DEFs as rock/decoration objects)
        ids = hota.maps.ids(r['def'])
        if ids and (r['cls'], r['sub']) not in ids and r['cls'] != 98:
            add_note(r, f'id from the HotA maps (VCMI/mapSupport: {r["cls"]}:{r["sub"]})')
            hota.id_overrides.append((r['def'], (r['cls'], r['sub']), ids[0]))
            r['cls'], r['sub'] = ids[0]
        k = (r['def'], r['cls'], r['sub'])
        if k in keys:
            stats['hota_dup'] += 1
            continue
        keys.add(k)
        rows.append(r)
    # templates the HotA editor writes into maps but the VCMI mod does not define (ids + masks from the maps)
    known = {r['def'] for r in rows}
    for d in sorted(hota.maps.t):
        ids = [i for i in hota.maps.ids(d) if i[0] not in PLACEHOLDER_CLASSES and i[0] != 34]
        if d in known or d == 'default' or not ids or hota.maps.count(d, *ids[0]) < 3:
            continue        # heroes / placeholders / single odd uses by map makers are not catalog templates
        hit = hota.fs.find_sprite_by_stem(d)
        png = sod.png.get(d) or (hota.conv.convert(hit) if hit else '')
        rows.append({'def': d, 'cls': ids[0][0], 'sub': ids[0][1], '_mask': hota.maps.mask(d, *ids[0]), 'land': '',
                     'terrains': '', 'source': 'hota:maps', 'png': png, 'subtype_name': '',
                     'notes': 'template only found in the HotA maps' + ('' if png else '; no sprite in the mod')})
        stats['maps_only'] += 1
    id_mismatch = []
    for r in rows:
        m = r.pop('_mask')
        side = 'sod' if r['source'] == 'objects.txt' else 'hota'
        if r['source'] == 'hota:maps':
            side = 'maps'
        ids = hota.maps.ids(r['def'])
        if ids:
            stats[f'{side}_in_maps'] += 1
            if (r['cls'], r['sub']) not in ids:
                id_mismatch.append((r['def'], (r['cls'], r['sub']), ids[:3]))
                add_note(r, 'HotA maps use ' + ', '.join(f'{c}:{s}' for c, s in ids[:3]))
        hm = hota.maps.mask(r['def'], r['cls'], r['sub'])
        if hm and not (m.has(BLOCK) or m.has(VISIT)) and (hm.has(BLOCK) or hm.has(VISIT)):
            m = hm
            add_note(r, 'mask from the HotA maps')
            stats[f'{side}_mask_from_maps'] += 1
        elif hm and len(m.cells) == 6 and len(m.cells[0]) == 8 and \
                (hm.string(BLOCK), hm.string(VISIT)) != (m.string(BLOCK), m.string(VISIT)):
            stats[f'{side}_mask_differs'] += 1
            add_note(r, f'HotA maps mask block={hm.string(BLOCK)} visit={hm.string(VISIT)}')
        r['w'], r['h'] = m.footprint()
        r['block'], r['visit'] = m.string(BLOCK), m.string(VISIT)
        r['class'], r['subtype'] = r.pop('cls'), r.pop('sub')
        r['class_name'] = sod.class_name(r['class']) or r.get('subtype_name', '')
        if not r.get('subtype_name'):
            r['subtype_name'] = sod.subtype_name(r['class'], r['subtype'])
        if r['class'] in PLACEHOLDER_CLASSES or not (m.has(BLOCK) or m.has(VISIT)):
            r['group'] = 'other'
        else:
            r['group'] = 'visitable' if m.has(VISIT) else 'decoration'
    rows.sort(key=lambda r: (r['source'] != 'objects.txt', r['class'], r['subtype'], r['def']))
    return rows, stats, id_mismatch


def build_creatures(sod, hota, defs_sod):
    """creatures.tsv rows + HotA creature map templates (class 54) for defs.tsv.

    ids: SoD = VCMI core config "index" (= CRTRAITS.TXT row); HotA = mapSupport mod.json
    settings.mapFormat.hornOfTheAbyss.creatures (VCMI creature key -> h3m creature id)."""
    cmap = hota.fmt.get('creatures', {})
    sod_tsv = {int(r['index']): r for r in csv.DictReader(open(sod.h3 / 'creatures.tsv', encoding='utf-8'), delimiter='\t')}
    sod54 = {r['sub']: r for r in defs_sod if r['cls'] == 54}
    by_key = collections.OrderedDict()
    for key, c in sod.creature_config().items():
        i = c['index']
        name = sod.cr_names[i] if i < len(sod.cr_names) else key
        if name.startswith('NOT USED'):
            continue
        t = sod_tsv.get(i, {})
        map_def = stem(t['map_def']) if t.get('map_def') else ''
        by_key[key] = {'id': i, 'name': name, 'faction': c.get('faction', ''), 'level': c.get('level', 0),
                       'upgrades': list(c.get('upgrades', [])), 'map_def': map_def, 'png': sod.png.get(map_def, ''),
                       'notes': '', '_mask': None, '_src': None}
    skipped = []
    for name, (c, srcs, _is_core, map_src) in hota.merged_entries('creatures', 'graphics/map').items():
        g = c.get('graphics', {}) if isinstance(c.get('graphics'), dict) else {}
        r = by_key.get(name)
        if r is None:
            if name not in cmap:
                skipped.append(f'{name} ({srcs[0][0]})')
                continue
            nm = c.get('name', {})
            nm = nm.get('singular') if isinstance(nm, dict) else nm
            r = by_key[name] = {'id': cmap[name], 'name': nm if isinstance(nm, str) and nm else humanize(name),
                                'faction': '', 'level': 0, 'upgrades': [], 'map_def': '', 'png': '', 'notes': '',
                                '_mask': None, '_src': None}
        elif 'faction' in c and c['faction'] != r['faction']:
            r['notes'] = f'HotA moves it to {c["faction"]} (SoD: {r["faction"]})'
        r['faction'] = c.get('faction', r['faction'])
        r['level'] = c.get('level', r['level'])
        r['upgrades'] = list(c.get('upgrades', r['upgrades']))
        if g.get('map') and map_src:
            d, png, _o = hota.sprite(g['map'], map_src[0])
            if r['map_def'] and r['_src'] is None:
                r['notes'] = (r['notes'] + '; ' if r['notes'] else '') + (
                    'HotA-patched map sprite (same DEF name)' if d == r['map_def'] else f'HotA map sprite (SoD {r["map_def"]})')
            r['map_def'], r['png'] = d, png or r['png']
            r['_mask'], r['_src'], r['_ref'] = g.get('mapMask'), f'hota:{map_src[0]}/{map_src[1]}', g['map']
    # The HotA maps say which DEF each monster id really uses (VCMI's config swaps e.g. Stormbird/Ayssid).
    for r in by_key.values():
        d2 = hota.maps.creature_def(r['id'])
        if d2 and r['map_def'] and d2 != r['map_def']:
            hit = hota.fs.find_sprite_by_stem(d2)
            if hit:
                hota.used_sprites.add(hit)
                r['notes'] = (r['notes'] + '; ' if r['notes'] else '') +                     f'HotA maps use {d2} for this id (VCMI config: {r["map_def"]})'
                r['map_def'], r['png'], r['_ref'] = d2, hota.conv.convert(hit), hit[2]
                r['_src'] = r['_src'] or f'hota:{hit[0]}/(HotA maps)'
                hota.creature_fixes.append((r['id'], r['name'], d2))
    upgraded = {x for r in by_key.values() for x in r['upgrades']}
    rows, extra_defs = [], []
    for key, r in by_key.items():
        r['upgraded'] = 1 if key in upgraded else 0
        if 145 <= r['id'] <= 150:
            r['notes'] = (r['notes'] + '; ' if r['notes'] else '') + 'war machine / tower: never a map monster'
        elif not r['map_def']:
            r['notes'] = (r['notes'] + '; ' if r['notes'] else '') + 'no adventure-map sprite'
        if r['_src'] and not 145 <= r['id'] <= 150:
            tm = inherit({'animation': r['_ref'], **({'mask': r['_mask']} if r['_mask'] else {})},
                         sod.core_template_base(54))
            row = hota.row(tm, 54, r['id'], r['_src'], r['_src'][5:].split('/config/')[0], r['name'],
                           '' if r['_mask'] else 'mask from VCMI core object base')
            row['def'], row['png'] = r['map_def'], r['png']
            old = sod54.get(r['id'])
            if not old or old['def'] != row['def']:
                extra_defs.append(row)
        rows.append(r)
    rows.sort(key=lambda r: r['id'])
    return rows, extra_defs, skipped


def sod_hero_png(sod, d):
    """SoD hero DEF preview: the standing-east frame if the unpack wrote frames, else the first frame."""
    path = sod.def_path.get(d)
    if path:
        frame = sod.h3 / Path(path).parent / Path(path).stem / '02_stand_E' / '000.png'
        if frame.exists():
            return rel(frame, sod.repo)
    return sod.png.get(d, '')


def build_heroclasses(sod, hota):
    """ids: SoD = vcmi/config/heroClasses.json index; HotA = mapSupport heroClasses (VCMI key -> h3m id)."""
    classes = collections.OrderedDict()
    for key, c in hota.fs.load(sod.cfg / 'heroClasses.json').items():
        if isinstance(c, dict) and 'index' in c:
            i = c['index']
            classes[key] = {'id': i, 'name': sod.class_names[i] if i < len(sod.class_names) else humanize(key),
                            'faction': c.get('faction', ''), 'source': 'vcmi:config/heroClasses.json',
                            'templates': {g: (t['animation'], None) for g, t in c['mapObject']['templates'].items()}}
    hmap = hota.fmt.get('heroClasses', {})
    for name, (c, srcs, _is_core, tm_src) in hota.merged_entries('heroClasses', 'mapObject/templates').items():
        if name not in classes:
            if name not in hmap:
                continue
            nm = c.get('name')
            classes[name] = {'id': hmap[name], 'name': nm if isinstance(nm, str) else humanize(name),
                             'faction': c.get('faction', ''), 'source': f'hota:{srcs[0][0]}/{srcs[0][1]}',
                             'templates': {}}
        elif tm_src:
            classes[name]['source'] += f' + hota:{tm_src[0]}/{tm_src[1]}'
        tm = c.get('mapObject', {}).get('templates', {}) if isinstance(c.get('mapObject'), dict) else {}
        for g, t in tm.items():
            if isinstance(t, dict) and t.get('animation'):
                classes[name]['templates'][g] = (t['animation'], tm_src[0] if tm_src else srcs[0][0])
    rows = []
    for c in classes.values():
        variants = []
        for g in ('male', 'female', 'default'):
            if g not in c['templates']:
                continue
            ref, sub = c['templates'][g]
            if sub is not None and '/' in ref.replace('\\', '/'):
                d, png, _o = hota.sprite(ref, sub)
            else:
                d, png = stem(ref), sod_hero_png(sod, stem(ref))
            if all(v[1] != d for v in variants):
                variants.append((g, d, png))
        rows.append({'id': c['id'], 'name': c['name'], 'faction': c['faction'], 'source': c['source'],
                     'gender_models': ';'.join(v[0] for v in variants),
                     'map_defs': ';'.join(v[1] for v in variants),
                     'png': ';'.join(v[2] for v in variants)})
    rows.sort(key=lambda r: r['id'])
    return rows


def build_factions(sod, hota):
    """factions.tsv rows + the town templates (class 98) that are not in OBJECTS.TXT.

    ids: SoD = vcmi/config/factions index; HotA = mapSupport factions (cove 9, factory 10, bulwark 11)."""
    fmap = hota.fmt.get('factions', {})
    facs = collections.OrderedDict()
    for key in SOD_FACTIONS:
        data = hota.fs.load(sod.cfg / 'factions' / f'{key}.json').get(key, {})
        town = data.get('town', {}) if isinstance(data.get('town'), dict) else {}
        tm = town.get('mapObject', {}).get('templates', {})
        idx = data.get('index', -1)
        facs[key] = {'id': idx, 'name': sod.towns[idx],
                     'templates': {k: (v['animation'], f'vcmi:config/factions/{key}.json', None)
                                   for k, v in tm.items() if isinstance(v, dict) and v.get('animation')}}
    for name, (c, srcs, _is_core, tm_src) in hota.merged_entries('factions', 'town/mapObject/templates').items():
        if name not in facs:
            if name not in fmap:
                continue
            nm = c.get('name')
            facs[name] = {'id': fmap[name], 'name': nm if isinstance(nm, str) else humanize(name), 'templates': {}}
        town = c.get('town', {}) if isinstance(c.get('town'), dict) else {}
        tm = town.get('mapObject', {}).get('templates', {}) if isinstance(town.get('mapObject'), dict) else {}
        for k, v in tm.items():
            if isinstance(v, dict) and v.get('animation') and tm_src:
                facs[name]['templates'][k] = (v['animation'], f'hota:{tm_src[0]}/{tm_src[1]}', tm_src[0])
    town_base = sod.core_template_base(98)
    sod_rows = {(r['def'], r['cls'], r['sub']): r for r in sod.objects_txt() if r['cls'] == 98}
    rows, defs_rows, seen = [], [], set()
    for key, f in facs.items():
        cls, sub = 98, f['id']
        out = {'id': f['id'], 'name': f['name'], 'key': key}
        notes = []
        for lvl in TOWN_LEVELS:
            t = f['templates'].get(lvl)
            if not t:
                continue
            ref, src, hsub = t
            if hsub is not None and '/' in ref.replace('\\', '/'):
                d, png, _o = hota.sprite(ref, hsub)
            else:
                d, png = stem(ref), sod.png.get(stem(ref), '')
            out[f'{lvl}_def'], out[f'_{lvl}_png'] = d, png
            sod_row = sod_rows.get((d, cls, sub))
            if sod_row:
                mask = sod_row['_mask']
                if hsub is not None:
                    notes.append(f'{d}: HotA redraw of the SoD DEF')
            else:
                note = 'mask from VCMI core town base'
                if hsub is None:
                    note += '; not in OBJECTS.TXT, swapped in by the engine'
                r = hota.row(inherit({'animation': ref}, town_base), cls, sub, src, hsub, f'{f["name"]} ({lvl})', note)
                r['def'], r['png'] = d, png
                mask = r['_mask']
                if (d, cls, sub) not in seen:
                    seen.add((d, cls, sub))
                    defs_rows.append(r)
            if lvl == 'castle' or 'w' not in out:
                out['w'], out['h'] = mask.footprint()
        sod_castle = next((r['def'] for r in sod_rows.values() if r['cls'] == cls and r['sub'] == sub), '')
        if sod_castle and sod_castle not in (out.get('fort_def'), out.get('citadel_def')):
            notes.append(f'SoD shows {sod_castle} for fort/citadel/castle')
        out['png'] = out.get('_castle_png', '')
        out['notes'] = '; '.join(dict.fromkeys(notes))
        rows.append(out)
    rows.sort(key=lambda r: r['id'])
    return rows, defs_rows


# --------------------------------------------------------------------------- contact sheets

class Sheets:
    COLS, ROWS, CELL, LABEL = 6, 6, 330, 46

    def __init__(self, out_dir, repo):
        from PIL import ImageFont
        self.out, self.repo = Path(out_dir), repo
        self.out.mkdir(parents=True, exist_ok=True)
        self.font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 13)
        self.index = []

    def series(self, name, items):
        """items: [(key, png repo path, [label lines])]; writes <name>_NN.png, 36 per sheet."""
        from PIL import Image, ImageDraw
        items = [it for it in items if it[1]]
        per = self.COLS * self.ROWS
        for n in range(0, len(items), per):
            chunk = items[n:n + per]
            rows = (len(chunk) + self.COLS - 1) // self.COLS
            sheet = Image.new('RGB', (self.COLS * self.CELL, rows * self.CELL), (128, 128, 128))
            draw = ImageDraw.Draw(sheet)
            fname = f'{name}_{n // per + 1:02d}.png'
            for i, (key, png, label) in enumerate(chunk):
                x0, y0 = (i % self.COLS) * self.CELL, (i // self.COLS) * self.CELL
                draw.rectangle([x0, y0, x0 + self.CELL - 1, y0 + self.CELL - 1], outline=(104, 104, 104))
                try:
                    im = Image.open(self.repo / png).convert('RGBA')
                    bbox = im.getchannel('A').getbbox()
                    if bbox:
                        im = im.crop(bbox)
                    aw, ah = self.CELL - 8, self.CELL - self.LABEL - 8
                    s = min(2.0, aw / im.width, ah / im.height)
                    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.NEAREST)
                    px = x0 + (self.CELL - im.width) // 2
                    py = y0 + 4 + (ah - im.height) // 2 + 0
                    sheet.paste(im, (px, py), im)
                except Exception as e:
                    label = label + [f'ERR {type(e).__name__}']
                for j, line in enumerate(label[:3]):
                    while line and draw.textlength(line, font=self.font) > self.CELL - 6:
                        line = line[:-1]
                    draw.text((x0 + 3, y0 + self.CELL - self.LABEL + 1 + j * 15), line, fill=(255, 255, 255) if j == 0 else (230, 230, 200), font=self.font)
                self.index.append({'sheet': fname, 'cell': i, 'key': key, 'label': ' | '.join(label), 'png': png})
            sheet.save(self.out / fname)

    def write_index(self):
        write_tsv(self.out / 'index.tsv', ['sheet', 'cell', 'key', 'label', 'png'], self.index)


def make_sheets(work, repo, defs, creatures, heroes, factions):
    sh = Sheets(work / 'sheets', repo)

    def short(r):
        return (r['subtype_name'] or r['class_name'])[:40]

    vis = [r for r in defs if r['group'] == 'visitable' and r['class'] not in (54, 98)]
    vis.sort(key=lambda r: (r['source'] != 'objects.txt', r['class'], r['subtype'], r['def']))
    sh.series('visitable', [(r['def'], r['png'], [r['def'], f"{r['class']}:{r['subtype']} {r['class_name']}", short(r)]) for r in vis])
    dec = [r for r in defs if r['group'] == 'decoration']
    dec.sort(key=lambda r: (r['class'], r['source'] != 'objects.txt', r['subtype'], r['def']))
    sh.series('decorations', [(r['def'], r['png'], [r['def'], f"{r['class']}:{r['subtype']} {r['class_name']}", short(r)]) for r in dec])
    oth = [r for r in defs if r['group'] == 'other' and r['class'] not in PLACEHOLDER_CLASSES]
    oth.sort(key=lambda r: (r['class'], r['source'] != 'objects.txt', r['subtype'], r['def']))
    sh.series('other', [(r['def'], r['png'], [r['def'], f"{r['class']}:{r['subtype']} {r['class_name']}", short(r)]) for r in oth])
    order = {f: i for i, f in enumerate(SOD_FACTIONS + ['cove', 'factory', 'bulwark', 'neutral'])}
    cr = sorted(creatures, key=lambda r: (order.get(r['faction'], 99), r['id']))
    sh.series('creatures', [(str(r['id']), r['png'], [f"{r['id']} {r['name']}", f"{r['faction']} L{r['level']}", r['map_def']]) for r in cr])
    hv = []
    for r in heroes:
        for g, d, p in zip(r['gender_models'].split(';'), r['map_defs'].split(';'), r['png'].split(';')):
            hv.append((f"{r['id']}:{g}", p, [f"{r['id']} {r['name']}", f"{r['faction']} {g}", d]))
    sh.series('heroclasses', hv)
    tv = []
    for r in factions:
        for lvl in TOWN_LEVELS:
            if r.get(f'{lvl}_def'):
                tv.append((f"{r['key']}:{lvl}", r.get(f'_{lvl}_png', ''), [r[f'{lvl}_def'], f"{r['id']} {r['name']}", lvl]))
    sh.series('towns', tv)
    sh.write_index()
    return collections.Counter(i['sheet'].rsplit('_', 1)[0] for i in sh.index), len({i['sheet'] for i in sh.index})


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--repo', type=Path, default=REPO_DEFAULT)
    ap.add_argument('--hota', type=Path, help='VCMI hota mod dir (default <repo>/vcmi/userdata/Mods/hota)')
    ap.add_argument('--vcmi-config', type=Path, help='default <repo>/vcmi/config')
    ap.add_argument('--h3', type=Path, help='unpacked SoD resources (default <repo>/assets/h3-original)')
    ap.add_argument('--work', type=Path, help='default <repo>/_work/assets3d_src')
    ap.add_argument('--out', type=Path, help='default <repo>/godot/assets3d/pipeline/sources')
    ap.add_argument('--no-sheets', action='store_true')
    a = ap.parse_args()
    repo = a.repo.resolve()
    hota_dir = (a.hota or repo / 'vcmi/userdata/Mods/hota').resolve()
    cfg = (a.vcmi_config or repo / 'vcmi/config').resolve()
    h3 = (a.h3 or repo / 'assets/h3-original').resolve()
    work = (a.work or repo / '_work/assets3d_src').resolve()
    out = (a.out or repo / 'godot/assets3d/pipeline/sources').resolve()
    for sub in ('hota_cfg', 'hota_defs', 'hota_png') + (() if a.no_sheets else ('sheets',)):   # start clean
        shutil.rmtree(work / sub, ignore_errors=True)
    (work / 'hota_png').mkdir(parents=True, exist_ok=True)

    u = load_tools(repo)
    sod = SoD(h3, cfg, u, repo)
    fs = ModFS(hota_dir, work, u)
    conv = SpriteConverter(fs, work / 'hota_png', repo)
    maps = MapTemplates(fs)
    hota = HotA(fs, sod, conv, maps)
    print(f'HotA mod: {len(fs.submods)} submods, {len(fs.files)} zip members ({fs.collisions} paths in several zips)')

    sod_rows = sod.objects_txt()
    checks = verify_sod_masks(sod_rows)
    creatures, creature_defs, cr_skipped = build_creatures(sod, hota, sod_rows)
    factions, town_defs = build_factions(sod, hota)
    defs, stats, id_mismatch = build_defs(sod, hota, creature_defs, town_defs)
    heroes = build_heroclasses(sod, hota)

    write_tsv(out / 'defs.tsv', ['def', 'class', 'subtype', 'class_name', 'subtype_name', 'w', 'h', 'block', 'visit',
                                 'land', 'source', 'png', 'group', 'terrains', 'notes'], defs)
    write_tsv(out / 'creatures.tsv', ['id', 'name', 'faction', 'level', 'upgraded', 'map_def', 'png', 'notes'], creatures)
    write_tsv(out / 'heroclasses.tsv', ['id', 'name', 'faction', 'gender_models', 'map_defs', 'png', 'source'], heroes)
    write_tsv(out / 'factions.tsv', ['id', 'name', 'key', 'village_def', 'fort_def', 'citadel_def', 'castle_def',
                                     'capitol_def', 'w', 'h', 'png', 'notes'], factions)

    # map-object-looking DEFs in the zips that no template references (HotA-new names only)
    used = {stem(h[2]) for h in hota.used_sprites}
    unref = sorted({stem(m) for m in fs.files if m.endswith('.def') and stem(m) not in used
                    and stem(m).startswith(('av', 'ab')) and stem(m) not in sod.def_path
                    and not any(k in m for k in ('/battle/', 'attak'))})

    # ----- summary
    n_sod = sum(r['source'] == 'objects.txt' for r in defs)
    print('\nOBJECTS.TXT orientation checks:')
    print('\n'.join(checks))
    print(f'\nrows: defs.tsv {len(defs)} (SoD {n_sod}, HotA {len(defs) - n_sod}; {stats["hota_dup"]} HotA templates '
          f'equal to an OBJECTS.TXT row skipped), creatures.tsv {len(creatures)}, heroclasses.tsv {len(heroes)}, '
          f'factions.tsv {len(factions)}')
    print('groups:', dict(sorted(collections.Counter(r['group'] for r in defs).items())))
    per_class = collections.Counter((r['class'], r['source'] == 'objects.txt') for r in defs)
    print('rows per class (class:SoD+HotA):')
    items = [f'{c}:{per_class[(c, True)]}+{per_class[(c, False)]}' for c in sorted({c for c, _ in per_class})]
    for i in range(0, len(items), 14):
        print('  ' + ' '.join(items[i:i + 14]))
    print(f'templates only known from the HotA maps (added, source hota:maps): {stats["maps_only"]} '
          f'{[r["def"] for r in defs if r["source"] == "hota:maps"]}')
    print(f'HotA templates: {len(defs) - n_sod - stats["maps_only"] + stats["hota_dup"]} mapped to h3m class:subtype '
          f'({len(hota.via_maps)} of them only via the HotA maps), {len(hota.unmapped)} unmapped (left out):')
    for o, t, d, src in hota.unmapped:
        print(f'  unmapped {o}/{t}: {d}  ({src})')
    for o, t, d, ids in hota.via_maps:
        print(f'  via maps {o}/{t}: {d} -> {ids[0]}:{ids[1]}')
    print(f'HotA template ids replaced by the id the HotA maps use: {len(hota.id_overrides)}')
    for d, old, new in hota.id_overrides:
        print(f'  {d}: {old[0]}:{old[1]} -> {new[0]}:{new[1]}')
    for cid, name, d in hota.creature_fixes:
        print(f'  creature {cid} {name}: map DEF {d} (from the HotA maps)')
    print(f'HotA maps cross-check ({maps.maps} maps/campaigns, {len(maps.t)} DEFs): in maps SoD {stats["sod_in_maps"]}, '
          f'HotA {stats["hota_in_maps"]}; class:subtype disagrees {len(id_mismatch)}; mask differs SoD '
          f'{stats["sod_mask_differs"]}, HotA {stats["hota_mask_differs"]}; empty mask filled from maps '
          f'{stats["sod_mask_from_maps"] + stats["hota_mask_from_maps"]}')
    for d, ours, ids in id_mismatch:
        print(f'  id differs: {d} {ours[0]}:{ours[1]} vs maps {", ".join(f"{c}:{s}" for c, s in ids)}')
    no_png = [r['def'] for r in defs if not r['png'] and r['group'] != 'other']
    print(f'defs without preview png (excluding group other): {len(no_png)} {no_png[:40]}')
    no_mask = [r['def'] for r in defs if 'no mask' in r['notes'] and 'mask from the HotA maps' not in r['notes']]
    if no_mask:
        print(f'HotA templates without any mask: {no_mask}')
    print(f'creatures without map png: {[r["id"] for r in creatures if not r["png"]]}')
    if cr_skipped:
        print(f'HotA creature configs without h3m id (not in creatures.tsv): {cr_skipped}')
    if hota.missing_sprites:
        print(f'sprites not found: {len(hota.missing_sprites)} {hota.missing_sprites[:20]}')
    if conv.errors:
        print(f'conversion errors: {len(conv.errors)}')
        for e in conv.errors[:20]:
            print('  ' + e)
    if hota.bad_mask_chars:
        print(f'unknown mask chars: {dict(hota.bad_mask_chars)}')
    print(f'converted HotA sprites: {sum(1 for v in conv.done.values() if v)} -> {rel(work / "hota_png", repo)}')
    print(f'unreferenced HotA av*/ab* DEFs in the zips: {len(unref)} {unref}')
    if not a.no_sheets:
        per, nsheets = make_sheets(work, repo, defs, creatures, heroes, factions)
        print(f'sheets: {nsheets} -> {rel(work / "sheets", repo)} items {dict(per)}')


if __name__ == '__main__':
    main()
