#!/usr/bin/env python3
"""Fetch only the export templates of one platform (Windows x64 / Linux x86_64) of Godot from the official
release .tpz, never storing the .tpz.

The .tpz (~1.3 GB zip) is read straight from GitHub: a range request gets the zip central directory, then the
archive is streamed once through SHA-512 (checked against the release SHA512-SUMS.txt) while only the wanted
members are inflated to disk (CRC-32 checked). Disk use = the extracted files only (~100 MB per platform).

    python -I godot/tools/fetch_export_templates.py                   # 4.7.2 Windows x64 -> _dl/godot-templates-4.7.2/
    python -I godot/tools/fetch_export_templates.py --platform linux  # 4.7.2 Linux x86_64 (linux_release.x86_64)
    python -I godot/tools/fetch_export_templates.py --version 4.7.2 --out DIR --members windows_release_x86_64.exe
    python -I godot/tools/fetch_export_templates.py --no-verify       # range requests only (~100 MB traffic)
    python -I godot/tools/fetch_export_templates.py --list            # list the archive (central directory only)
"""
import argparse
import hashlib
import os
import re
import struct
import sys
import urllib.request
import zlib

BASE = "https://github.com/godotengine/godot/releases/download/{v}-stable/"
TPZ = "Godot_v{v}-stable_export_templates.tpz"
UA = {"User-Agent": "hota3d-fetch-templates/1"}
PLATFORM_MEMBERS = {
    "windows": ["windows_release_x86_64.exe", "windows_release_x86_64_console.exe"],  # + console wrapper (0.2 MB)
    "linux": ["linux_release.x86_64"],
}


def http(url, rng=None):
    h = dict(UA)
    if rng:
        h["Range"] = rng
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60)


def fetch_range(url, start, end):
    with http(url, "bytes=%d-%d" % (start, end)) as r:
        data = r.read()
    if len(data) != end - start + 1:
        raise SystemExit("range %d-%d: got %d bytes (server ignores Range?)" % (start, end, len(data)))
    return data


def central_directory(url):
    # the release CDN (Azure blob) rejects suffix ranges "bytes=-N": probe the size with bytes=0-0 first
    with http(url, "bytes=0-0") as r:
        cr = r.headers.get("Content-Range", "")
        r.read()
    m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
    if not m:
        raise SystemExit("no Content-Range in reply: %r" % cr)
    total = int(m.group(3))
    tail_start = max(0, total - 65558)
    tail = fetch_range(url, tail_start, total - 1)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise SystemExit("zip end of central directory not found")
    _, _, _, _, n, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF:          # zip64
        j = tail.rfind(b"PK\x06\x07", 0, i)
        z64_off = struct.unpack("<Q", tail[j + 8:j + 16])[0]
        rec = fetch_range(url, z64_off, z64_off + 55)
        n, cd_size, cd_off = struct.unpack("<QQQ", rec[32:56])
    if cd_off >= tail_start:
        cd = tail[cd_off - tail_start:cd_off - tail_start + cd_size]
    else:
        cd = fetch_range(url, cd_off, cd_off + cd_size - 1)
    entries = {}
    p = 0
    for _ in range(n):
        if cd[p:p + 4] != b"PK\x01\x02":
            raise SystemExit("bad central directory entry at %d" % p)
        (method, crc, csize, usize, nlen, xlen, clen, lho) = (
            struct.unpack("<H", cd[p + 10:p + 12])[0], *struct.unpack("<III", cd[p + 16:p + 28]),
            *struct.unpack("<HHH", cd[p + 28:p + 34]), struct.unpack("<I", cd[p + 42:p + 46])[0])
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = cd[p + 46 + nlen:p + 46 + nlen + xlen]
        if 0xFFFFFFFF in (csize, usize, lho):           # zip64 extra field
            q = 0
            while q + 4 <= len(extra):
                hid, hlen = struct.unpack("<HH", extra[q:q + 4])
                if hid == 1:
                    vals = list(struct.unpack("<%dQ" % (hlen // 8), extra[q + 4:q + 4 + hlen - hlen % 8]))
                    if usize == 0xFFFFFFFF: usize = vals.pop(0)
                    if csize == 0xFFFFFFFF: csize = vals.pop(0)
                    if lho == 0xFFFFFFFF: lho = vals.pop(0)
                q += 4 + hlen
        entries[name] = dict(name=name, method=method, crc=crc, csize=csize, usize=usize, lho=lho)
        p += 46 + nlen + xlen + clen
    return total, entries


class Member:
    def __init__(self, e, path):
        self.e, self.path = e, path
        self.hdr = b""
        self.data_start = None
        self.out = open(path + ".part", "wb")
        self.dec = zlib.decompressobj(-15) if e["method"] == 8 else None
        if e["method"] not in (0, 8):
            raise SystemExit("%s: unsupported compression method %d" % (e["name"], e["method"]))
        self.crc = 0
        self.size = 0

    def _write(self, b):
        if b:
            self.crc = zlib.crc32(b, self.crc)
            self.size += len(b)
            self.out.write(b)

    def feed(self, pos, chunk):
        """chunk = archive bytes [pos, pos+len)."""
        end = pos + len(chunk)
        lho = self.e["lho"]
        if self.data_start is None:
            a, b = max(pos, lho), min(end, lho + 30)
            if a < b:
                self.hdr += chunk[a - pos:b - pos]
            if len(self.hdr) == 30:
                if self.hdr[:4] != b"PK\x03\x04":
                    raise SystemExit("%s: bad local header" % self.e["name"])
                nlen, xlen = struct.unpack("<HH", self.hdr[26:30])
                self.data_start = lho + 30 + nlen + xlen
        if self.data_start is not None:
            a, b = max(pos, self.data_start), min(end, self.data_start + self.e["csize"])
            if a < b:
                piece = chunk[a - pos:b - pos]
                self._write(self.dec.decompress(piece) if self.dec else piece)

    def finish(self):
        if self.dec:
            self._write(self.dec.flush())
        self.out.close()
        ok = self.size == self.e["usize"] and (self.crc & 0xFFFFFFFF) == self.e["crc"]
        return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="4.7.2")
    ap.add_argument("--out", default=None, help="default: <repo>/_dl/godot-templates-<version>")
    ap.add_argument("--platform", choices=sorted(PLATFORM_MEMBERS), default="windows",
                    help="default member set: " + "; ".join("%s = %s" % (k, ",".join(v))
                                                            for k, v in sorted(PLATFORM_MEMBERS.items())))
    ap.add_argument("--members", default=None,
                    help="comma-separated file names inside templates/ (overrides --platform)")
    ap.add_argument("--list", action="store_true", help="list the archive members and exit (nothing written)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the full-archive SHA-512 pass: fetch only the members by range (CRC still checked)")
    a = ap.parse_args()
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    out = os.path.abspath(a.out or os.path.join(repo, "_dl", "godot-templates-%s" % a.version))
    url = BASE.format(v=a.version) + TPZ.format(v=a.version)
    members = a.members or ",".join(PLATFORM_MEMBERS[a.platform])

    total, entries = central_directory(url)
    print("tpz: %s, %.1f MB, %d entries" % (url, total / 1e6, len(entries)))
    if a.list:
        for e in sorted(entries.values(), key=lambda e: e["name"]):
            print("  %-48s %8.1f MB (packed %.1f MB)" % (e["name"], e["usize"] / 1e6, e["csize"] / 1e6))
        return
    os.makedirs(out, exist_ok=True)
    wanted = []
    for m in [s.strip() for s in members.split(",") if s.strip()]:
        e = entries.get("templates/" + m) or next((v for k, v in entries.items() if k.endswith("/" + m)), None)
        if not e:
            raise SystemExit("member %s not in archive; have: %s" % (m, ", ".join(sorted(entries))))
        wanted.append(Member(e, os.path.join(out, m)))
        print("  %-40s %8.1f MB (packed %.1f MB)" % (e["name"], e["usize"] / 1e6, e["csize"] / 1e6))

    expected = None
    if not a.no_verify:
        with http(BASE.format(v=a.version) + "SHA512-SUMS.txt") as r:
            for line in r.read().decode().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == TPZ.format(v=a.version):
                    expected = parts[0].lower()
        if not expected:
            raise SystemExit("no SHA-512 for %s in SHA512-SUMS.txt" % TPZ.format(v=a.version))

    ok = True
    try:
        if a.no_verify:
            for w in wanted:
                e = w.e
                hdr = fetch_range(url, e["lho"], e["lho"] + 29)
                w.feed(e["lho"], hdr)
                w.feed(w.data_start, fetch_range(url, w.data_start, w.data_start + e["csize"] - 1))
        else:
            h = hashlib.sha512()
            pos = 0
            step = 100 << 20
            with http(url) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    for w in wanted:
                        w.feed(pos, chunk)
                    pos += len(chunk)
                    if pos // step != (pos - len(chunk)) // step:
                        print("  streamed %d / %d MB" % (pos >> 20, total >> 20), flush=True)
            if pos != total:
                print("stream ended at %d of %d bytes" % (pos, total))
                ok = False
            elif h.hexdigest() != expected:
                print("SHA-512 mismatch: %s" % h.hexdigest())
                ok = False
            else:
                print("SHA-512 ok (%s...)" % expected[:16])
        for w in wanted:
            good = w.finish()
            print("  %s: %s" % (os.path.basename(w.path), "crc ok" if good else "CRC/size MISMATCH"))
            ok = ok and good
    finally:
        for w in wanted:
            if not w.out.closed:
                w.out.close()
            if ok:
                os.replace(w.path + ".part", w.path)
            elif os.path.exists(w.path + ".part"):
                os.remove(w.path + ".part")
    if not ok:
        raise SystemExit(1)
    print("-> %s" % out)


if __name__ == "__main__":
    main()
