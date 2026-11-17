#!/usr/bin/env python3
"""Rebuild UEFI CPER binaries from Linux GHES '[Hardware Error]' hex dumps.

Section payload bytes are taken verbatim from the kernel log; only the
128-byte record header + 72-byte section descriptor are synthesized.
Usage: mkcper.py <raw_log.txt> <out_dir>
"""
import re, struct, sys, uuid, os

SEV = {"recoverable": 0, "fatal": 1, "corrected": 2, "info": 3}
raw, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

recs = {}   # rec_id -> {"sev":..., "sections":[{"type":uuid,"sev":..,"len":..,"hex":bytearray}]}
cur = None
for line in open(raw, errors="replace"):
    m = re.search(r"\{(\d+)\}\[Hardware Error\]:(.*)$", line)
    if not m:
        continue
    rid, body = int(m.group(1)), m.group(2)
    r = recs.setdefault(rid, {"sev": "info", "sections": []})
    if (s := re.search(r"event severity: (\w+)", body)):
        r["sev"] = s.group(1)
    elif (s := re.search(r"Error \d+, type: (\w+)", body)):
        cur = {"sev": s.group(1), "type": None, "len": None, "hex": bytearray()}
        r["sections"].append(cur)
    elif (s := re.search(r"section type: .*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", body)):
        cur["type"] = uuid.UUID(s.group(1))
    elif (s := re.search(r"section length: (0x[0-9a-f]+)", body)):
        cur["len"] = int(s.group(1), 16)
    elif (s := re.match(r"\s+([0-9a-f]{8}): ((?:[0-9a-f]{8} ){1,4})", body + " ")):
        off = int(s.group(1), 16)
        words = s.group(2).split()
        chunk = b"".join(struct.pack("<I", int(w, 16)) for w in words)
        cur["hex"][off:off + len(chunk)] = chunk

for rid, r in sorted(recs.items()):
    secs = [s for s in r["sections"] if s["type"]]
    n = len(secs)
    hdr_len = 128 + 72 * n
    body, descs, off = b"", b"", hdr_len
    for s in secs:
        data = bytes(s["hex"][: s["len"]]) if s["len"] else bytes(s["hex"])
        if s["len"] and len(data) != s["len"]:
            print(f"WARN rec {rid}: have {len(data)} of {s['len']} bytes", file=sys.stderr)
        descs += struct.pack("<IIHBBI", off, len(data), 0x0300, 0, 0, 0)
        descs += s["type"].bytes_le + b"\x00" * 16
        descs += struct.pack("<I", SEV.get(s["sev"], 3)) + b"\x00" * 20
        body += data
        off += len(data)
    total = hdr_len + len(body)
    hdr = b"CPER" + struct.pack("<H", 0x0101) + b"\xff\xff\xff\xff" + struct.pack("<H", n)
    hdr += struct.pack("<III", SEV.get(r["sev"], 3), 0, total) + b"\x00" * 8
    hdr += b"\x00" * 64 + struct.pack("<QIQ", rid, 0, 0) + b"\x00" * 12
    assert len(hdr) == 128
    p = os.path.join(out, f"ghes_rec{rid}.bin")
    open(p, "wb").write(hdr + descs + body)
    print(p, r["sev"], n, "section(s)", [hex(s["len"] or 0) for s in secs], total, "bytes")
