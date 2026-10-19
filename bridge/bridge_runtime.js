// Bridge part 2 of the Frida agent: hooks and commands (PLAN 1.4-1.7). Appended after game_agent.js and
// bridge_agent.js (uses their helpers). Everything is inactive until rpc bridgeEnable(), so the launcher's menu
// automation keeps the original behaviour.
//
//   commands  path / select_hero / move_hero / end_turn, run from advManager::Main on an idle message, i.e. at the
//             same place and in the same way as the original buttons (ProcessDeSelect 7 / 0xC, ShowRoute)
//   events    hero_move (MoveHero per-step diff, AI too), turn (NextPlayer), battle (DoCombat), window
//   dialogs   NormalDialog / extended_dialog replaced: the question goes to the client, the game thread waits for
//             dialog_answer (or a default when no client / timeout)
//   windows   passthrough (PLAN 1.9): engine windows the client does not draw natively (DoDialog modals, town
//             screen, hero meeting, manual battles) stay open and run as in the original game; the bridge
//             streams the engine frame (rpc frame) and the client's mouse/keys go into the engine's input queue
//             (rpc windowInput). Without a passthrough-capable client: the old policies (level-up -> `choose`,
//             combat results -> OK, others skipped with cancel, town screen opened and closed at once)
//
// Messages to Python (bridge.py): {t:'bridge', m:<protocol message>} goes to the clients as is;
// {t:'state_raw', s:<dynState>} is diffed into `state`; {t:'cmd_done', id, result, error};
// {t:'frame', w, h, pitch, hash} + RGB565 bytes (answer to rpc frame).

const BR = {
    on: false, client: false, dialogTimeout: 120, defaults: {}, busy: false, debugRoute: false,
    dialogSeq: 0, answers: {}, pendingDialog: null, lastPos: {}, moves: {}, moveOrder: [], levelUp: null,
    pass: false,          // a passthrough-capable client is connected (bridge.py: client hello caps "windows")
    battles: 'auto',      // 'manual': the human's battles run on the engine's battle screen (passthrough);
                          // 'game': as the game's own Quick Combat option says (off -> battle screen, on -> quick)
    policy: {},           // window name -> policy override (bridge.py --windows name=policy,...; '*' = unknown)
    manualBattle: false,
    lastMe: -1,           // `me` of the last snapshot/state (hot-seat: a change -> new snapshot)
    phase: MODE === 'spawn' ? 'menu' : null,   // 'menu' | 'adventure' (advManager's executive loop runs), see below
    gen: 0,               // adventure sessions started (a new map / save each time)
    // visible mode (PLAN 4.6, bridge.py --visible): the player also plays in the game window. Engine dialogs and
    // windows run natively there (and go to the client too), animations keep the game's timing, changes the player
    // makes in the game window are found by the mirror tick (state digest) and sent as `state` why "external"
    visible: VISIBLE,
    nativeDlg: null,      // {id, style, by} of the NormalDialog / extended_dialog running natively (visible mode)
    digest: null,         // state digest of the last state sent (mirror tick)
    mirrorMs: 250,        // mirror tick period on the game thread
};
function bsend(m) { send({ t: 'bridge', m: m }); }
function wm() { return p32(A.gpWindowManager); }
function adv() { return p32(A.gpAdvManager); }
function curPlayerData() { return ptr(E.gpCurPlayer).readPointer(); }
function heroPtr(id) { return heroPool().add(id * G.heroSize); }
function point(v) { return [v & 0x3ff, (v >>> 16) & 0x3ff, (v >>> 26) & 0xf]; }

const fnSetHeroContext = new NativeFunction(ptr(E.SetHeroContext), 'void', ['pointer', 'int', 'int', 'int', 'int'], 'thiscall');
const fnShowRoute = new NativeFunction(ptr(E.ShowRoute), 'void', ['pointer', 'int', 'int', 'int'], 'thiscall');
const fnPump = new NativeFunction(ptr(E.Process1WindowsMessage), 'void', [], 'mscdecl');

// ---------- hero movement tracking (any hero, AI included) ----------
function onMapHeroes() {
    const out = [];
    for (const p of players()) for (const id of p.heroes) if (id >= 0) out.push(id);
    return out;
}
function trackPositions(record) {
    for (const id of onMapHeroes()) {
        const h = heroPtr(id);
        const pos = [h.readS16(), h.add(2).readS16(), h.add(4).readS16()];
        const last = BR.lastPos[id];
        if (record && last && (last[0] !== pos[0] || last[1] !== pos[1] || last[2] !== pos[2])) {
            if (!BR.moves[id]) { BR.moves[id] = []; BR.moveOrder.push(id); }
            BR.moves[id].push(pos);
        }
        BR.lastPos[id] = pos;
    }
}
// hero_move events for the moves collected so far; other players' heroes only on tiles the local player sees.
// external: the moves were made by the player in the game window (visible mode), not by a bridge command
function flushMoves(external) {
    if (!BR.on) return;
    trackPositions(true);
    if (!BR.moveOrder.length) return;
    const who = me();
    const mi = mapInfo();
    const fog = readFog(mi, who);
    for (const id of BR.moveOrder) {
        const owner = heroPtr(id).add(HERO.owner).readS8();
        let cells = BR.moves[id];
        if (owner !== who) cells = cells.filter(c => c[2] >= 0 && c[2] < mi.levels && fog[c[2]][c[1] * mi.size + c[0]]);
        const ev = { t: 'event', kind: 'hero_move', hero: id, owner: owner, cells: cells };
        if (external) ev.external = true;
        if (cells.length) bsend(ev);
    }
    BR.moves = {}; BR.moveOrder = [];
}
// full dynamic state -> bridge.py (diffed into `state`); the mirror digest follows what was sent
function sendState(why) {
    send({ t: 'state_raw', s: dynState(), why: why });
    if (BR.visible) {
        try { BR.digest = stateDigest(); } catch (e) { BR.digest = null; }
    }
}
Interceptor.attach(ptr(E.MoveHero), { onLeave() { if (BR.on) trackPositions(true); } });

// ---------- turns ----------
Interceptor.attach(ptr(E.NextPlayer), {
    onEnter() { if (BR.on && BR.phase === 'adventure') flushMoves(); },
    onLeave() {
        if (!BR.on || BR.phase !== 'adventure') return;
        trackPositions(false);
        bsend({ t: 'event', kind: 'turn', current: curPlayer(), date: readDate() });
        // hot-seat: another human's turn -> the client gets that player's view (fog, `me`) as a new snapshot
        const who = me();
        if (BR.lastMe >= 0 && who !== BR.lastMe) {
            log('view of player ' + who + ' (was ' + BR.lastMe + ')');
            BR.lastMe = who;
            send({ t: 'snapshot_raw', s: snapshot(), why: 'player' });
            BR.digest = null;
            return;
        }
        BR.lastMe = who;
        sendState('turn');
    },
});

// ---------- speed: no delays (animations, fades, AI pauses) ----------
// visible mode: the player watches the game window, so heroes walk and screens fade with the game's own timing
const origDelay = new NativeFunction(ptr(E.Delay), 'void', ['int'], 'fastcall');
Interceptor.replace(ptr(E.Delay), new NativeCallback(function (ms) {
    if (!BR.on || BR.phase !== 'adventure' || BR.visible) return origDelay(ms);   // menus, videos, credits: original timing
    fnPump();
}, 'void', ['int'], 'fastcall'));
const origDelayTil = new NativeFunction(ptr(E.DelayTil), 'void', ['uint32'], 'fastcall');
Interceptor.replace(ptr(E.DelayTil), new NativeCallback(function (t) {
    if (!BR.on || BR.phase !== 'adventure' || BR.visible) return origDelayTil(t);
    fnPump();
}, 'void', ['uint32'], 'fastcall'));

// ---------- dialogs ----------
const RES_RU = ['дерево', 'ртуть', 'руда', 'сера', 'кристаллы', 'самоцветы', 'золото'];
const RES_ICON = ['wood', 'mercury', 'ore', 'sulfur', 'crystal', 'gems', 'gold'];
const PRIMARY_RU = ['атака', 'защита', 'сила магии', 'знания'];
// Names from the engine's own tables (cp1251, the Russian HotA): pointer globals from H3API, HotA re-points them
// to its bigger tables. name(tablePtrAddr, elemSize, nameOff, index, count)
function engineName(addr, size, off, i, count) {
    try {
        if (i < 0 || i >= count) return null;
        const p = ptr(addr).readPointer().add(i * size).add(off).readPointer();
        return p.isNull() ? null : cstr(p, 64);
    } catch (e) { return null; }
}
const skillName = i => engineName(0x67DCF0, 0x10, 0, i, 32);        // g_H3SecondarySkillText
const artifactName = i => engineName(0x660B68, 0x20, 0, i, 1024);    // g_H3ArtifactSetup
const spellName = i => engineName(0x687FA8, 0x88, 0x10, i, 128);     // g_H3Spell
const creatureName = (i, n) => engineName(0x6747B0, 0x74, n === 1 ? 0x14 : 0x18, i, 1024);   // g_H3CreatureInformation
const LEVEL_RU = ['', 'базовый', 'продвинутый', 'эксперт'];
// NormalDialog picture (type, subtype) -> {type, sub, icon, text}; types from H3API H3PictureCategories
function describePic(type, sub) {
    if (type < 0) return null;
    const d = { type: type, sub: sub };
    if (type <= 6) { d.icon = RES_ICON[type]; d.text = sub + ' ' + RES_RU[type]; }
    else if (type === 8) { d.icon = 'artifact'; d.text = artifactName(sub) || 'артефакт ' + sub; }
    else if (type === 9) { d.icon = 'spell'; d.text = spellName(sub) || 'заклинание ' + sub; }
    else if (type === 10) { d.icon = 'flag'; d.text = 'флаг игрока ' + sub; }
    else if (type >= 11 && type <= 13) { d.icon = 'luck'; d.text = 'удача ' + sub; }
    else if (type >= 14 && type <= 16) { d.icon = 'morale'; d.text = 'мораль ' + sub; }
    else if (type === 17) { d.icon = 'exp'; d.text = sub + ' опыта'; }
    else if (type === 20) {   // secondary skill: sub = (skill + 1) * 3 + level - 1 (TLevelUpWindow, iconWidget)
        d.icon = 'skill'; d.skill = Math.floor(sub / 3) - 1; d.skill_level = sub % 3 + 1;
        d.text = (skillName(d.skill) || 'навык ' + d.skill) + ' (' + LEVEL_RU[d.skill_level] + ')';
    }
    else if (type === 21) { d.icon = 'creature'; d.text = creatureName(sub, 2) || 'существо ' + sub; }
    else if (type >= 22 && type <= 30) { d.icon = 'building'; d.text = 'строение ' + sub; }
    else if (type >= 31 && type <= 34) { d.icon = 'primary'; d.text = PRIMARY_RU[type - 31] + ' +' + sub; }
    else if (type === 35) { d.icon = 'mana'; d.text = sub + ' маны'; }
    else { d.icon = 'pic' + type; d.text = 'картинка ' + type + ':' + sub; }
    return d;
}
const DLG = { OK: 30722, CANCEL: 30721, YES: 30725, NO: 30726, LEFT: 30729, RIGHT: 30730 };
function protoStyle(style) {
    if (style === 2) return 'yesno';
    if (style === 7 || style === 10) return 'choose';
    return 'ok';
}
// answer index (PROTOCOL: ok/yes = 0, no = 1, choose = option index) -> gpWindowManager->dialogReturn
function answerValue(style, a) {
    if (style === 2) return a === 1 ? DLG.NO : DLG.YES;
    if (style === 7 || style === 10) {
        if (a === 1) return DLG.RIGHT;
        if (a === 0) return DLG.LEFT;
        return style === 10 ? DLG.CANCEL : DLG.LEFT;
    }
    if (style === 5 || style === 6) return a === 1 ? DLG.CANCEL : DLG.OK;
    return DLG.OK;
}
function defaultAnswer(style) {
    const d = BR.defaults[String(style)];
    return d !== undefined ? d : 0;   // ok / yes / left picture
}
// Send the dialog to the client and wait on the game thread for the answer. Thread.sleep releases the JS lock,
// so dialog_answer and state reads from Python keep working; the window message pump keeps the game alive.
function askClient(msg, deflt) {
    const id = ++BR.dialogSeq;
    msg.t = 'dialog';
    msg.id = id;
    if (!BR.client) {
        msg.auto = deflt;
        bsend(msg);
        log('dialog ' + id + ' (no client) -> ' + deflt + ': ' + msg.text.slice(0, 100));
        return deflt;
    }
    BR.pendingDialog = msg;
    bsend(msg);
    const end = Date.now() + BR.dialogTimeout * 1000;
    let ans = null;
    while (Date.now() < end) {
        if (BR.answers[id] !== undefined) { ans = BR.answers[id]; delete BR.answers[id]; break; }
        if (!BR.client) break;
        fnPump();
        Thread.sleep(0.02);
    }
    BR.pendingDialog = null;
    if (ans === null) {
        log('dialog ' + id + ' timeout or client gone -> ' + deflt);
        bsend({ t: 'event', kind: 'dialog_closed', id: id, answer: deflt, auto: true });
        return deflt;
    }
    return ans;
}
const NDARGS = ['pointer', 'int', 'int', 'int', 'int', 'int', 'int', 'int', 'int', 'int', 'int', 'int'];
const origNormalDialog = new NativeFunction(ptr(E.NormalDialog), 'void', NDARGS, 'fastcall');
Interceptor.replace(ptr(E.NormalDialog), new NativeCallback(function (text, style, x, y, p1t, p1s, p2t, p2s, special, timeout, p3t, p3s) {
    // inside a passthrough window the message box is part of that window: the engine draws it, the player clicks it
    // outside the adventure map (main menu, scenario/load screens, high scores ...) the whole screen is streamed
    if (!BR.on || ptActive() || BR.phase !== 'adventure') return origNormalDialog(text, style, x, y, p1t, p1s, p2t, p2s, special, timeout, p3t, p3s);
    if (style === 4 && BR.visible) return origNormalDialog(text, style, x, y, p1t, p1s, p2t, p2s, special, timeout, p3t, p3s);
    if (style === 4) {   // right-click popup: nothing to answer (visible mode: shown while the button is held)
        wm().add(0x38).writeS32(DLG.OK);
        return;
    }
    const pics = [describePic(p1t, p1s), describePic(p2t, p2s), describePic(p3t, p3s)].filter(p => p !== null);
    const ps = protoStyle(style);
    const msg = { text: text.isNull() ? '' : cstr(text, 4096), style: ps, engine_style: style, pics: pics, options: [] };
    if (ps === 'choose') msg.options = pics.slice(0, 2).map(p => ({ text: p.text, icon: p.icon, pic: [p.type, p.sub] }));
    if (style === 10) msg.cancel = true;
    if (BR.visible) {   // shown in the game window and in the client; answered from either side
        nativeDialog(msg, style, () => origNormalDialog(text, style, x, y, p1t, p1s, p2t, p2s, special, timeout, p3t, p3s));
        return;
    }
    const a = askClient(msg, defaultAnswer(style));
    const v = answerValue(style, a);
    wm().add(0x38).writeS32(v);
    log('NormalDialog style ' + style + ' answer ' + a + ' -> ' + v);
}, 'void', NDARGS, 'fastcall'));

// extended_dialog(text, vector<{type, sub}>*, x, y, timeout): text + pictures, OK only
const origExtDialog = new NativeFunction(ptr(E.ExtendedDialog), 'void', ['pointer', 'pointer', 'int', 'int', 'int'], 'fastcall');
Interceptor.replace(ptr(E.ExtendedDialog), new NativeCallback(function (text, vecp, x, y, timeout) {
    if (!BR.on || ptActive() || BR.phase !== 'adventure') return origExtDialog(text, vecp, x, y, timeout);
    const pics = [];
    if (!vecp.isNull()) {
        const v = vec(vecp, 8);
        for (let i = 0; i < Math.min(v.n, 32); i++) {
            const d = describePic(v.first.add(i * 8).readS32(), v.first.add(i * 8 + 4).readS32());
            if (d) pics.push(d);
        }
    }
    const msg = { text: text.isNull() ? '' : cstr(text, 4096), style: 'ok', engine_style: 'extended', pics: pics, options: [] };
    if (BR.visible) {
        nativeDialog(msg, 'extended', () => origExtDialog(text, vecp, x, y, timeout));
        return;
    }
    askClient(msg, 0);
    wm().add(0x38).writeS32(DLG.OK);
}, 'void', ['pointer', 'pointer', 'int', 'int', 'int'], 'fastcall'));

// ---------- visible mode: engine dialogs run natively and are mirrored (PLAN 4.6) ----------
// The dialog is drawn in the game window and runs the engine's own modal loop, so the player can answer it there;
// meanwhile the client gets the same `dialog` message (with native: true). A dialog_answer from the client is turned
// into clicks on the dialog's own buttons (found by widget id in the top window), queued through the passthrough
// input ring. Whoever answers first wins; then `event dialog_closed {id, answer, by: native|client}`.
function answerIndex(style, v) {   // dialogReturn -> PROTOCOL answer
    if (style === 2) return v === DLG.NO ? 1 : 0;
    if (style === 7 || style === 10) return v === DLG.RIGHT ? 1 : v === DLG.CANCEL ? -1 : 0;
    if (style === 5 || style === 6) return v === DLG.CANCEL ? 1 : 0;
    return 0;
}
function dialogButtons(style, a) {   // PROTOCOL answer -> widget ids to click in order
    if (style === 2) return [a === 1 ? DLG.NO : DLG.YES];
    if (style === 7 || style === 10) {
        if (a === -1) return [DLG.CANCEL];
        return [a === 1 ? DLG.RIGHT : DLG.LEFT, DLG.OK];
    }
    if (style === 5 || style === 6) return [a === 1 ? DLG.CANCEL : DLG.OK];
    return [DLG.OK];
}
// center of the widget `id` of the top engine window (screen coordinates), or null
function widgetCenter(id) {
    const w = wm().add(0x58).readPointer();
    if (w.isNull()) return null;
    for (let g = w.add(0x28).readPointer(), n = 0; !g.isNull() && n < 256; g = g.add(0xc).readPointer(), n++) {
        if (g.add(0x10).readS16() !== (id << 16 >> 16)) continue;
        return [w.add(0x18).readS32() + g.add(0x18).readS16() + (g.add(0x1c).readS16() >> 1),
            w.add(0x1c).readS32() + g.add(0x1a).readS16() + (g.add(0x1e).readS16() >> 1)];
    }
    return null;
}
function answerNative(answer) {
    const d = BR.nativeDlg;
    const ids = dialogButtons(d.style, answer);
    for (const id of ids) {
        const c = widgetCenter(id);
        if (c !== null) {
            ptPush(4, c[0], c[1], 0, c[0], c[1]); ptPush(8, c[0], c[1], 0, c[0], c[1]); ptPush(16, c[0], c[1], 0, c[0], c[1]);
        } else {   // no such button (another layout): Enter = OK / yes, Esc = no / cancel
            const scan = id === DLG.NO || id === DLG.CANCEL ? 1 : 0x1c;
            ptPush(1, scan, 0, 0, 0, 0); ptPush(2, scan, 0, 0, 0, 0);
        }
    }
    d.by = 'client';
    log('dialog ' + d.id + ': client answer ' + answer + ' -> clicks on ' + JSON.stringify(ids));
}
function nativeDialog(msg, style, run) {
    const id = ++BR.dialogSeq;
    msg.t = 'dialog';
    msg.id = id;
    msg.native = true;
    try { flushMoves(); } catch (e) { }
    BR.nativeDlg = { id: id, style: style, by: 'native' };
    BR.pendingDialog = msg;
    bsend(msg);
    syncInputActive();
    log('dialog ' + id + ' (native, visible mode): ' + msg.text.slice(0, 100));
    try { run(); } finally {
        const ret = wm().add(0x38).readS32();
        const by = BR.nativeDlg ? BR.nativeDlg.by : 'native';
        BR.nativeDlg = null;
        BR.pendingDialog = null;
        syncInputActive();
        const a = answerIndex(style, ret);
        bsend({ t: 'event', kind: 'dialog_closed', id: id, answer: a, by: by, ret: ret });
        log('dialog ' + id + ' closed by ' + by + ': ' + ret + ' -> ' + a);
    }
}

// ---------- engine windows: passthrough (PLAN 1.9) and the old skip policies ----------
// While a passthrough window runs, the game thread is inside the engine's own modal loop (DoDialog or
// executive::MainLoop of a manager). Every loop polls inputManager::GetEvent: a C hook there
//   - delivers the client's mouse/keys (rpc windowInput -> ring below) into the engine's input queue, one message
//     whenever the engine queue is empty, as Windows would (AppWndProc -> CheckDown/CheckUp format);
//   - copies the screen bitmap into a staging buffer between two frames when bridge.py asks (rpc frame).
// GetCursorPos returns the virtual mouse (last injected position) while a passthrough window is open, so hover
// effects and H3POINT::GetCursorPosition see the client's mouse. Visible mode (cursorSwitch): only while the client's
// mouse was the last one to act; once the real mouse moves over the game window, the real position again.
// The same hook runs the mirror tick (visible mode): every mirrorMs on the game thread, between two input polls.
const PTS = Memory.alloc(128);           // shared i32 block, see PTI
const PTI = { active: 0, vx: 1, vy: 2, ox: 3, oy: 4, capReq: 5, capDone: 6, hash: 7, head: 8, tail: 9,
    w: 10, h: 11, pitch: 12, bytes: 13, delivered: 14, virt: 15, realX: 16, realY: 17, realSeen: 18,
    cursorSwitch: 19, mirrorMs: 20, mirrorLast: 21, winX: 22, winY: 23, winSeen: 24, rightHeld: 25 };
const PT_RING = Memory.alloc(64 * 32);   // injected messages, 8 x u32 each (inputManager message layout)
const PT_STAGE_MAX = 0x200000;
const PT_STAGE = Memory.alloc(PT_STAGE_MAX);
for (let k = 0; k < 32; k++) PTS.add(4 * k).writeS32(0);
PTS.add(4 * PTI.virt).writeS32(1);   // hidden mode: always the virtual mouse while active (as before)
const jsMirror = new NativeCallback(function () {
    try { mirrorTick(); } catch (e) { log('mirror tick: ' + (e.stack || e)); }
}, 'void', []);
const ptModule = new CModule(`
#include <gum/guminterceptor.h>
extern volatile int pts[32];
extern volatile unsigned int ring[512];
extern unsigned char stage[];
extern void js_mirror (void);
extern unsigned int __attribute__((stdcall)) GetTickCount (void);
#define STAGE_MAX 0x200000
static unsigned int rd (unsigned int a) { return *(volatile unsigned int *) a; }
int pt_capture (void) {
  unsigned int wm = rd (0x6992D0), bmp, w, h, pitch, n, i, hash = 2166136261u;
  unsigned int * src;
  unsigned int * dst = (unsigned int *) stage;
  if (!wm) return 0;
  bmp = rd (wm + 0x40);
  if (!bmp) return 0;
  w = rd (bmp + 0x24); h = rd (bmp + 0x28); pitch = rd (bmp + 0x2c); src = (unsigned int *) rd (bmp + 0x30);
  if (!src || !w || !h || w > 4096 || pitch * h > STAGE_MAX) return 0;
  n = (pitch * h) >> 2;
  for (i = 0; i < n; i++) { unsigned int v = src[i]; dst[i] = v; hash = (hash ^ v) * 16777619u; }
  pts[10] = (int) w; pts[11] = (int) h; pts[12] = (int) pitch; pts[13] = (int) (pitch * h); pts[7] = (int) hash;
  return 1;
}
void onEnter (GumInvocationContext * ic) {
  unsigned int im;
  if (pts[5]) { pts[5] = 0; pt_capture (); pts[6] = 1; }
  if (pts[20]) {
    unsigned int t = GetTickCount ();
    if (t - (unsigned int) pts[21] >= (unsigned int) pts[20]) { pts[21] = (int) t; js_mirror (); }
  }
  if (!pts[0] || pts[8] == pts[9]) return;
  im = rd (0x699530);
  if (!im) return;
  if (rd (im + 0x838) == rd (im + 0x83C)) {   /* engine queue empty: deliver the next injected message */
    unsigned int tail = rd (im + 0x83C) & 63, k;
    unsigned int * m = (unsigned int *) (im + 0x38 + tail * 32);
    volatile unsigned int * s = ring + (pts[8] & 63) * 8;
    for (k = 0; k < 8; k++) m[k] = s[k];
    if (s[0] >= 4 && s[0] <= 64) { pts[1] = (int) s[4]; pts[2] = (int) s[5]; pts[15] = 1; }
    *(volatile unsigned int *) (im + 0x83C) = (tail + 1) & 63;
    pts[8] = (pts[8] + 1) & 63;
    pts[14]++;
  }
}
`, { pts: PTS, ring: PT_RING, stage: PT_STAGE, js_mirror: jsMirror,
     GetTickCount: Process.getModuleByName('kernel32.dll').getExportByName('GetTickCount') });
Interceptor.attach(ptr(A.GetEvent), { onEnter: ptModule.onEnter });
const fnPtCapture = new NativeFunction(ptModule.pt_capture, 'int', []);
const cursorModule = new CModule(`
#include <gum/guminterceptor.h>
extern volatile int pts[32];
typedef struct { int x, y; } Pt;
extern int __attribute__((stdcall)) ClientToScreen (void * hwnd, Pt * p);
typedef struct { int * pt; } State;
void onEnter (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  s->pt = (int *) gum_invocation_context_get_nth_argument (ic, 0);
}
void onLeave (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  int ok = gum_invocation_context_get_return_value (ic) != 0;
  if (!s->pt) return;
  if (pts[19] && ok) {   /* visible mode: did the real mouse move? */
    if (!pts[18] || s->pt[0] != pts[16] || s->pt[1] != pts[17]) {
      if (pts[18]) pts[15] = 0;
      pts[16] = s->pt[0]; pts[17] = s->pt[1]; pts[18] = 1;
    }
  }
  if (pts[0] && pts[15]) {   /* the client's (virtual) mouse */
    s->pt[0] = pts[1] + pts[3]; s->pt[1] = pts[2] + pts[4];
    gum_invocation_context_replace_return_value (ic, (gpointer) 1);
    return;
  }
  if (pts[19] && !ok && pts[24]) {   /* visible mode, no cursor (no input desktop): where the window's last mouse
                                        message was, so mouse messages sent to the window work as real input */
    Pt o; o.x = 0; o.y = 0;
    ClientToScreen (*(void * volatile *) 0x699650, &o);
    s->pt[0] = pts[22] + o.x; s->pt[1] = pts[23] + o.y;
    gum_invocation_context_replace_return_value (ic, (gpointer) 1);
  }
}
/* inputManager::CheckDown (0x4EC370, AppWndProc for WM_MOUSEMOVE..WM_RBUTTONDBLCLK; EDX = message, args wParam,
   lParam): a mouse message of the game window itself, i.e. the player's real mouse */
void mouse_enter (GumInvocationContext * ic) {
  unsigned int msg = (unsigned int) ic->cpu_context->edx;
  unsigned int lp;
  if (msg < 0x200 || msg > 0x206) return;
  lp = (unsigned int) gum_invocation_context_get_nth_argument (ic, 1);
  pts[22] = (short) (lp & 0xffff); pts[23] = (short) (lp >> 16); pts[24] = 1;
  if (msg == 0x204) pts[25] = 1;          /* right button held in the game window: right-click info popups */
  else if (msg == 0x205) pts[25] = 0;
  if (pts[19]) pts[15] = 0;
}
`, { pts: PTS, ClientToScreen: Process.getModuleByName('user32.dll').getExportByName('ClientToScreen') });
Interceptor.attach(ptr(0x4EC370), { onEnter: cursorModule.mouse_enter });
Interceptor.attach(Process.getModuleByName('user32.dll').getExportByName('GetCursorPos'), cursorModule);
const fnClientToScreen = new NativeFunction(Process.getModuleByName('user32.dll').getExportByName('ClientToScreen'),
    'int', ['pointer', 'pointer'], 'stdcall');

const PT = { stack: [], lastHash: null, opened: 0 };
function ptActive() { return PT.stack.length > 0; }
function ptPush(cmd, sub, item, qual, x, y) {
    const head = PTS.add(4 * PTI.head).readS32(), tail = PTS.add(4 * PTI.tail).readS32();
    if (((tail + 1) & 63) === head) return false;   // full
    const m = PT_RING.add(tail * 32);
    m.writeU32(cmd); m.add(4).writeS32(sub); m.add(8).writeS32(item); m.add(12).writeU32(qual);
    m.add(16).writeS32(x); m.add(20).writeS32(y); m.add(24).writeU32(0); m.add(28).writeU32(0);
    PTS.add(4 * PTI.tail).writeS32((tail + 1) & 63);
    return true;
}
function ptOpen(name, info) {
    if (!PT.stack.length) {
        // the client first sees what led here (hero walked into the town ...), then the window
        try { flushMoves(); sendState('window_open'); } catch (e) { log('pt state ' + e); }
        try {   // window client origin on the (invisible) screen, for the virtual GetCursorPos
            const p = Memory.alloc(8);
            p.writeS32(0); p.add(4).writeS32(0);
            fnClientToScreen(ptr(A.hwndApp).readPointer(), p);
            PTS.add(4 * PTI.ox).writeS32(p.readS32()); PTS.add(4 * PTI.oy).writeS32(p.add(4).readS32());
        } catch (e) { }
        PTS.add(4 * PTI.head).writeS32(PTS.add(4 * PTI.tail).readS32());   // drop stale input
        PTS.add(4 * PTI.active).writeS32(1);
        PT.opened = Date.now();
    }
    PT.stack.push(name);
    PT.lastHash = null;
    const b = screenBitmap();
    bsend(Object.assign({ t: 'window_open', name: name, w: b ? b.w : 800, h: b ? b.h : 600, depth: PT.stack.length },
        info || {}));
    log('window ' + name + ' passthrough open, depth ' + PT.stack.length);
}
function ptClose(name, ret) {
    PT.stack.pop();
    bsend({ t: 'window_close', name: name, depth: PT.stack.length + 1, ret: ret });
    log('window ' + name + ' passthrough closed (' + ret + '), depth ' + PT.stack.length);
    if (PT.stack.length) return;
    PTS.add(4 * PTI.active).writeS32(0);
    PTS.add(4 * PTI.head).writeS32(PTS.add(4 * PTI.tail).readS32());
    if (BR.phase !== 'adventure') syncInputActive();
    try { flushMoves(); sendState('window_close'); } catch (e) { log('pt state ' + e); }
}
// Run fn (the engine's own modal loop) as a passthrough window.
function ptRun(name, info, fn) {
    ptOpen(name, info);
    let r;
    try { r = fn(); } finally { ptClose(name, wm().add(0x38).readS32()); }
    return r;
}

// vtable -> [name, legacy policy]. Policies: 'pass' = passthrough (the default for every window below when a
// capable client is connected; without one -> the legacy policy), 'run' = run the original loop without streaming,
// 'ok' / 'cancel' = skip the modal loop with that dialogReturn, 'levelup' = native `choose` dialog in the client.
// bridge.py --windows name=policy,... overrides (name '*' = every window not in the table, incl. HotA's own).
const WINDOWS = {
    '0x63db40': ['dialogBox', 'run'], '0x63fe74': ['levelUp', 'levelup'], '0x63d46c': ['combatResults', 'ok'],
    '0x63eae8': ['heroScreen', 'cancel'], '0x64373c': ['townScreen', 'cancel'], '0x642438': ['heroMeeting', 'cancel'],
    '0x640c5c': ['recruit', 'cancel'], '0x643864': ['garrison', 'cancel'], '0x643990': ['tavern', 'cancel'],
    '0x643a08': ['marketplace', 'cancel'], '0x643a80': ['blackMarket', 'cancel'], '0x643be8': ['university', 'cancel'],
    '0x63eb68': ['hillFort', 'cancel'], '0x641630': ['altar', 'cancel'], '0x642e40': ['townGate', 'cancel'],
    '0x63db9c': ['dimensionDoor', 'cancel'], '0x641ddc': ['spellbook', 'cancel'], '0x643c64': ['viewWorld', 'cancel'],
    '0x63b8f8': ['splitStack', 'cancel'], '0x63a610': ['adventureOptions', 'cancel'], '0x63ff60': ['mainMenu', 'run'],
    '0x63e6d8': ['gameType', 'run'], '0x641720': ['scenarioInfo', 'cancel'], '0x641cbc': ['selectScenario', 'run'],
    '0x640330': ['kingdomOverview', 'cancel'], '0x642478': ['systemOptions', 'cancel'],
};
const WINDOWS_BY_NAME = {};
for (const k in WINDOWS) WINDOWS_BY_NAME[WINDOWS[k][0]] = 1;
// never changed: the launcher's menu automation; defaults with a passthrough client: level-up stays the native
// `choose` question, the battle results window is closed with OK (the client gets `event battle`)
const FIXED = { mainMenu: 1, gameType: 1, selectScenario: 1 };
const DEFAULT_PASS = { levelUp: 'levelup', combatResults: 'ok' };
function windowName(vt) {
    const k = vt.toString();
    if (WINDOWS[k]) return WINDOWS[k];
    const hm = hotaDll();
    if (hm && vt.compare(hm.base) >= 0 && vt.compare(hm.base.add(hm.size)) < 0)
        return ['hota:0x' + vt.sub(hm.base).add(0x10000000).toString(16), 'cancel'];
    return ['vt:' + k, 'cancel'];
}
// effective policy of a window now
function windowPolicy(name, legacy) {
    if (FIXED[name]) return legacy;
    const o = BR.policy[name] !== undefined ? BR.policy[name] : (WINDOWS_BY_NAME[name] ? undefined : BR.policy['*']);
    // visible mode: every window runs in the game window (the player can always click it there), streamed to a
    // passthrough client as well; level-up and battle results too
    let p = o !== undefined ? o : ((!BR.visible && DEFAULT_PASS[name]) || 'pass');
    if (name === 'combatResults' && BR.battles === 'game' && o === undefined) p = 'pass';   // the player clicks OK
    if (p === 'pass' && !BR.pass && !ptActive() && !BR.visible) p = legacy;   // nobody can click it: old behaviour
    return p;
}
// TLevelUpWindow(this, hero, primary, skillPic1, skillPic2): remember the offered skills for the client
Interceptor.attach(ptr(0x4F8F10), {
    onEnter(a) {
        BR.levelUp = { hero: a[0].isNull() ? -1 : a[0].add(HERO.id).readS32(), primary: a[1].toInt32(),
            s1: a[2].toInt32(), s2: a[3].toInt32() };
    },
});
const origDoDialog = new NativeFunction(ptr(E.DoDialog), 'int', ['pointer', 'pointer', 'pointer', 'int'], 'thiscall');
Interceptor.replace(ptr(E.DoDialog), new NativeCallback(function (self, win, handler, fade) {
    if (!BR.on || win.isNull() || BR.phase !== 'adventure') return origDoDialog(self, win, handler, fade);
    // visible mode: the modal loop of a NormalDialog / extended_dialog that is mirrored as `dialog` (no window_open),
    // and right-click info popups of the game window (quick views, held right button) stay native
    if ((BR.nativeDlg !== null || (BR.visible && PTS.add(4 * PTI.rightHeld).readS32())) && !ptActive())
        return origDoDialog(self, win, handler, fade);
    const [name, legacy] = windowName(win.readPointer());
    const policy = windowPolicy(name, legacy);
    if (policy === 'run') return origDoDialog(self, win, handler, fade);
    if (policy === 'pass') return ptRun(name, { kind: 'dialog' }, () => origDoDialog(self, win, handler, fade));
    if (policy === 'levelup') {
        const lu = BR.levelUp || {};
        const opts = [lu.s1, lu.s2].filter(s => s !== undefined && s >= 0).map(s => {
            const d = describePic(20, s);
            return { text: d.text, icon: 'skill', pic: [20, s], skill: d.skill, skill_level: d.skill_level };
        });
        const msg = { text: 'Повышение уровня: выберите навык', style: opts.length > 1 ? 'choose' : 'ok',
            engine_style: 'levelup', hero: lu.hero, primary: lu.primary, options: opts, pics: [] };
        const a = askClient(msg, 0);
        const v = opts.length > 1 ? (a === 1 ? 0x7db : 0x7da) : DLG.OK;
        self.add(0x38).writeS32(v);
        bsend({ t: 'event', kind: 'window', name: name, action: 'answered', answer: a, hero: lu.hero });
        BR.levelUp = null;
        return 0;
    }
    const v = policy === 'ok' ? DLG.OK : DLG.CANCEL;
    self.add(0x38).writeS32(v);
    log('window ' + name + ' skipped -> ' + v);
    bsend({ t: 'event', kind: 'window', name: name, action: 'closed', ret: v });
    return 0;
}, 'int', ['pointer', 'pointer', 'pointer', 'int'], 'thiscall'));

// Manager screens run their own executive::MainLoop via executive::CallManager.
//  - town screen (hero enters own town, or open_town): passthrough; legacy 'close': it runs, so the engine does
//    its visit logic, and gets the "close" message {0x200, 10} on its first idle input poll in MainLoop
//    (townManager::Main case 10 -> return 2 -> leave the loop)
//  - hero meeting (hero steps onto an own hero): passthrough; legacy 'skip': not run (no exchange)
//  - battle screen: passthrough when --battles manual turned quick combat off for this battle (DoCombat below)
const MANAGERS = { '0x643730': ['townScreen', 'close'], '0x64245c': ['heroMeeting', 'skip'],
    '0x63d3e8': ['combat', 'run'] };
const inj = Memory.alloc(16);   // [0] messages left to inject, [1] command, [2] subType, [3] itemId
inj.writeU32(0);
const injModule = new CModule(`
#include <gum/guminterceptor.h>
extern volatile int inj[4];
typedef struct { unsigned int * msg; } State;
void onEnter (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  s->msg = (unsigned int *) gum_invocation_context_get_nth_argument (ic, 0);
}
void onLeave (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  unsigned int ra;
  if (inj[0] <= 0 || s->msg == 0 || s->msg[0] != 0) return;
  ra = (unsigned int) gum_invocation_context_get_return_address (ic);
  if (ra < 0x4B0BA0u || ra >= 0x4B0C95u) return;   /* only the executive main loop */
  s->msg[0] = inj[1]; s->msg[1] = inj[2]; s->msg[2] = inj[3]; s->msg[3] = 0;
  inj[0]--;
}
`, { inj: inj });
Interceptor.attach(ptr(A.GetEvent), injModule);
const origCallManager = new NativeFunction(ptr(E.CallManager), 'void', ['pointer', 'pointer'], 'thiscall');
Interceptor.replace(ptr(E.CallManager), new NativeCallback(function (ex, mgr) {
    const m = BR.on && BR.phase === 'adventure' && !mgr.isNull() ? MANAGERS[mgr.readPointer().toString()] : undefined;
    if (m === undefined) return origCallManager(ex, mgr);
    const [name, legacy] = m;
    const info = { kind: 'manager' };
    if (name === 'townScreen') {
        try { const t = mgr.add(0x38).readPointer(); if (!t.isNull()) info.town = t.readS8(); } catch (e) { }
    }
    if (name === 'combat') {
        if (!BR.manualBattle) return origCallManager(ex, mgr);   // quick combat: nothing is drawn
        BR.manualBattle = false;
        return ptRun(name, info, () => origCallManager(ex, mgr));
    }
    let policy = ptActive() ? 'pass' : (BR.policy[name] || 'pass');
    if (policy === 'pass' && !BR.pass && !ptActive() && !BR.visible) policy = legacy;
    if (policy === 'run') return origCallManager(ex, mgr);
    if (policy === 'pass') return ptRun(name, info, () => origCallManager(ex, mgr));
    const ev = { t: 'event', kind: 'window', name: name, action: 'closed' };
    if (info.town !== undefined) ev.town = info.town;
    if (name === 'townScreen' && policy === 'close') {
        inj.writeS32(1); inj.add(4).writeU32(0x200); inj.add(8).writeU32(10); inj.add(12).writeU32(0);
        origCallManager(ex, mgr);
        inj.writeS32(0);
        ev.action = 'opened+closed';
    }
    log('manager ' + name + ' ' + JSON.stringify(ev));
    bsend(ev);
}, 'void', ['pointer', 'pointer'], 'thiscall'));

// input from the client (bridge.py window_input): engine coordinates, DIK scan codes
const MOUSE_CMD = { left: { down: 8, up: 16 }, right: { down: 32, up: 64 } };
function windowInput(o) {
    if (!ptActive() && !menuStreaming()) return false;
    const qual = (o.shift ? 1 : 0) | (o.ctrl ? 4 : 0) | (o.alt ? 32 : 0);
    if (o.kind === 'mouse') {
        const b = screenBitmap();
        const x = Math.max(0, Math.min((b ? b.w : 800) - 1, o.x | 0)), y = Math.max(0, Math.min((b ? b.h : 600) - 1, o.y | 0));
        let cmd = 4;   // COMMAND_MOUSE_ROLLOVER
        if (o.action === 'down' || o.action === 'up') {
            const bt = MOUSE_CMD[o.button || 'left'];
            if (!bt) return false;
            cmd = bt[o.action];
        }
        // as CheckDown builds it: subType = x, itemId = y, mouseX/mouseY
        if (o.action === 'click') return ptPush(4, x, y, qual, x, y) && ptPush(8, x, y, qual, x, y) && ptPush(16, x, y, qual, x, y);
        return ptPush(cmd, x, y, qual, x, y);
    }
    if (o.kind === 'key') {
        const scan = o.scan | 0;
        if (scan <= 0 || scan > 0xff) return false;
        if (o.action === 'down' || o.action === 'press') ptPush(1, scan, 0, qual, 0, 0);
        if (o.action === 'up' || o.action === 'press') ptPush(2, scan, 0, qual, 0, 0);
        return true;
    }
    return false;
}
// engine frame for bridge.py: copied between two input polls when the game thread is in a loop, else directly
function grabFrame(force) {
    PTS.add(4 * PTI.capDone).writeS32(0);
    PTS.add(4 * PTI.capReq).writeS32(1);
    const end = Date.now() + 80;
    while (!PTS.add(4 * PTI.capDone).readS32() && Date.now() < end) Thread.sleep(0.003);
    if (!PTS.add(4 * PTI.capDone).readS32()) {
        PTS.add(4 * PTI.capReq).writeS32(0);
        if (!fnPtCapture()) return null;
    }
    const hash = PTS.add(4 * PTI.hash).readS32();
    if (!force && hash === PT.lastHash) return { same: true };
    PT.lastHash = hash;
    const w = PTS.add(4 * PTI.w).readS32(), h = PTS.add(4 * PTI.h).readS32(), pitch = PTS.add(4 * PTI.pitch).readS32();
    send({ t: 'frame', w: w, h: h, pitch: pitch, hash: hash }, PT_STAGE.readByteArray(pitch * h));
    return { w: w, h: h, pitch: pitch, hash: hash };
}

// ---------- game phase: main menu <-> adventure map (PLAN 4.5) ----------
// oldmain (0x4EEA70) is the game's outer loop: main menu, new game / load / campaign screens, high scores, credits
// ... -> AddManager(advManager) -> executive::MainLoop -> RemoveManager -> back to the menus. The adventure map
// therefore runs exactly while that one MainLoop call (made from oldmain; nested MainLoops of the town screen or
// combat come from CallManager) runs. Loading a save from the map, restart, "main menu", victory/defeat, the next
// campaign scenario all leave this loop and enter it again with the new game state.
//   adventure  the bridge's hooks work as before; entering it sends `phase` + a fresh snapshot (new map / players)
//   menu       everything the engine shows is streamed (bridge.py sends window_frame), the client's mouse/keys go
//              into the engine's input queue as for a passthrough window; dialogs and windows run natively
const OLDMAIN_LO = 0x4EEA70, OLDMAIN_HI = 0x4EEA70 + 7068;
const MAIN_LOOP = 0x4B0BA0;
function menuStreaming() { return BR.on && BR.phase === 'menu'; }
function syncInputActive() {   // injected input + virtual cursor: passthrough window open, the menu streamed, or
    // (visible mode) a native dialog the client may answer
    const on = PT.stack.length > 0 || menuStreaming() || BR.nativeDlg !== null;
    if (on && !PTS.add(4 * PTI.active).readS32()) {
        PTS.add(4 * PTI.head).writeS32(PTS.add(4 * PTI.tail).readS32());   // drop stale input
        try {
            const p = Memory.alloc(8);
            p.writeS32(0); p.add(4).writeS32(0);
            fnClientToScreen(ptr(A.hwndApp).readPointer(), p);
            PTS.add(4 * PTI.ox).writeS32(p.readS32()); PTS.add(4 * PTI.oy).writeS32(p.add(4).readS32());
        } catch (e) { }
    }
    PTS.add(4 * PTI.active).writeS32(on ? 1 : 0);
}
function phaseMsg() {
    const m = { t: 'phase', phase: BR.phase, gen: BR.gen };
    if (BR.phase === 'adventure') {
        try {
            const mi = mapInfo();
            m.size = mi.size; m.levels = mi.levels; m.me = me(); m.current = curPlayer();
            m.file = cstr(game().add(G.filename), 64);
            m.humans = readPlayers().filter(p => p.human && p.alive).map(p => p.id);
        } catch (e) { }
    }
    return m;
}
function enterAdventure() {
    BR.phase = 'adventure';
    BR.gen++;
    typeCache = null;                       // object types belong to the map (gpGame itself stays the same)
    BR.lastPos = {}; BR.moves = {}; BR.moveOrder = [];
    BR.levelUp = null;
    BR.lastMe = -1;
    BR.digest = null;
    meLast = -1;
    syncInputActive();
    try { trackPositions(false); } catch (e) { }
    if (!BR.on) return;
    const m = phaseMsg();
    log('phase adventure #' + BR.gen + ' ' + JSON.stringify(m));
    bsend(m);
    // read right here on the game thread, before the first message of the map (scenario intro ...) blocks it
    try { const sn = snapshot(); BR.lastMe = sn.me; send({ t: 'snapshot_raw', s: sn, why: 'adventure' }); }
    catch (e) { log('snapshot at adventure start: ' + (e.stack || e)); }
}
function leaveAdventure() {
    BR.phase = 'menu';
    try { flushMoves(); } catch (e) { }
    syncInputActive();
    if (!BR.on) return;
    log('phase menu (gGameCommand ' + ptr(A.gGameCommand).readS32() + ', game over ' + ptr(E.gbGameOver).readU32() + ')');
    bsend({ t: 'phase', phase: 'menu', gen: BR.gen, why: ptr(E.gbGameOver).readU32() ? 'game_over' : 'left' });
}
Interceptor.attach(ptr(MAIN_LOOP), {
    onEnter() {
        const ra = this.returnAddress.toUInt32();
        this.top = ra >= OLDMAIN_LO && ra < OLDMAIN_HI;
        if (this.top) enterAdventure();
    },
    onLeave() { if (this.top) leaveAdventure(); },
});
if (BR.phase === null) BR.phase = managers().indexOf('advManager') >= 0 ? 'adventure' : 'menu';   // attach mode

// ---------- battles ----------
function readArmy(p) {   // armyGroup: int type[7], int count[7]
    if (p.isNull()) return null;
    const a = [];
    for (let k = 0; k < 7; k++) {
        const c = p.add(4 * k).readS32();
        a.push(c < 0 ? -1 : [c, p.add(0x1c + 4 * k).readS32()]);
    }
    return a;
}
function armyLosses(before, after) {
    const out = [];
    if (!before) return out;
    for (let k = 0; k < 7; k++) {
        const b = before[k];
        if (b === -1) continue;
        const a = after ? after[k] : -1;
        const left = a !== -1 && a[0] === b[0] ? a[1] : 0;
        if (left < b[1]) out.push([b[0], b[1] - left]);
    }
    return out;
}
// advManager::DoCombat(adv, point, atkHero, atkArmy, defPlayer, defTown, defHero, defArmy, seed, finish, layout)
Interceptor.attach(ptr(E.DoCombat), {
    onEnter(a) {
        if (!BR.on || BR.phase !== 'adventure') return;
        // --battles manual: quick combat off for this battle when someone can play it (the battle screen is a
        // passthrough window, CallManager(combatManager) above); otherwise the engine's quick combat as before
        // --battles game (bridge.py --menu): the game's Quick Combat option decides, as in the original; nobody to
        // play the battle screen -> quick combat for this battle only
        // visible mode: the player can always fight on the battle screen in the game window
        const gc = ptr(A.gConfig);
        const someone = BR.pass || BR.visible;
        let manual;
        if (BR.battles === 'game') {
            this.quick = gc.add(0x24).readU32();
            manual = this.quick === 0 && someone;
            if (!manual) gc.add(0x24).writeU32(1);
        } else {
            manual = BR.battles === 'manual' && someone;
            gc.add(0x24).writeU32(manual ? 0 : 1);
        }
        BR.manualBattle = manual;
        const atkHero = a[1], defHero = a[5];
        this.b = { point: point(a[0].toUInt32()), atkHero: atkHero.isNull() ? -1 : atkHero.add(HERO.id).readS32(),
            atkOwner: atkHero.isNull() ? -1 : atkHero.add(HERO.owner).readS8(),
            atkArmyP: a[2], defPlayer: a[3].toInt32(), defTown: a[4].isNull() ? -1 : a[4].readS8(),
            defHero: defHero.isNull() ? -1 : defHero.add(HERO.id).readS32(), defArmyP: a[6] };
        this.b.atkBefore = readArmy(a[2]);
        this.b.defBefore = readArmy(a[6]);
        log('battle start ' + JSON.stringify({ atk: this.b.atkHero, def: this.b.defHero, town: this.b.defTown, at: this.b.point }));
    },
    onLeave(r) {
        if (!BR.on || !this.b) return;
        BR.manualBattle = false;
        if (this.quick !== undefined) ptr(A.gConfig).add(0x24).writeU32(this.quick);   // the player's option back
        const b = this.b;
        const winner = r.toInt32();
        const defender = b.defHero >= 0 ? { type: 'hero', id: b.defHero, owner: b.defPlayer } :
            b.defTown >= 0 ? { type: 'town', id: b.defTown, owner: b.defPlayer } :
            { type: 'monster', army: b.defBefore, at: b.point };
        let atkAfter = null, defAfter = null;
        try { atkAfter = readArmy(b.atkArmyP); } catch (e) { }
        try { defAfter = readArmy(b.defArmyP); } catch (e) { }
        // other players' battles only when the local player sees the battle tile
        const who = me();
        const atkOwner = b.atkOwner;
        if (atkOwner !== who && b.defPlayer !== who) {
            const mi = mapInfo();
            const [x, y, z] = b.point;
            if (x >= mi.size || y >= mi.size || z >= mi.levels || !readFog(mi, who)[z][y * mi.size + x]) return;
        }
        bsend({ t: 'event', kind: 'battle', attacker: b.atkHero, attacker_owner: atkOwner, defender: defender,
            winner: winner === 0 ? 'attacker' : winner === 1 ? 'defender' : String(winner), at: b.point,
            losses: { attacker: armyLosses(b.atkBefore, atkAfter), defender: armyLosses(b.defBefore, defAfter) } });
        log('battle end, winner side ' + winner);
    },
});

// ---------- visible mode: mirror what the player does in the game window (PLAN 4.6) ----------
// Every BR.mirrorMs the GetEvent C hook calls mirrorTick on the game thread (so the engine is between two input
// polls, its state consistent). On the human's own turn, with no bridge command running, no window/dialog open, it
// compares a cheap digest (tiles + object lists, fog, players, towns, the heroes on the map, date, current player)
// with the digest of the last state the bridge sent. A difference means the player changed something in the game
// window: hero moves collected meanwhile go out as hero_move (external: true), then a `state` why "external".
// AI turns, windows and dialogs report themselves (NextPlayer, window_close, dialog_closed + state) as before.
const hashModule = new CModule(`
unsigned int hash_mem (const unsigned int * p, unsigned int n) {
  unsigned int h = 2166136261u, i;
  if (!p) return 0;
  for (i = 0; i < (n >> 2); i++) h = (h ^ p[i]) * 16777619u;
  return h;
}
`);
const fnHashMem = new NativeFunction(hashModule.hash_mem, 'uint', ['pointer', 'uint']);
function stateDigest() {
    const g = game();
    const mi = mapInfo();
    const n = mi.size * mi.size * mi.levels;
    const tv = vec(g.add(0x21610), 0x168);
    const parts = [fnHashMem(mi.cells, n * CELL), fnHashMem(ptr(E.mapExtra).readPointer(), n * 2),
        fnHashMem(g.add(G.player), 8 * G.playerSize), fnHashMem(tv.first, tv.n * 0x168),
        g.add(G.day).readU32(), g.add(G.month).readU16(), curPlayer(), ptr(E.giCurWatchPlayer).readS32()];
    // the hero without his route target (+0x35..+0x3E): a first click on the map only shows the route
    for (const id of onMapHeroes()) {
        const h = heroPtr(id);
        parts.push(id, fnHashMem(h, HERO.targetX), fnHashMem(h.add(0x40), G.heroSize - 0x40));
    }
    return parts.join(',');
}
function mirrorQuiet() {   // not now: no map, a command of ours runs, a window / dialog / battle is open, game over
    if (!BR.on || !BR.visible || BR.phase !== 'adventure' || BR.busy || ptActive() || BR.nativeDlg !== null) return true;
    if (ptr(E.dialogDepth).readS32() !== 0 || ptr(E.gbInCombat).readU8() !== 0 || ptr(E.gbGameOver).readU32() !== 0) return true;
    const a = adv();
    if (a.isNull() || a.add(0x34).readS32() === 2) return true;
    const cp = curPlayerData();
    return cp.isNull() || !cp.add(0xe2).readU8() || !cp.add(0xe1).readU8();   // AI turn: NextPlayer reports it
}
BR.mirrorStats = { ticks: 0, external: 0 };
function mirrorTick() {
    if (mirrorQuiet()) return;
    BR.mirrorStats.ticks++;
    const d = stateDigest();
    if (d === BR.digest) return;
    const first = BR.digest === null;
    BR.digest = d;
    if (first) return;    // baseline after a snapshot / phase change
    BR.mirrorStats.external++;
    flushMoves(true);
    sendState('external');
}

// ---------- commands on the game thread ----------
// A C listener on advManager::Main (0x408710) calls js_cmd only when a command is queued and the message is the
// idle one (command 0). js_cmd checks: local human's turn, no modal open, no combat, adv manager active.
// path / select_hero run right there. move_hero / end_turn REWRITE the idle message into the button click
// {0x200, 0xD, 7 | 0xC}, so Main itself runs ProcessDeSelect -> DoAdvCommand / NextPlayer -> DoEvent, exactly as
// for a click. (Gum does not run Frida hooks for calls made from inside a listener on the same thread, so the
// dialog/window/battle hooks would be skipped if the engine were called from js_cmd.) Main's onLeave of that
// invocation reports cmd_done.
const cmdQueue = [];
const cmdShared = Memory.alloc(8);   // [0] command queued, [4] injected message in flight
cmdShared.writeU32(0); cmdShared.add(4).writeU32(0);
let inflight = null;
function canRunCommand() {
    if (!BR.on || BR.busy || BR.phase !== 'adventure') return false;
    if (ptr(E.gbGameOver).readU32() !== 0) return false;
    if (ptr(E.dialogDepth).readS32() !== 0) return false;
    if (ptr(E.gbInCombat).readU8() !== 0) return false;
    const a = adv();
    if (a.isNull() || a.add(0x34).readS32() === 2) return false;   // status DISABLED: another manager runs
    const cp = curPlayerData();
    if (cp.isNull() || !cp.add(0xe2).readU8() || !cp.add(0xe1).readU8()) return false;   // AI turn
    if (curPlayer() !== ptr(E.giCurWatchPlayer).readS32()) return false;
    return true;
}
const STATE_AFTER = { move_hero: 1, end_turn: 1, open_town: 1, open_hero: 1 };
function finishCommand(c, res, err) {
    try { flushMoves(); } catch (e) { log('flush ' + e); }
    if (STATE_AFTER[c.cmd] && BR.phase === 'adventure') {
        try { sendState(c.cmd); } catch (e) { log('state ' + e); }
    } else if (BR.visible && BR.digest !== null && BR.phase === 'adventure') {
        try { BR.digest = stateDigest(); } catch (e) { }   // select_hero etc. are ours, not "external"
    }
    send({ t: 'cmd_done', id: c.id, cmd: c.cmd, result: res, error: err });
}
// returns 1 when the idle message was rewritten into a button click (then Main's onLeave calls js_cmd_done)
const jsCmd = new NativeCallback(function (msg) {
    if (!canRunCommand()) return 0;
    const c = cmdQueue.shift();
    if (cmdQueue.length === 0) cmdShared.writeU32(0);
    if (!c) return 0;
    let res = null, err = null, inject = null;
    try {
        res = runCommand(c);
        if (res && res.button) {          // button click {0x200, 0xD, id}, as ProcessDeSelect gets it
            inject = [0x200, 0xd, res.button];
            delete res.button;
        }
        if (inject && res.adv !== undefined) {   // DoAdvCommand runs this advCommand instead of 7 (see below)
            BR.advOverride = res.adv;
            delete res.adv;
        }
    } catch (e) { err = e.message || String(e); log('cmd ' + c.cmd + ' failed: ' + (e.stack || e)); }
    if (!inject) { finishCommand(c, res, err); return 0; }
    for (let k = 0; k < 8; k++) msg.add(4 * k).writeU32(0);
    msg.writeU32(inject[0]); msg.add(4).writeU32(inject[1]); msg.add(8).writeU32(inject[2]);
    inflight = { c: c, res: res };
    BR.busy = true;
    return 1;
}, 'int', ['pointer']);
const jsCmdDone = new NativeCallback(function () {
    const f = inflight;
    inflight = null;
    BR.busy = false;
    BR.advOverride = null;
    if (!f) return;
    let res = f.res, err = null;
    try { res = afterCommand(f.c, res); } catch (e) { err = e.message || String(e); }
    finishCommand(f.c, res, err);
}, 'void', []);
const mainModule = new CModule(`
#include <gum/guminterceptor.h>
extern volatile unsigned int cmd_shared[2];
extern int js_cmd (unsigned int * msg);
extern void js_cmd_done (void);
typedef struct { int mine; } State;
void onEnter (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  unsigned int * msg;
  s->mine = 0;
  if (!cmd_shared[0] || cmd_shared[1]) return;
  msg = (unsigned int *) gum_invocation_context_get_nth_argument (ic, 0);
  if (msg == 0 || msg[0] != 0) return;
  if (js_cmd (msg)) { s->mine = 1; cmd_shared[1] = 1; }
}
void onLeave (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  if (!s->mine) return;
  cmd_shared[1] = 0;
  js_cmd_done ();
}
`, { cmd_shared: cmdShared, js_cmd: jsCmd, js_cmd_done: jsCmdDone });
Interceptor.attach(ptr(E.advMain), mainModule);
// open_town / open_hero: the injected "move" button reaches DoAdvCommand (ProcessDeSelect case 7); before its body
// runs, advCommand 7 becomes 3 (town::View of the selected town, as Enter on a selected town) or 2 (hero screen of
// the selected hero, H3Hero::ShowDialog) -- the same calls the adventure UI makes.
BR.advOverride = null;
Interceptor.attach(ptr(E.DoAdvCommand), {
    onEnter() {
        if (BR.advOverride === null || !BR.busy) return;
        adv().add(ADV.advCommand).writeS32(BR.advOverride);
        BR.advOverride = null;
    },
});
const fnSetTownContext = new NativeFunction(ptr(0x417790), 'void', ['pointer', 'int', 'int', 'int'], 'thiscall');

function selectHero(id) {
    if (curPlayerData().add(4).readS32() !== id) fnSetHeroContext(adv(), id, 0, 0, 1);
}
function checkOwnHero(id) {
    if (typeof id !== 'number' || id < 0 || id >= heroCount()) throw new Error('bad hero id ' + id);
    const h = heroPtr(id);
    if (h.add(HERO.owner).readS8() !== me()) throw new Error('hero ' + id + ' is not yours');
    return h;
}
function checkTo(to) {
    const mi = mapInfo();
    if (!Array.isArray(to) || to.length < 3 || to[0] < 0 || to[1] < 0 || to[0] >= mi.size || to[1] >= mi.size ||
        to[2] < 0 || to[2] >= mi.levels) throw new Error('bad target ' + JSON.stringify(to));
}
function setTarget(h, to) {
    h.add(HERO.targetX).writeS32(to[0]); h.add(HERO.targetY).writeS32(to[1]); h.add(HERO.targetZ).writeS16(to[2]);
}
// gpSearchArray->result (sa+0x48 vector of pathCell*, reversed: [n-1] = first step); advManager.pRouteArray
// (+0x48) has the arrow frame per tile: 1..25 = reachable today, +25 = beyond today.
function readRoute() {
    const sa = ptr(E.gpSearchArray).readPointer();
    const first = sa.add(0x4c).readPointer(), last = sa.add(0x50).readPointer();
    const n = first.isNull() ? 0 : last.sub(first).toInt32() >> 2;
    const route = adv().add(ADV.pRouteArray).readPointer();
    const W = ptr(E.MAP_WIDTH).readS32(), H = ptr(E.MAP_HEIGHT).readS32();
    const cells = [];
    let reachable = 0, today = true;
    for (let i = n - 1; i >= 0; i--) {
        const pc = first.add(i * 4).readPointer();
        const [x, y, z] = point(pc.readU32());
        const cost = pc.add(0x18).readU16();
        const arrow = route.isNull() ? 0 : route.add(((z * H + y) * W + x) * 2).readU16();
        const c = [x, y, z, cost];
        if (BR.debugRoute) c.push(pc.add(0x1c).readU16(), arrow, (pc.add(4).readU32() >>> 12) & 0xf);
        cells.push(c);
        if (today && arrow > 0 && arrow <= 25) reachable++; else today = false;
    }
    return { cells: cells, reachable: reachable };
}
// after the injected click was processed by Main
function afterCommand(c, res) {
    if (c.cmd === 'move_hero') {
        const h = heroPtr(c.hero);
        res.at = [h.readS16(), h.add(2).readS16(), h.add(4).readS16()];
        res.mp = h.add(HERO.mp).readS32();
        res.alive = h.add(HERO.owner).readS8() === me();
    } else if (c.cmd === 'end_turn') {
        res.current = curPlayer();
        res.date = readDate();
    }
    return res;
}
// runCommand returns the result, or {button: itemId, ...} to have Main process that button click
function runCommand(c) {
    const a = adv();
    switch (c.cmd) {
    case 'noop': return true;
    case 'select_hero':
        checkOwnHero(c.hero);
        selectHero(c.hero);
        return { hero: c.hero };
    case 'path': {
        const h = checkOwnHero(c.hero);
        checkTo(c.to);
        selectHero(c.hero);
        setTarget(h, c.to);
        a.add(ADV.seedingValid).writeU32(0);
        fnShowRoute(a, 1, 1, 1);      // as the first click on a map tile (ProcessMapSelect)
        const r = readRoute();
        r.hero = c.hero;
        r.to = c.to;
        return r;
    }
    case 'move_hero': {
        const h = checkOwnHero(c.hero);
        checkTo(c.to);
        selectHero(c.hero);
        const from = [h.readS16(), h.add(2).readS16(), h.add(4).readS16()];
        const mp0 = h.add(HERO.mp).readS32();
        setTarget(h, c.to);
        a.add(ADV.seedingValid).writeU32(0);
        trackPositions(false);
        // "move hero" button: advCommand 7 -> DoAdvCommand (walk, visit, battle) -> DoEvent
        return { button: 7, hero: c.hero, to: c.to, from: from, mp_before: mp0 };
    }
    case 'end_turn':
        // "end turn" button: move reminder (NormalDialog, forwarded) -> game::NextPlayer; AI turns follow in Main
        return { button: 0xc, before: curPlayer() };
    case 'open_town': {   // town screen of an own town (passthrough window)
        const tw = readTowns(mapInfo()).find(t => t.id === c.town);
        if (!tw) throw new Error('bad town id ' + c.town);
        if (tw.owner !== me()) throw new Error('town ' + c.town + ' is not yours');
        fnSetTownContext(a, c.town, 0, 1);    // as a click in the town list
        return { button: 7, adv: 3, town: c.town };
    }
    case 'open_hero': {   // hero screen of an own hero (passthrough window)
        checkOwnHero(c.hero);
        selectHero(c.hero);
        return { button: 7, adv: 2, hero: c.hero };
    }
    case 'system_menu':   // the "system options" button of the adventure screen (ProcessDeSelect item 10 ->
        // advManager::DoSystemOptions): load / save / restart / main menu / quit / settings; the window comes as a
        // passthrough window, "main menu" or "load" leave the adventure map (phase menu)
        return { button: 10 };
    case 'adventure_options':   // item 9 -> DoAdventureOptions: view world, puzzle, dig, scenario info
        return { button: 9 };
    case 'adv_key': {     // any adventure-map hotkey (DIK scan code): spellbook, kingdom overview, quest log, dig ...
        // a real key press in the engine's input queue (we are on the game thread, between two polls): the
        // window manager gives it to the adventure window's button hotkeys first, then advManager::Main
        // (ProcessKeyPress), as for a key on the keyboard; a window it opens comes as a passthrough window
        const scan = c.scan | 0;
        if (scan <= 0 || scan > 0xff) throw new Error('bad scan code ' + c.scan);
        const qual = (c.shift ? 1 : 0) | (c.ctrl ? 4 : 0) | (c.alt ? 32 : 0);
        pushMessage(1, scan, 0, qual, 0, 0);
        pushMessage(2, scan, 0, qual, 0, 0);
        return { scan: scan };
    }
    default: throw new Error('unknown command ' + c.cmd);
    }
}
let cmdSeq = 0;

// ---------- RPC (merged into game_agent.js exports) ----------
Object.assign(rpc.exports, {
    snapshotJob() { return runOnGameThread(() => JSON.stringify(snapshot())); },
    stateJob() { return runOnGameThread(() => JSON.stringify(dynState())); },
    snapshotNow() { return JSON.stringify(snapshot()); },
    stateNow() { return JSON.stringify(dynState()); },
    objectTypes() { return objectTypes(); },
    bridgeEnable(o) {
        o = o || {};
        BR.on = true;
        if (o.dialogTimeout) BR.dialogTimeout = o.dialogTimeout;
        if (o.defaults) BR.defaults = o.defaults;
        if (o.debugRoute !== undefined) BR.debugRoute = !!o.debugRoute;
        if (o.battles) BR.battles = o.battles;
        if (o.windows) BR.policy = o.windows;
        if (o.visible !== undefined) BR.visible = !!o.visible;
        if (o.mirrorMs) BR.mirrorMs = o.mirrorMs | 0;
        const gc = ptr(A.gConfig);
        // visible mode: the player's own walk speeds (animated walking in the game window); else 4 = no animation
        if (!BR.visible) { gc.writeU32(4); gc.add(4).writeU32(4); }   // walkSpeed[0] (AI), [1] (own)
        PTS.add(4 * PTI.cursorSwitch).writeS32(BR.visible ? 1 : 0);
        if (BR.visible) PTS.add(4 * PTI.virt).writeS32(0);
        PTS.add(4 * PTI.mirrorMs).writeS32(BR.visible ? Math.max(50, BR.mirrorMs) : 0);
        BR.digest = null;
        // QuickCombat: auto/manual -> the engine fights the human's battles itself (DoCombat turns it off for a
        // manual battle); game -> the player's own option (set below)
        gc.add(0x18).writeU32(0);                 // AutoSave off (never overwrite the user's AUTOSAVE)
        if (BR.battles === 'game') gc.add(0x24).writeU32(o.quickCombat === undefined ? gc.add(0x24).readU32() : o.quickCombat);
        else gc.add(0x24).writeU32(1);
        if (BR.phase === 'adventure') trackPositions(false);
        syncInputActive();
        return { on: true, phase: BR.phase, walk: [gc.readU32(), gc.add(4).readU32()], quick: gc.add(0x24).readU32(),
            moveReminder: gc.add(0x20).readU32(), battles: BR.battles, windows: BR.policy, visible: BR.visible };
    },
    bridgeDisable() {
        BR.on = false; BR.client = false; BR.pass = false;
        PTS.add(4 * PTI.mirrorMs).writeS32(0);
        syncInputActive();
        return true;
    },
    mirrorState() {   // visible mode diagnostics (test_client --scenario mirror)
        return { visible: BR.visible, period: PTS.add(4 * PTI.mirrorMs).readS32(), ticks: BR.mirrorStats.ticks,
            external: BR.mirrorStats.external, quiet: (() => { try { return mirrorQuiet(); } catch (e) { return String(e); } })(),
            nativeDialog: BR.nativeDlg, virt: PTS.add(4 * PTI.virt).readS32(), present: rpc.exports.present() };
    },
    phase() { return phaseMsg(); },
    setClient(on) { BR.client = !!on; return BR.client; },
    // a client that renders passthrough windows is connected (else windows fall back to the old policies)
    setPassthrough(on) { BR.pass = !!on; return BR.pass; },
    windowState() {
        return { stack: PT.stack.slice(), active: PTS.add(4 * PTI.active).readS32(), vmouse: [PTS.add(4).readS32(), PTS.add(8).readS32()],
            queued: (PTS.add(4 * PTI.tail).readS32() - PTS.add(4 * PTI.head).readS32()) & 63,
            delivered: PTS.add(4 * PTI.delivered).readS32(), pass: BR.pass, battles: BR.battles, since: PT.opened };
    },
    windowInput(o) {   // one input or a list (in order); returns how many were queued
        if (!Array.isArray(o)) return windowInput(o || {}) ? 1 : 0;
        let n = 0;
        for (const x of o) if (windowInput(x || {})) n++;
        return n;
    },
    frame(force) { return grabFrame(!!force); },
    dialogAnswer(id, answer) {
        const d = BR.nativeDlg;
        if (d !== null && d.id === id) {        // visible mode: click the answer in the game's own dialog
            if (d.by === 'client') return false;   // answered already
            answerNative(answer | 0);
            return true;
        }
        BR.answers[id] = answer | 0;
        return true;
    },
    pendingDialog() { return BR.pendingDialog; },
    cmd(c) {
        c.id = ++cmdSeq;
        cmdQueue.push(c);
        cmdShared.writeU32(1);
        return c.id;
    },
    queue() {
        return { n: cmdQueue.length, busy: BR.busy, can: canRunCommand(), cur: curPlayer(), depth: ptr(E.dialogDepth).readS32(),
            advStatus: adv().isNull() ? null : adv().add(0x34).readS32(), dialog: BR.pendingDialog };
    },
    debugEval(src) { return JSON.stringify(eval(src)); },   // development aid: evaluate JS in the agent
    probe() {
        const g = game();
        return { heroDisp402480: '0x' + ptr(0x402480).readU32().toString(16), heroDisp4317EE: '0x' + ptr(0x4317ee).readU32().toString(16),
            count4BF620: ptr(0x4bf620).readU32(), count4BB1EE: ptr(0x4bb1ee).readU32(),
            heroPool: heroPool().toString(), getHero0: runOnGameThread(() => fnGetHero(g, 0).toString()),
            gpGame: g.toString(), cur: curPlayer(), watch: ptr(E.giCurWatchPlayer).readS32(),
            advStatus: adv().isNull() ? null : adv().add(0x34).readS32(),
            gConfig: Array.from(new Uint32Array(ptr(A.gConfig).readByteArray(0x50))) };
    },
});
