// Bridge part 1 of the Frida agent: reading the engine state (PLAN 1.3). bridge.py loads
// game_agent.js + bridge_agent.js + bridge_runtime.js as one script (launch.Game(..., extra=...)), so this file
// uses game_agent.js helpers (A, G, p32, cstr, players, heroPool, heroCount, runOnGameThread, log).
//
//   snapshot()  - map tiles, fog, objects (id/type/subtype/def/anchor/masks/owner), heroes, towns, players, date
//   dynState()  - the dynamic part; bridge.py diffs it into PROTOCOL `state`
//
// Offsets: ENGINE_API.md (tags [R] = checked at runtime on HotA 1.8.0).

const E = {
    giCurPlayer: 0x69CCF4, gpCurPlayer: 0x69CCFC, giCurWatchPlayer: 0x6977DC, mapExtra: 0x698A48,
    MAP_WIDTH: 0x6783C8, MAP_HEIGHT: 0x6783CC, gbGameOver: 0x697308, gbInCombat: 0x699590,
    giDialogTimeout: 0x6977D4, dialogDepth: 0x6AAD88, gpSearchArray: 0x6992D4, bShowIt: 0x698A10,
    // functions
    advMain: 0x408710, ProcessDeSelect: 0x4099D0, SetHeroContext: 0x417A80, ShowRoute: 0x418D30,
    DoAdvCommand: 0x407AE0, DoEvent: 0x4AA710, MoveHero: 0x47FF00, NextPlayer: 0x4C6CA0, DoCombat: 0x4AD160,
    NormalDialog: 0x4F6C00, ExtendedDialog: 0x4F7D20, DoDialog: 0x602AE0, CallManager: 0x4B09D0,
    TownView: 0x5BE610, GetFlaggedObjectOwner: 0x40FC80, hasFlag: 0x40FC00, FindTrigger: 0x4FF280,
    GetHero: 0x4317D0, Delay: 0x4F8A50, DelayTil: 0x4F8980, Process1WindowsMessage: 0x4F8640,
    EraseObj: 0x4AA820,
};
const MAPOFF = 0x1fb70;   // game.worldMap (NewfullMap)
const CELL = 0x26;
const ADV = { map: 0x5c, advCommand: 0x40, pRouteArray: 0x48, seedingValid: 0x50 };
const HERO = { x: 0, y: 2, z: 4, valid: 6, mana: 0x18, id: 0x1a, owner: 0x22, name: 0x23, cls: 0x30, portrait: 0x34,
    targetX: 0x35, targetY: 0x39, targetZ: 0x3d, facing: 0x47, mpMax: 0x49, mp: 0x4d, exp: 0x51, level: 0x55,
    armyT: 0x91, armyN: 0xad, flags: 0x105, primary: 0x476 };
const COLORS = ['red', 'blue', 'tan', 'green', 'orange', 'purple', 'teal', 'pink'];

const fnGetFlaggedOwner = new NativeFunction(ptr(E.GetFlaggedObjectOwner), 'int', ['pointer'], 'fastcall');
const fnFindTrigger = new NativeFunction(ptr(E.FindTrigger), 'int', ['pointer', 'pointer', 'pointer'], 'thiscall');
const fnGetHero = new NativeFunction(ptr(E.GetHero), 'pointer', ['pointer', 'int'], 'thiscall');

function game() { return p32(A.gpGame); }
function mapInfo() {
    const g = game();
    return { size: g.add(MAPOFF + 0xd4).readS32(), levels: g.add(MAPOFF + 0xd8).readU8() ? 2 : 1,
        cells: g.add(MAPOFF + 0xd0).readPointer() };
}
function vec(p, elem) {  // exe_vector: +4 first, +8 last
    const first = p.add(4).readPointer(), last = p.add(8).readPointer();
    return { first: first, n: first.isNull() ? 0 : last.sub(first).toInt32() / elem };
}
function hex48(lo, hi) {  // 6 mask bytes -> 12 hex digits of the 48-bit number (bit i = 8*row + col)
    return ('0000' + hi.toString(16)).slice(-4) + ('00000000' + (lo >>> 0).toString(16)).slice(-8);
}
// The local human whose view the client shows. Single player: the only human. Hot-seat (several local humans): the
// player the engine shows the map to (giCurWatchPlayer, the human whose turn it is); during AI turns it keeps the
// last human, so the view does not jump.
let meLast = -1;
function me() {
    const g = game();
    const localHuman = i => {
        const p = g.add(G.player + i * G.playerSize);
        return p.add(0xe2).readU8() !== 0 && p.add(0xe1).readU8() !== 0;
    };
    const w = ptr(E.giCurWatchPlayer).readS32();
    if (w >= 0 && w < 8 && localHuman(w)) return (meLast = w);
    if (meLast >= 0 && meLast < 8 && localHuman(meLast)) return meLast;
    for (let i = 0; i < 8; i++) if (localHuman(i)) return i;
    return 0;
}

// ---------- object types / objects ----------
let typeCache = null;   // ObjectTypes do not change during a game; rebuilt when the game pointer changes
function objectTypes() {
    const g = game();
    if (typeCache && typeCache.g.equals(g)) return typeCache.list;
    const v = vec(g.add(MAPOFF), 0x44);
    const list = [];
    for (let i = 0; i < v.n; i++) {
        const t = v.first.add(i * 0x44);
        const sp = t.add(4).readPointer();
        let def = sp.isNull() ? '' : sp.readCString();
        if (/\.def$/i.test(def)) def = def.slice(0, -4);
        const pass = new Uint8Array(t.add(0x1c).readByteArray(6)), trig = new Uint8Array(t.add(0x2c).readByteArray(6));
        // passable mask 1 = passable -> block = inverted
        const bl = [], vi = [];
        for (let k = 0; k < 6; k++) { bl.push(~pass[k] & 0xff); vi.push(trig[k]); }
        const lo = a => (a[0] | (a[1] << 8) | (a[2] << 16) | (a[3] << 24)) >>> 0, hi = a => a[4] | (a[5] << 8);
        list.push({ def: def, w: t.add(0x10).readS8(), h: t.add(0x11).readS8(), type: t.add(0x38).readS32(),
            subtype: t.add(0x3c).readS32(), underlay: t.add(0x40).readU8(),
            block: hex48(lo(bl), hi(bl)), visit: hex48(lo(vi), hi(vi)), passRaw: Array.from(pass), trigRaw: Array.from(trig) });
    }
    typeCache = { g: g, list: list };
    return list;
}

const FLAGGED = { 17: 1, 20: 1, 33: 1, 42: 1, 53: 1, 77: 1, 87: 1, 98: 1 };
// HotA keeps wandering monsters in its own table: the trigger cell setup of a monster (54) is an index, the
// count is int at  *(*(gpGame + [HotA.dll+0x634AC8]) + 0x1C) + setup*0x24  (HotA hook_004170DA, MonsterQuickView).
let hotaModule = null;   // looked up lazily: at spawn time HotA.dll is not loaded yet
function hotaDll() { return hotaModule || (hotaModule = Process.findModuleByName('HotA.dll')); }
function monsterCount(setup) {
    try {
        const off = hotaDll().base.add(0x634ac8).readU32();
        const arr = game().add(off).readPointer().add(0x1c).readPointer();
        return arr.add((setup & 0xffff) * 0x24).readS32();
    } catch (e) { return -1; }
}
// Every object id that is still on some tile's ObjectCellList (EraseObj does not shrink Objects).
function liveObjectIds(mi, raw) {
    const ids = new Set();
    const n = mi.size * mi.size * mi.levels;
    const dv = new DataView(raw);
    for (let i = 0; i < n; i++) {
        const o = i * CELL;
        const first = dv.getUint32(o + 0x12, true), last = dv.getUint32(o + 0x16, true);
        if (!first || last <= first) continue;
        const cnt = (last - first) >>> 2;
        const ents = new DataView(ptr(first).readByteArray(cnt * 4));
        for (let k = 0; k < cnt; k++) ids.add(ents.getUint16(k * 4, true));
    }
    return ids;
}
function readObjects(mi, raw) {
    const g = game();
    const types = objectTypes();
    const v = vec(g.add(MAPOFF + 0x10), 0xc);
    const ids = liveObjectIds(mi, raw);
    const objs = new DataView(v.first.readByteArray(v.n * 0xc));
    const out = [];
    const tx = Memory.alloc(8);
    for (const id of Array.from(ids).sort((a, b) => a - b)) {
        if (id >= v.n) continue;
        const o = id * 0xc;
        const x = objs.getUint8(o + 4), y = objs.getUint8(o + 5), z = objs.getUint8(o + 6), ti = objs.getUint16(o + 8, true);
        const t = types[ti];
        if (!t) continue;
        const r = { id: id, type: t.type, subtype: t.subtype, def: t.def, x: x, y: y, z: z, block: t.block, visit: t.visit,
            owner: -1 };
        if (t.underlay) r.underlay = 1;
        // trigger tile: live per-object data (setup) and owner
        if (t.visit !== '000000000000') {
            tx.writeS32(-1); tx.add(4).writeS32(-1);
            fnFindTrigger(v.first.add(o), tx, tx.add(4));
            const trX = tx.readS32(), trY = tx.add(4).readS32();
            if (trX >= 0 && trY >= 0 && trX < mi.size && trY < mi.size) {
                const ci = (z * mi.size + trY) * mi.size + trX;
                const cell = mi.cells.add(ci * CELL);
                const setup = cell.readU32();
                if (FLAGGED[t.type]) r.owner = fnGetFlaggedOwner(cell);
                if (t.type === 54) r.count = monsterCount(setup);
                else if (t.type === 79) r.count = setup & 0x7ffff;
                if (t.type === 98 || t.type === 77) r.town = setup | 0;
                r.trigger = [trX, trY, z];
            }
        }
        out.push(r);
    }
    return out;
}

// ---------- tiles / fog ----------
function readTiles(mi, raw) {
    const dv = new Uint8Array(raw);
    const N = mi.size * mi.size;
    const terrain = [], frame = [], river = [], road = [], tflags = [];
    for (let z = 0; z < mi.levels; z++) {
        const t = new Array(N), f = new Array(N), rv = new Array(N), rd = new Array(N), fl = new Array(N);
        for (let i = 0; i < N; i++) {
            const o = (z * N + i) * CELL;
            t[i] = dv[o + 4]; f[i] = dv[o + 5]; rv[i] = dv[o + 6] & 7; rd[i] = dv[o + 8];
            const mirror = dv[o + 0xc], acc = dv[o + 0xd];
            // bit0 mirrorX, bit1 mirrorY, bit2 coast, bit3 blocked, bit4 visitable (PROTOCOL.md)
            fl[i] = (mirror & 3) | ((acc & 2) ? 4 : 0) | ((acc & 1) ? 8 : 0) | ((acc & 0x10) ? 16 : 0);
        }
        terrain.push(t); frame.push(f); river.push(rv); road.push(rd); tflags.push(fl);
    }
    return { size: mi.size, levels: mi.levels, terrain: terrain, terrain_frame: frame, river: river, road: road, tflags: tflags };
}
function readFog(mi, who) {
    const ex = ptr(E.mapExtra).readPointer();
    const N = mi.size * mi.size;
    const bits = new Uint16Array(ex.readByteArray(N * mi.levels * 2));
    const out = [];
    for (let z = 0; z < mi.levels; z++) {
        const f = new Array(N);
        for (let i = 0; i < N; i++) f[i] = (bits[z * N + i] >> who) & 1;
        out.push(f);
    }
    return out;
}

// ---------- heroes / towns / players / date ----------
function heroObj(h, mi, fog) {
    const army = [];
    for (let k = 0; k < 7; k++) {
        const c = h.add(HERO.armyT + 4 * k).readS32();
        army.push(c < 0 ? -1 : [c, h.add(HERO.armyN + 4 * k).readS32()]);
    }
    const x = h.readS16(), y = h.add(2).readS16(), z = h.add(4).readS16();
    const onMap = x >= 0 && y >= 0 && x < mi.size && y < mi.size && z >= 0 && z < mi.levels;
    const flags = h.add(HERO.flags).readU32();
    return { id: h.add(HERO.id).readS32(), name: cstr(h.add(HERO.name), 13), owner: h.add(HERO.owner).readS8(),
        class: h.add(HERO.cls).readS32(), portrait: h.add(HERO.portrait).readU8(), x: x, y: y, z: z,
        dir: h.add(HERO.facing).readU8(), mp: h.add(HERO.mp).readS32(), mp_max: h.add(HERO.mpMax).readS32(),
        mana: h.add(HERO.mana).readS16(), level: h.add(HERO.level).readS16(), exp: h.add(HERO.exp).readS32(),
        primary: [0, 1, 2, 3].map(k => h.add(HERO.primary + k).readS8()), army: army,
        visible: onMap && fog !== null ? fog[z][y * mi.size + x] === 1 : false,
        boat: (flags & 0x40000) !== 0, sleeping: h.add(0x11c).readU8() !== 0 };
}
function readHeroes(mi, fog) {
    const base = heroPool();
    const out = [];
    const ownerOf = {};
    for (const p of players()) for (const id of p.heroes) if (id >= 0) ownerOf[id] = p.id;
    for (let i = 0, n = heroCount(); i < n; i++) {
        const h = base.add(i * G.heroSize);
        const owner = h.add(HERO.owner).readS8();
        if (owner < 0 || ownerOf[i] === undefined) continue;   // only heroes in some player's hero list
        out.push(heroObj(h, mi, fog));
    }
    return out;
}
function readTowns(mi) {
    const g = game();
    const v = vec(g.add(0x21610), 0x168);
    const out = [];
    for (let i = 0; i < v.n; i++) {
        const t = v.first.add(i * 0x168);
        const built = t.add(0x158).readU64();
        const bit = b => !built.shr(b).and(1).equals(0);
        const garrison = [];
        for (let k = 0; k < 7; k++) {
            const c = t.add(0xe0 + 4 * k).readS32();
            garrison.push(c < 0 ? -1 : [c, t.add(0xfc + 4 * k).readS32()]);
        }
        const np = t.add(0xc8).readPointer();
        out.push({ id: t.readS8(), name: np.isNull() ? '' : cstr(np, 32), owner: t.add(1).readS8(), faction: t.add(4).readS8(),
            x: t.add(5).readU8(), y: t.add(6).readU8(), z: t.add(7).readU8(),
            fort: bit(9) ? 3 : bit(8) ? 2 : bit(7) ? 1 : 0, capitol: bit(13),
            visiting: t.add(0x10).readS32(), garrison_hero: t.add(0xc).readS32(), garrison: garrison,
            built: built.toString(16) });
    }
    return out;
}
function readPlayers() {
    return players().map(p => ({ id: p.id, color: COLORS[p.id], human: p.human, alive: p.alive && (p.numHeroes > 0 || p.numTowns > 0),
        res: p.res, heroes: p.heroes, towns: p.numTowns }));
}
function readDate() {
    const g = game();
    return { day: g.add(G.day).readU16(), week: g.add(G.week).readU16(), month: g.add(G.month).readU16() };
}
function curPlayer() { return ptr(E.giCurPlayer).readS32(); }
// the hero selected in the engine (playerData.currHero of the current player) when it is `who`'s turn, else -1
function selectedHero(who) {
    const cp = ptr(E.gpCurPlayer).readPointer();
    if (cp.isNull() || curPlayer() !== who) return -1;
    return cp.add(4).readS32();
}

function snapshot() {
    const mi = mapInfo();
    const raw = mi.cells.readByteArray(mi.size * mi.size * mi.levels * CELL);
    const who = me();
    const fog = readFog(mi, who);
    return { t: 'snapshot', map: readTiles(mi, raw), fog: fog, objects: readObjects(mi, raw),
        heroes: readHeroes(mi, fog), towns: readTowns(mi), players: readPlayers(), date: readDate(),
        current: curPlayer(), me: who, selected: selectedHero(who) };
}
// Dynamic part only (objects as a full list; bridge.py diffs it into objects_changed/objects_removed, fog likewise).
function dynState() {
    const mi = mapInfo();
    const raw = mi.cells.readByteArray(mi.size * mi.size * mi.levels * CELL);
    const who = me();
    const fog = readFog(mi, who);
    return { objects: readObjects(mi, raw), fog: fog, heroes: readHeroes(mi, fog), towns: readTowns(mi),
        players: readPlayers(), date: readDate(), current: curPlayer(), me: who, selected: selectedHero(who) };
}

