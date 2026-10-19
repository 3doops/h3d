// Frida agent for h3hota.exe (SoD + HotA.dll, no HD mod). launch.py loads it right after spawn
// (AGENT_MODE 'spawn') or into a running game (AGENT_MODE 'attach': no startup hooks, only jobs/state/RPC).
//
// 1. Startup fixes so the game runs without a desktop (Windows session 0):
//    - CreateDPlayObject (0x5559A0) -> 0: DirectPlay is not installed; loading it hangs on a hidden
//      "install the DirectPlay feature?" prompt. CoCreateInstance of DirectPlay (network game in the menu) fails
//      at once for the same reason.
//    - GetDeviceCaps(BITSPIXEL) -> 16 for the exe, so the engine stays windowed instead of switching to
//      fullscreen 800x600x16 (SetDisplayMode fails with DDERR_UNSUPPORTED in session 0).
//    - DirectDraw: offscreen surfaces are created as RGB565 and every surface reports RGB565, so the
//      engine's screen bitmap (gpWindowManager->screenBitmap) is a plain 16-bit image; blits to the
//      32-bit primary fail (E_NOTIMPL) and are reported as success.
// 2. Automation: skips the main menu / game type windows, filters the Maps\ or GAMES\ file list down to one
//    file so the scenario dialog has a single entry, clicks/keys through the engine's input queue.
// 3. Game-thread jobs (C hook on inputManager::GetEvent), idle CPU throttle, state readers, RPC.
// 4. Visible mode (AGENT_VISIBLE, launch.py/bridge.py --visible, PLAN 4.6): the game window is a normal window on the
//    player's desktop (the default hides it), every frame the engine presents is also drawn into it with GDI (the
//    16-bit DirectDraw blit to a 32-bit desktop may fail), a Windows MessageBox is really shown, and minimizing the
//    window does not freeze the game (IsIconic -> 0: Process1WindowsMessage would block in GetMessage).
'use strict';

const MODE = typeof AGENT_MODE !== 'undefined' ? AGENT_MODE : 'spawn';
const VISIBLE = typeof AGENT_VISIBLE !== 'undefined' ? !!AGENT_VISIBLE : false;
const A = {
    gpGame: 0x699538, gpAdvManager: 0x6992B8, gpCombatManager: 0x699420, gpInputManager: 0x699530,
    gpWindowManager: 0x6992D0, gpExec: 0x699550, gConfig: 0x6987A8, gGameCommand: 0x697728, hwndApp: 0x699650,
    CreateDPlayObject: 0x5559A0, InitDirectDraw: 0x601AB0, GetEvent: 0x4EC660, SaveGame: 0x4BEB60,
    TMainMenu_DoModal: 0x4FBD70, TGameTypeWindow_DoModal: 0x4D5B20,
};
const cfg = { menu: null, gameType: null, fileFilter: null, inMainMenu: false, log: [], visible: VISIBLE };

const KEEP = [];   // CModules / callbacks that must stay alive
function log(s) { cfg.log.push(s); if (cfg.log.length > 200) cfg.log.shift(); send({ t: 'log', msg: s }); }
function u32(a) { return ptr(a).readU32(); }
function p32(a) { return ptr(a).readPointer(); }

// ---------- 4. visible mode: present into the game window with GDI ----------
// The engine draws into an offscreen RGB565 surface and blits the changed rectangle to the primary surface
// (FUN_00600430, from H3Redraw / WM_PAINT; the mouse cursor sprite is composited in at that moment). On a 32-bit
// desktop that 16-bit blit may fail (in session 0 it always does), so in visible mode a C listener on Blt/BltFast of
// the primary also copies the same rectangle from the source surface's DC into the window's client DC.
// PRESENT: [0] on, [1] primary surface, [2] presents, [3] GDI ok, [4] GDI failed, [5..8] last client dest x, y, w, h
const PRESENT = Memory.alloc(64);
for (let k = 0; k < 16; k++) PRESENT.add(4 * k).writeS32(0);
PRESENT.writeS32(VISIBLE ? 1 : 0);
const presentModule = new CModule(`
#include <gum/guminterceptor.h>
typedef struct { int left, top, right, bottom; } Rect;
typedef struct { int x, y; } Pt;
extern volatile int pv[16];
extern void * __attribute__((stdcall)) GetDC (void * hwnd);
extern int __attribute__((stdcall)) ReleaseDC (void * hwnd, void * hdc);
extern int __attribute__((stdcall)) ClientToScreen (void * hwnd, Pt * p);
extern int __attribute__((stdcall)) BitBlt (void * d, int x, int y, int w, int h, void * s, int sx, int sy, unsigned int rop);
extern int __attribute__((stdcall)) StretchBlt (void * d, int x, int y, int w, int h, void * s, int sx, int sy, int sw, int sh, unsigned int rop);
typedef long (__attribute__((stdcall)) * SurfGetDC) (void * self, void ** hdc);
typedef long (__attribute__((stdcall)) * SurfReleaseDC) (void * self, void * hdc);
static void present (void * src, int dx, int dy, int dw, int dh, const Rect * sr) {
  void * hwnd = *(void * volatile *) 0x699650;
  void ** vt;
  void * sdc = 0;
  void * wdc;
  Pt o;
  int sx = 0, sy = 0, sw = dw, sh = dh, ok;
  if (!hwnd || dw <= 0 || dh <= 0 || dw > 4096 || dh > 4096) return;
  if (sr) { sx = sr->left; sy = sr->top; sw = sr->right - sr->left; sh = sr->bottom - sr->top; }
  o.x = 0; o.y = 0;
  ClientToScreen (hwnd, &o);
  vt = *(void ***) src;
  if (((SurfGetDC) vt[17]) (src, &sdc) != 0 || !sdc) { pv[4]++; return; }   /* IDirectDrawSurface::GetDC */
  wdc = GetDC (hwnd);
  if (wdc) {
    if (sw == dw && sh == dh) ok = BitBlt (wdc, dx - o.x, dy - o.y, dw, dh, sdc, sx, sy, 0x00CC0020);
    else ok = StretchBlt (wdc, dx - o.x, dy - o.y, dw, dh, sdc, sx, sy, sw, sh, 0x00CC0020);
    if (ok) pv[3]++; else pv[4]++;
    ReleaseDC (hwnd, wdc);
  } else pv[4]++;
  ((SurfReleaseDC) vt[26]) (src, sdc);                                        /* IDirectDrawSurface::ReleaseDC */
  pv[2]++;
  pv[5] = dx - o.x; pv[6] = dy - o.y; pv[7] = dw; pv[8] = dh;
}
void blt_enter (GumInvocationContext * ic) {
  void * self = gum_invocation_context_get_nth_argument (ic, 0);
  Rect * dr = (Rect *) gum_invocation_context_get_nth_argument (ic, 1);
  void * src = gum_invocation_context_get_nth_argument (ic, 2);
  Rect * sr = (Rect *) gum_invocation_context_get_nth_argument (ic, 3);
  if (!pv[0] || !src || self != (void *) pv[1]) return;
  if (dr) present (src, dr->left, dr->top, dr->right - dr->left, dr->bottom - dr->top, sr);
  else if (sr) {
    Pt o; o.x = 0; o.y = 0;
    ClientToScreen (*(void * volatile *) 0x699650, &o);
    present (src, o.x + sr->left, o.y + sr->top, sr->right - sr->left, sr->bottom - sr->top, sr);
  }
}
void bltfast_enter (GumInvocationContext * ic) {
  void * self = gum_invocation_context_get_nth_argument (ic, 0);
  int x = (int) gum_invocation_context_get_nth_argument (ic, 1);
  int y = (int) gum_invocation_context_get_nth_argument (ic, 2);
  void * src = gum_invocation_context_get_nth_argument (ic, 3);
  Rect * sr = (Rect *) gum_invocation_context_get_nth_argument (ic, 4);
  if (!pv[0] || !src || !sr || self != (void *) pv[1]) return;
  present (src, x, y, sr->right - sr->left, sr->bottom - sr->top, sr);
}
`, { pv: PRESENT, GetDC: Process.getModuleByName('user32.dll').getExportByName('GetDC'),
     ReleaseDC: Process.getModuleByName('user32.dll').getExportByName('ReleaseDC'),
     ClientToScreen: Process.getModuleByName('user32.dll').getExportByName('ClientToScreen'),
     BitBlt: Process.getModuleByName('gdi32.dll').getExportByName('BitBlt'),
     StretchBlt: Process.getModuleByName('gdi32.dll').getExportByName('StretchBlt') });

// ---------- 1. startup fixes (spawn mode only) ----------
function installStartupHooks() {
    Interceptor.replace(ptr(A.CreateDPlayObject), new NativeCallback(() => 0, 'int', [], 'mscdecl'));
    // Network game (main menu -> multiplayer -> TCP/IP, IPX, ...): CDPlay::Create (0x4968B0) and CDPlayLobby::Init
    // (0x4985D0) CoCreateInstance DirectPlay / DirectPlayLobby, which hangs the same way. Answer "class not
    // registered": the game then reports that the connection is not available and stays in the menu.
    {
        const DPLAY_CLSIDS = [0x643E28, 0x643DC8];   // CLSID_DirectPlay, CLSID_DirectPlayLobby (exe .rdata)
        const co = Process.getModuleByName('ole32.dll').getExportByName('CoCreateInstance');
        const origCo = new NativeFunction(co, 'int', ['pointer', 'pointer', 'uint', 'pointer', 'pointer'], 'stdcall');
        Interceptor.replace(co, new NativeCallback(function (clsid, outer, ctx, iid, ppv) {
            if (DPLAY_CLSIDS.indexOf(clsid.toUInt32()) >= 0) {
                if (!ppv.isNull()) ppv.writePointer(NULL);
                log('CoCreateInstance(DirectPlay) -> REGDB_E_CLASSNOTREG (network game not available)');
                return 0x80040154 | 0;
            }
            return origCo(clsid, outer, ctx, iid, ppv);
        }, 'int', ['pointer', 'pointer', 'uint', 'pointer', 'pointer'], 'stdcall'));
    }
    // ... but the network buttons of the multiplayer window (TMultiPlayerWindow::OnWidgetDeselect 0x50F740: 0x65
    // online, 0x67 IPX, 0x68 TCP/IP, 0x69 modem, 0x6A direct connection) reach InitConnection (0x555AD0), which,
    // with no DirectPlay object, calls ShutDown("DirectPlay Error") -> an invisible MessageBox, then exit. Answer
    // these buttons with a message of the game itself instead; hot-seat (0x66) and the rest stay as they are.
    {
        const origMp = new NativeFunction(ptr(0x50F740), 'int', ['pointer', 'int', 'pointer'], 'thiscall');
        const fnMsg = new NativeFunction(ptr(0x4F6C00), 'void', ['pointer', 'int', 'int', 'int', 'int', 'int', 'int',
            'int', 'int', 'int', 'int', 'int'], 'fastcall');
        let text = null;   // made on first use (CP1251_HI below is not defined yet at spawn time)
        const TEXT = 'Сетевая игра здесь недоступна: DirectPlay отключён (игра запущена клиентом HotA 3D без ' +
            'рабочего стола).' + String.fromCharCode(10, 10) + 'На одном компьютере можно играть вдвоём и больше: ' +
            '«Новая игра» -> «Несколько игроков» -> «Один компьютер» (hot-seat).';
        const NET = { 0x65: 'online', 0x67: 'IPX', 0x68: 'TCP/IP', 0x69: 'modem', 0x6a: 'direct' };
        Interceptor.replace(ptr(0x50F740), new NativeCallback(function (self, item, close) {
            if (NET[item] === undefined) return origMp(self, item, close);
            log('multiplayer: ' + NET[item] + ' not available (DirectPlay stub)');
            if (text === null) text = allocCp1251(TEXT);
            fnMsg(text, 1, -1, -1, -1, 0, -1, 0, -1, 0, -1, 0);
            return 1;
        }, 'int', ['pointer', 'int', 'pointer'], 'thiscall'));
    }
    // Nobody sees a Windows MessageBox in session 0 and it would block the game forever (ShutDown's "Unexpected
    // Program Termination", HotA errors ...): log the text and answer OK at once. Visible mode: the player sees it on
    // the desktop, so it is shown for real (the bridge still forwards the text).
    for (const name of ['MessageBoxA', 'MessageBoxW']) {
        const f = Process.getModuleByName('user32.dll').getExportByName(name);
        const orig = new NativeFunction(f, 'int', ['pointer', 'pointer', 'pointer', 'uint'], 'stdcall');
        Interceptor.replace(f, new NativeCallback(function (hwnd, txt, cap, type) {
            let t = '', c = '';
            try { t = name.endsWith('W') ? txt.readUtf16String() : cstr(txt, 1024); } catch (e) { }
            try { c = name.endsWith('W') ? cap.readUtf16String() : cstr(cap, 128); } catch (e) { }
            if (VISIBLE) {
                log('MessageBox [' + c + '] ' + t + ' (shown)');
                send({ t: 'messagebox', caption: c, text: t, shown: true });
                return orig(hwnd, txt, cap, type);
            }
            log('MessageBox [' + c + '] ' + t + ' -> OK');
            send({ t: 'messagebox', caption: c, text: t });
            return 1;
        }, 'int', ['pointer', 'pointer', 'pointer', 'uint'], 'stdcall'));
    }

    // The game window. WinMain creates it with WS_VISIBLE (style 0x10CA0000, no ShowWindow call); in Windows session
    // 0 nobody sees it anyway, on a real desktop the hidden mode keeps it off the screen: WS_VISIBLE is dropped and
    // the ShowWindow(SW_RESTORE) calls of the game (hot-seat "next player", ...) become SW_HIDE. Visible mode leaves
    // it as the game made it (a normal captioned 800x600 window at gConfig.ScreenX/Y).
    {
        const u32dll = Process.getModuleByName('user32.dll');
        Interceptor.attach(u32dll.getExportByName('CreateWindowExA'), {
            onEnter(a) {
                if (VISIBLE || a[1].compare(ptr(0x10000)) < 0) return;
                let cls = '';
                try { cls = a[1].readAnsiString(); } catch (e) { }
                if (cls !== 'Heroes III') return;
                a[3] = ptr(a[3].toUInt32() & ~0x10000000);   // WS_VISIBLE
                log('game window created hidden');
            },
        });
        Interceptor.attach(u32dll.getExportByName('ShowWindow'), {
            onEnter(a) { if (!VISIBLE && a[0].equals(ptr(A.hwndApp).readPointer())) a[1] = ptr(0); },
        });
        // minimized window: the engine would stop in GetMessage (Process1WindowsMessage) and stop presenting; with the
        // Godot client next to it the game must go on, so the game window never counts as minimized. Called on every
        // message pump (~10^5/s idle): a C listener.
        const iconic = new CModule(`
#include <gum/guminterceptor.h>
typedef struct { int app; } State;
void onEnter (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  s->app = gum_invocation_context_get_nth_argument (ic, 0) == *(void * volatile *) 0x699650;
}
void onLeave (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  if (s->app) gum_invocation_context_replace_return_value (ic, (gpointer) 0);
}
`);
        KEEP.push(iconic);
        Interceptor.attach(u32dll.getExportByName('IsIconic'), iconic);
    }

    {
        const exe = Process.getModuleByName('h3hota.exe');
        const gdc = Process.getModuleByName('gdi32.dll').getExportByName('GetDeviceCaps');
        Interceptor.attach(gdc, {
            onEnter(a) { this.fix = a[1].toInt32() === 12 && exe.base.compare(this.returnAddress) <= 0 && exe.base.add(exe.size).compare(this.returnAddress) > 0; },
            onLeave(r) { if (this.fix) r.replace(ptr(16)); },
        });
    }
    Interceptor.attach(ptr(A.InitDirectDraw), { onEnter() { ptr(A.gConfig + 0x60).writeU32(0); } });  // gConfig.FullScreen = 0

    function write565(pf) {
        pf.add(4).writeU32(0x40); pf.add(8).writeU32(0); pf.add(12).writeU32(16);
        pf.add(16).writeU32(0xF800); pf.add(20).writeU32(0x07E0); pf.add(24).writeU32(0x001F); pf.add(28).writeU32(0);
    }
    {
        let ddHooked = false, surfHooked = false;
        const ddc = Process.getModuleByName('ddraw.dll').getExportByName('DirectDrawCreate');
        Interceptor.attach(ddc, {
            onEnter(a) { this.pp = a[1]; },
            onLeave(r) {
                if (r.toInt32() !== 0 || ddHooked) return;
                ddHooked = true;
                const vt = this.pp.readPointer().readPointer();
                Interceptor.attach(vt.add(0x18).readPointer(), {  // IDirectDraw::CreateSurface
                    onEnter(a) {
                        const d = a[1];
                        this.pps = a[2];
                        this.primary = (d.add(0x68).readU32() & 0x200) !== 0;   // DDSCAPS_PRIMARYSURFACE
                        if (d.add(0x68).readU32() & 0x40) {  // DDSCAPS_OFFSCREENPLAIN -> force RGB565
                            d.add(4).writeU32(d.add(4).readU32() | 0x1000);
                            d.add(0x48).writeU32(32);
                            write565(d.add(0x48));
                        }
                    },
                    onLeave(r) {
                        if (r.toInt32() !== 0) return;
                        if (this.primary) PRESENT.add(4).writePointer(this.pps.readPointer());
                        if (surfHooked) return;
                        surfHooked = true;
                        const svt = this.pps.readPointer().readPointer();
                        Interceptor.attach(svt.add(0x54).readPointer(), {  // GetPixelFormat
                            onEnter(a) { this.pf = a[1]; }, onLeave() { write565(this.pf); },
                        });
                        for (const off of [0x14, 0x1c, 0x2c]) {  // Blt, BltFast, Flip: nobody sees the primary
                            Interceptor.attach(svt.add(off).readPointer(), { onLeave(r) { if (r.toInt32() !== 0) r.replace(ptr(0)); } });
                        }
                        // visible mode: what the engine blits to the primary surface is also drawn into the window
                        Interceptor.attach(svt.add(0x14).readPointer(), { onEnter: presentModule.blt_enter });
                        Interceptor.attach(svt.add(0x1c).readPointer(), { onEnter: presentModule.bltfast_enter });
                    },
                });
            },
        });
    }

    // ---------- 2. automation ----------
    // Main menu and "game type" windows: return the configured choice without running the window.
    // Main menu: 0x65 new game, 0x66 load game. Game type: 100 single scenario.
    const origMainMenu = new NativeFunction(ptr(A.TMainMenu_DoModal), 'int', ['pointer'], 'thiscall');
    Interceptor.replace(ptr(A.TMainMenu_DoModal), new NativeCallback(function (self) {
        if (cfg.menu === null) {
            cfg.inMainMenu = true;
            try { return origMainMenu(self); } finally { cfg.inMainMenu = false; }
        }
        log('main menu -> 0x' + cfg.menu.toString(16));
        p32(A.gpWindowManager).add(0x38).writeU32(cfg.menu);
        cfg.menu = null;
        return 0;
    }, 'int', ['pointer'], 'thiscall'));
    const origGameType = new NativeFunction(ptr(A.TGameTypeWindow_DoModal), 'int', ['pointer'], 'thiscall');
    Interceptor.replace(ptr(A.TGameTypeWindow_DoModal), new NativeCallback(function (self) {
        if (cfg.gameType === null) return origGameType(self);
        log('game type -> ' + cfg.gameType);
        p32(A.gpWindowManager).add(0x38).writeU32(cfg.gameType);
        return 0;
    }, 'int', ['pointer'], 'thiscall'));

    // File list filter: while cfg.fileFilter is set, FindFirstFileA/FindNextFileA over Maps\ or GAMES\ only
    // return the file with that name (case-insensitive).
    {
        const k32 = Process.getModuleByName('kernel32.dll');
        const findNext = new NativeFunction(k32.getExportByName('FindNextFileA'), 'int', ['pointer', 'pointer'], 'stdcall');
        const handles = new Set();
        function wanted(fd) {
            const name = fd.add(44).readAnsiString();
            return name.toLowerCase() === cfg.fileFilter.toLowerCase() || name === '.' || name === '..' ||
                (fd.readU32() & 0x10) !== 0;
        }
        function advance(h, fd) {
            while (!wanted(fd)) { if (!findNext(h, fd)) return false; }
            return true;
        }
        Interceptor.attach(k32.getExportByName('FindFirstFileA'), {
            onEnter(a) {
                this.pat = a[0].readAnsiString() || '';
                this.fd = a[1];
                // HotA lists maps with FindFirstFileA("*.h3m") after changing into Maps\, saves with "*.gm?"
                this.on = cfg.fileFilter !== null && /(^|\\)\*\.(h3m|gm[\d?*])$/i.test(this.pat);
                if (this.on) log('file list ' + this.pat + ' -> only ' + cfg.fileFilter);
            },
            onLeave(r) {
                if (!this.on || r.toInt32() === -1) return;
                handles.add(r.toString());
                if (!advance(r, this.fd)) {
                    // nothing matched: make the caller's next FindNextFile fail; return the handle with a dummy name
                    this.fd.add(44).writeAnsiString('.');
                    this.fd.writeU32(0x10);
                }
            },
        });
        Interceptor.attach(k32.getExportByName('FindNextFileA'), {
            onEnter(a) { this.h = a[0]; this.fd = a[1]; this.on = handles.has(a[0].toString()); },
            onLeave(r) {
                if (!this.on || r.toInt32() === 0) return;
                if (!advance(this.h, this.fd)) r.replace(ptr(0));
            },
        });
        Interceptor.attach(k32.getExportByName('FindClose'), { onEnter(a) { handles.delete(a[0].toString()); } });
    }
}
if (MODE === 'spawn') installStartupHooks();

// Engine input queue: inputManager.iBuffer[64] at +0x38 (message = 8 ints), iHead +0x838, iTail +0x83C.
function pushMessage(cmd, sub, item, qual, x, y) {
    const im = p32(A.gpInputManager);
    if (im.isNull()) return false;
    const tail = im.add(0x83C).readS32();
    const m = im.add(0x38 + tail * 32);
    m.writeU32(cmd); m.add(4).writeS32(sub); m.add(8).writeS32(item); m.add(12).writeU32(qual);
    m.add(16).writeS32(x); m.add(20).writeS32(y); m.add(24).writeU32(0); m.add(28).writeU32(0);
    im.add(0x83C).writeS32((tail + 1) & 63);
    return true;
}

// ---------- game-thread jobs + idle throttle ----------
// Engine functions must run on the game thread. A tiny C hook on inputManager::GetEvent (polled by every
// dialog/manager loop, ~10^5 calls/s when idle) calls into JS only when a job is queued.
// The same hook sleeps 1 ms after every `idle_polls` consecutive empty polls: the plain exe busy-waits and
// would keep a core at ~75% while idle (HD mod has its own "SmartReduceUsage"; we run without HD).
const jobs = [];
// CModule statics live in read-only memory, so all mutable state is in this block:
// [0] pending job flag, [1] idle_polls (0 = never sleep), [2] current run of empty polls
const shared = Memory.alloc(12);
const pending = shared, idlePolls = shared.add(4), emptyPolls = shared.add(8);
idlePolls.writeU32(0);  // launch.py enables it once the adventure map is up (it slows down menus/videos)
const jsTick = new NativeCallback(function () {
    while (jobs.length) {
        const j = jobs.shift();
        try { j.result = j.fn(); } catch (e) { j.error = e.stack || String(e); }
        j.done = true;
    }
}, 'void', []);
const tickModule = new CModule(`
#include <gum/guminterceptor.h>
extern volatile unsigned int pending;
extern volatile unsigned int idle_polls;
extern volatile unsigned int empty_polls;
extern void js_tick (void);
extern void __attribute__((stdcall)) Sleep (unsigned int ms);
typedef struct { unsigned int * msg; } State;
void onEnter (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  s->msg = (unsigned int *) gum_invocation_context_get_nth_argument (ic, 0);
  if (pending) { pending = 0; js_tick (); }
}
void onLeave (GumInvocationContext * ic) {
  State * s = GUM_IC_GET_INVOCATION_DATA (ic, State);
  if (s->msg[0] != 0) { empty_polls = 0; return; }
  if (idle_polls != 0 && ++empty_polls >= idle_polls) { empty_polls = 0; Sleep (1); }
}
`, { pending: pending, idle_polls: idlePolls, empty_polls: emptyPolls, js_tick: jsTick,
     Sleep: Process.getModuleByName('kernel32.dll').getExportByName('Sleep') });
Interceptor.attach(ptr(A.GetEvent), tickModule);

let jobSeq = 0;
const jobById = {};
function runOnGameThread(fn) {
    const j = { id: ++jobSeq, fn: fn, done: false };
    jobs.push(j); jobById[j.id] = j;
    pending.writeU32(1);
    return j.id;
}

const SaveGame = new NativeFunction(ptr(A.SaveGame), 'void', ['pointer', 'pointer', 'int', 'int', 'int', 'int'], 'thiscall');

// ---------- state ----------
// Offsets in game (*gpGame), SoD layout from NH3API (analysis/nh3api_types.json), checked on HotA 1.8.0.
const G = {
    day: 0x1f63e, week: 0x1f640, month: 0x1f642, filename: 0x1f4d5, mapSize: 0x1fb70 + 0xd4, twoLevels: 0x1fb70 + 0xd8,
    player: 0x20ad0, playerSize: 0x168, heroPoolSoD: 0x21620, heroSize: 0x492,
};
// HotA moves the hero array (156 -> 215 heroes) out of game and patches every
// "lea r, [game + id*0x492 + 0x21620]" with the runtime displacement; read it back from one patched site.
const HERO_DISP_SITE = 0x402480;   // disp32 of "lea esi, [eax + edx*2 + disp]" in FUN_00402450
const HERO_COUNT_SITE = 0x4bb1ee;  // imm32 of "cmp esi, 0x9c" (HotA: 0xd7) in game::GetStartingHeroId
function heroPool() {
    const g = p32(A.gpGame);
    return g.isNull() ? null : g.add(ptr(HERO_DISP_SITE).readU32());
}
function heroCount() {
    const v = ptr(HERO_COUNT_SITE).readU32();
    return v >= 156 && v < 1024 ? v : 156;
}
const VT = {  // window vtables -> names (NH3API / H3API)
    '0x63a5e4': 'adventureMap', '0x63db40': 'dialogBox', '0x641cbc': 'selectScenario',
};
const CP1251_HI = 'ЂЃ‚ѓ„…†‡€‰Љ‹ЊЌЋЏђ‘’“”•–—\u0098™љ›њќћџ ЎўЈ¤Ґ¦§Ё©Є«¬­®Ї°±Ііґµ¶·ё№є»јЅѕї';
function allocCp1251(str) {   // JS string -> cp1251 bytes for the engine (Russian HotA)
    const out = [];
    for (const ch of str) {
        const c = ch.charCodeAt(0);
        if (c < 0x80) out.push(c);
        else if (c >= 0x410 && c <= 0x44F) out.push(0xC0 + c - 0x410);
        else if (c === 0x401) out.push(0xA8);
        else if (c === 0x451) out.push(0xB8);
        else { const k = CP1251_HI.indexOf(ch); out.push(k >= 0 ? 0x80 + k : 0x3F); }
    }
    out.push(0);
    const m = Memory.alloc(out.length);
    m.writeByteArray(out);
    return m;
}
function cstr(p, max) {  // engine strings are cp1251 in the Russian HotA
    const b = new Uint8Array(p.readByteArray(max || 64));
    let s = '';
    for (const c of b) {
        if (c === 0) break;
        s += c < 0x80 ? String.fromCharCode(c) : c < 0xC0 ? CP1251_HI[c - 0x80] : String.fromCharCode(0x410 + c - 0xC0);
    }
    return s;
}
function managers() {
    const ex = p32(A.gpExec);
    const out = [];
    if (ex.isNull()) return out;
    for (let m = ex.readPointer(), n = 0; !m.isNull() && n < 32; m = m.add(4).readPointer(), n++)
        out.push(m.add(0x14).readCString());
    return out;
}
function windows() {  // heroWindowManager.headWindow (+0x58) is the top window, nextWindow +8
    const wm = p32(A.gpWindowManager);
    const out = [];
    if (wm.isNull()) return out;
    for (let w = wm.add(0x58).readPointer(), n = 0; !w.isNull() && n < 32; w = w.add(8).readPointer(), n++) {
        const vt = w.readPointer().toString();
        out.push({ name: VT[vt] || vt, x: w.add(0x18).readS32(), y: w.add(0x1c).readS32(), w: w.add(0x20).readS32(), h: w.add(0x24).readS32() });
    }
    return out;
}
function players() {
    const g = p32(A.gpGame);
    const out = [];
    for (let i = 0; i < 8; i++) {
        const p = g.add(G.player + i * G.playerSize);
        const heroes = [];
        const n = p.add(1).readS8();
        for (let k = 0; k < Math.max(0, Math.min(n, 8)); k++) heroes.push(p.add(8 + 4 * k).readS32());
        const res = [];
        for (let r = 0; r < 7; r++) res.push(p.add(0x9c + 4 * r).readS32());
        out.push({ id: i, color: p.readS8(), alive: !g.add(0x1f636 + i).readU8(), numHeroes: n, heroes: heroes,
            numTowns: p.add(0x3e).readS8(), human: p.add(0xe2).readU8() !== 0, local: p.add(0xe1).readU8() !== 0,
            res: res, name: cstr(p.add(0xcc), 21) });
    }
    return out;
}
function heroes(all) {  // heroes placed on the map (owner >= 0 or visible), or all with all=true
    const base = heroPool();
    const out = [];
    if (base === null) return out;
    for (let i = 0, n = heroCount(); i < n; i++) {
        const h = base.add(i * G.heroSize);
        const owner = h.add(0x22).readS8(), x = h.readS16();
        if (!all && owner < 0) continue;
        out.push({ id: h.add(0x1a).readS32(), name: cstr(h.add(0x23), 13), owner: owner, x: x, y: h.add(2).readS16(),
            z: h.add(4).readS16(), class: h.add(0x30).readS32(), portrait: h.add(0x34).readU8(),
            mp: h.add(0x4d).readS32(), mp_max: h.add(0x49).readS32(), mana: h.add(0x18).readS16(),
            level: h.add(0x55).readS16(), exp: h.add(0x51).readS32() });
    }
    return out;
}
function terrainStats() {  // tiles per terrain type and level: NewfullMap.cellData (+0xd0), NewmapCell 0x26 bytes
    const g = p32(A.gpGame);
    const size = g.add(G.mapSize).readS32(), levels = g.add(G.twoLevels).readU8() ? 2 : 1;
    const cells = g.add(0x1fb70 + 0xd0).readPointer();
    const out = [];
    for (let z = 0; z < levels; z++) {
        const raw = new Uint8Array(cells.add(z * size * size * 0x26).readByteArray(size * size * 0x26));
        const hist = {};
        for (let i = 0; i < size * size; i++) { const t = raw[i * 0x26 + 4]; hist[t] = (hist[t] || 0) + 1; }
        out.push(hist);
    }
    return out;
}
function phase() {
    const m = managers(), w = windows();
    const top = w.length ? w[0].name : null;
    if (m.indexOf('combatManager') >= 0) return 'combat';
    if (m.indexOf('advManager') >= 0) return top === 'adventureMap' ? 'adventure' : 'adventure-dialog';
    if (top === 'selectScenario') return 'select-scenario';
    return cfg.inMainMenu ? 'main-menu' : 'menu';
}

// ---------- 3. RPC ----------
function screenBitmap() {
    const wm = p32(A.gpWindowManager);
    if (wm.isNull()) return null;
    const bmp = wm.add(0x40).readPointer();
    if (bmp.isNull()) return null;
    return { w: bmp.add(0x24).readS32(), h: bmp.add(0x28).readS32(), pitch: bmp.add(0x2c).readS32(), map: bmp.add(0x30).readPointer() };
}

rpc.exports = {
    configure(o) { Object.assign(cfg, o); return cfg; },
    log() { return cfg.log; },
    shot() {
        const b = screenBitmap();
        if (b === null) return null;
        send({ t: 'shot', w: b.w, h: b.h, pitch: b.pitch }, b.map.readByteArray(b.pitch * b.h));
        return [b.w, b.h, b.pitch];
    },
    hwnd() { return u32(A.hwndApp); },
    // visible mode: GDI presents into the game window (PLAN 4.6)
    present() {
        const r = k => PRESENT.add(4 * k).readS32();
        return { visible: VISIBLE, on: r(0) !== 0, primary: PRESENT.add(4).readPointer().toString(), presents: r(2),
            gdi_ok: r(3), gdi_failed: r(4), last: [r(5), r(6), r(7), r(8)] };
    },
    push(cmd, sub, item, qual, x, y) { return pushMessage(cmd, sub, item, qual, x || 0, y || 0); },
    // left click at screen coordinates (800x600 engine screen)
    click(x, y) { return pushMessage(8, x, y, 0, x, y) && pushMessage(16, x, y, 0, x, y); },
    // key press by scan code (Enter 0x1C, Esc 0x01, Space 0x39, letters: Q 0x10 ... M 0x32)
    key(scan, qual) { return pushMessage(1, scan, 0, qual || 0, 0, 0) && pushMessage(2, scan, 0, qual || 0, 0, 0); },
    job(id) {
        const j = jobById[id];
        if (!j) return null;
        if (j.done) delete jobById[id];
        return { done: j.done, result: j.result, error: j.error };
    },
    // Save the current game to GAMES\<name>.GM1 (engine adds the extension).
    save(name) {
        return runOnGameThread(() => { SaveGame(p32(A.gpGame), Memory.allocAnsiString(name), 1, 1, 1, 0); return name; });
    },
    state() {
        const g = p32(A.gpGame);
        return {
            phase: phase(), managers: managers(), windows: windows(),
            date: g.isNull() ? null : { month: g.add(G.month).readU16(), week: g.add(G.week).readU16(), day: g.add(G.day).readU16() },
            mapSize: g.isNull() ? 0 : g.add(G.mapSize).readS32(), levels: g.isNull() ? 0 : (g.add(G.twoLevels).readU8() ? 2 : 1),
            file: g.isNull() ? '' : cstr(g.add(G.filename), 64), visible: VISIBLE,
        };
    },
    players() { return players(); },
    heroes(all) { return heroes(!!all); },
    terrainStats() { return terrainStats(); },
    // consecutive empty GetEvent polls before a 1 ms sleep; 0 disables the throttle
    idlePolls(n) { if (n !== undefined && n !== null) idlePolls.writeU32(n); return idlePolls.readU32(); },
    addresses() {
        return { gpGame: p32(A.gpGame).toString(), heroPool: String(heroPool()), heroCount: heroCount(),
            heroDisp: '0x' + ptr(HERO_DISP_SITE).readU32().toString(16) };
    },
    globals() {
        const r = {};
        for (const n of ['gpGame', 'gpAdvManager', 'gpCombatManager', 'gpInputManager', 'gpWindowManager', 'gpExec'])
            r[n] = p32(A[n]).toString();
        r.gGameCommand = ptr(A.gGameCommand).readS32();
        return r;
    },
};
