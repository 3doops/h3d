# 2D preview of a snapshot JSON (terrain, fog, roads, rivers, objects, heroes): python map_preview.py snap.json out.png  (needs pillow)
import json, sys
from PIL import Image, ImageDraw
s = json.load(open(sys.argv[1], encoding='utf-8'))
N = s['map']['size']; C = 12
col = {0:(140,100,60),1:(220,200,140),2:(70,140,50),3:(235,240,250),4:(70,90,60),5:(130,120,100),6:(90,70,60),7:(60,30,20),8:(30,70,150),9:(40,40,40)}
img = Image.new('RGB', (N*C*2+10, N*C), 'black'); d = ImageDraw.Draw(img)
for z in range(2):
    ox = z*(N*C+10)
    T = s['map']['terrain'][z]; F = s['fog'][z]; R = s['map']['road'][z]; V = s['map']['river'][z]
    for y in range(N):
        for x in range(N):
            c = col[T[y*N+x]]
            if not F[y*N+x]: c = tuple(v//3 for v in c)
            d.rectangle([ox+x*C, y*C, ox+x*C+C-1, y*C+C-1], fill=c)
            if R[y*N+x]: d.rectangle([ox+x*C+4, y*C+4, ox+x*C+7, y*C+7], fill=(160,110,60))
            if V[y*N+x]: d.rectangle([ox+x*C+3, y*C+3, ox+x*C+8, y*C+8], fill=(80,160,255))
    for o in s['objects']:
        if o['z'] != z: continue
        b = int(o['block'],16); v = int(o['visit'],16)
        t = o['type']
        oc = (0,90,0) if t in (137,155,135,199,119,153) else (100,100,100) if t in (134,147,136,210) else (255,0,255) if t==98 else (255,200,0) if t in (79,101,5) else (255,0,0) if t==54 else (0,200,200)
        for bit in range(48):
            if (b|v)>>bit & 1:
                cx, cy = o['x']-7+bit%8, o['y']-5+bit//8
                fill = (255,255,0) if v>>bit&1 else oc
                d.rectangle([ox+cx*C+2, cy*C+2, ox+cx*C+C-3, cy*C+C-3], fill=fill)
    for h in s['heroes']:
        if h['z']==z: d.ellipse([ox+h['x']*C, h['y']*C, ox+h['x']*C+C, h['y']*C+C], fill=(255,0,0) if h['owner']==0 else (0,0,255), outline='white')
img.save(sys.argv[2])
