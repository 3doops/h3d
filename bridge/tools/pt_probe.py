"""Window passthrough probe (PLAN 1.9): talk to a running bridge.py as a client that shows engine windows, run a few
actions, save the newest engine frame as JPEG. For finding click coordinates in engine windows step by step.

    python -I tools/pt_probe.py --url ws://127.0.0.1:8765 --do "cmd:open_town:2" --wait 2 --out ../out/pt/town.jpg
    python -I tools/pt_probe.py --do "click:400,300" --do "key:Esc" --do "sleep:1" --out ../out/pt/after.jpg
    python -I tools/pt_probe.py --do "rclick:100,200" --state      # also print the bridge queue / window state

Actions (--do, in order): cmd:NAME[:ID] (open_town:<town>, open_hero:<hero>, adv_key:<key>, end_turn, ...),
click:X,Y  rclick:X,Y (right button down, wait, up)  down:X,Y  up:X,Y  move:X,Y  key:NAME  sleep:SEC,
save:PATH (newest frame now), json:{...} (any message), eval:JS (bridge.py --debug: evaluate JS in the game agent).
The connection closes at the end; an open engine window stays open (the bridge re-sends it on the next connect).
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time


async def main(a):
    from websockets.asyncio.client import connect
    async with connect(a.url, max_size=2 ** 24) as ws:
        state = {'frame': None, 'win': [], 'log': []}

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                t = m.get('t')
                if t == 'window_frame':
                    state['frame'] = m
                elif t in ('window_open', 'window_close'):
                    state['log'].append(m)
                    print(t, json.dumps(m, ensure_ascii=False))
                elif t in ('event', 'cmd_done', 'error', 'dialog') or (t == 'debug' and (a.state or m.get('what') == 'eval')):
                    print(t, json.dumps(m, ensure_ascii=False)[:400])
                elif t == 'state':
                    me = 0
                    print('state res', m['players'][me]['res'])
                elif t == 'phase':
                    print(t, json.dumps(m, ensure_ascii=False))
                elif t == 'snapshot':
                    print('snapshot', m['map']['size'], 'x', m['map']['levels'], 'objects', len(m['objects']),
                          'me', m.get('me'), 'current', m.get('current'), 'why', m.get('why'))
        rt = asyncio.create_task(reader())
        await ws.send(json.dumps({'t': 'hello', 'client': 'pt_probe', 'caps': ['windows']}))
        await asyncio.sleep(a.settle)
        for act in a.do:
            kind, _, arg = act.partition(':')
            if kind == 'sleep':
                await asyncio.sleep(float(arg))
            elif kind == 'cmd':
                name, _, val = arg.partition(':')
                m = {'t': 'cmd', 'cmd': name}
                if name == 'open_town':
                    m['town'] = int(val)
                elif name in ('open_hero', 'select_hero'):
                    m['hero'] = int(val)
                elif name == 'adv_key':
                    m['key'] = val
                await ws.send(json.dumps(m))
            elif kind in ('click', 'down', 'up', 'move'):
                x, y = map(int, arg.split(','))
                await ws.send(json.dumps({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y, 'button': 'left',
                                          'action': 'move'}))
                if kind == 'click':
                    for act2 in ('down', 'up'):
                        await ws.send(json.dumps({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y,
                                                  'button': 'left', 'action': act2}))
                        await asyncio.sleep(0.15)
                elif kind != 'move':
                    await ws.send(json.dumps({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y,
                                              'button': 'left', 'action': kind}))
            elif kind == 'rclick':
                x, y = map(int, arg.split(','))
                for act2 in ('move', 'down'):
                    await ws.send(json.dumps({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y,
                                              'button': 'right', 'action': act2}))
                await asyncio.sleep(1.0)
                if a.out:
                    await asyncio.sleep(a.wait)
                    save(state, a.out.replace('.jpg', '_rclick.jpg'))
                await ws.send(json.dumps({'t': 'window_input', 'kind': 'mouse', 'x': x, 'y': y, 'button': 'right',
                                          'action': 'up'}))
            elif kind == 'json':          # any protocol message
                await ws.send(arg)
            elif kind == 'save':          # save the newest frame now (after a short settle)
                await asyncio.sleep(0.6)
                save(state, arg)
            elif kind == 'eval':          # bridge.py --debug only
                await ws.send(json.dumps({'t': 'debug_eval', 'src': arg}))
            elif kind == 'key':
                await ws.send(json.dumps({'t': 'window_input', 'kind': 'key', 'key': arg, 'action': 'press'}))
            await asyncio.sleep(0.25)
        await asyncio.sleep(a.wait)
        await ws.send(json.dumps({'t': 'window_refresh'}))
        await asyncio.sleep(0.6)
        if a.state:
            await ws.send(json.dumps({'t': 'debug_queue'}))
            await asyncio.sleep(0.5)
        if a.out:
            save(state, a.out)
        rt.cancel()


def save(state, path):
    f = state['frame']
    if f is None:
        print('no frame')
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(base64.b64decode(f['data']))
    print(f"frame #{f['seq']} {f['w']}x{f['h']} {f.get('name')} -> {path}")


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='ws://127.0.0.1:8765')
    ap.add_argument('--do', action='append', default=[])
    ap.add_argument('--wait', type=float, default=1.0, help='seconds after the last action before saving')
    ap.add_argument('--settle', type=float, default=1.5, help='seconds after connecting (snapshot) before acting')
    ap.add_argument('--out', help='save the newest frame (JPEG)')
    ap.add_argument('--state', action='store_true')
    t0 = time.time()
    asyncio.run(main(ap.parse_args()))
    print(f'{time.time() - t0:.1f}s')
