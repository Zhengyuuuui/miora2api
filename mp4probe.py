"""极简 MP4 探测：走 box 树找 tkhd / stsd，拿时长与真实分辨率。"""
from __future__ import annotations
import struct

def _boxes(buf: bytes, off: int, end: int):
    while off + 8 <= end:
        size = struct.unpack(">I", buf[off:off+4])[0]
        typ = buf[off+4:off+8]
        if size == 0:
            size = end - off
        if size < 8:
            break
        yield typ, off, size
        off += size

def probe(path_or_bytes) -> dict:
    data = open(path_or_bytes, "rb").read() if isinstance(path_or_bytes, str) else path_or_bytes
    out = {"bytes": len(data), "width": None, "height": None,
           "duration_s": None, "video": False, "audio": False}
    mvhd = None; tkhd = None; stsd = None
    for t, o, s in _boxes(data, 0, len(data)):
        if t == b"moov":
            if mvhd is None:
                for t2, o2, s2 in _boxes(data, o + 8, o + s):
                    if t2 == b"mvhd":
                        mvhd = (o2, s2); break
            for t2, o2, s2 in _boxes(data, o + 8, o + s):
                if t2 != b"trak":
                    continue
                is_video = False
                for t3, o3, s3 in _boxes(data, o2 + 8, o2 + s2):
                    if t3 == b"tkhd" and tkhd is None:
                        tkhd = (o3, s3)
                    if t3 == b"mdia":
                        for t4, o4, s4 in _boxes(data, o3 + 8, o3 + s3):
                            if t4 == b"hdlr":
                                out["video"] = out["video"] or data[o4+16:o4+20] == b"vide"
                                out["audio"] = out["audio"] or data[o4+16:o4+20] == b"soun"
                            if t4 == b"minf":
                                for t5, o5, s5 in _boxes(data, o4 + 8, o4 + s4):
                                    if t5 == b"vmhd":
                                        is_video = True
                                    if t5 == b"stbl" and is_video and stsd is None:
                                        for t6, o6, s6 in _boxes(data, o5 + 8, o5 + s5):
                                            if t6 == b"stsd":
                                                stsd = (o6, s6)
    if mvhd:
        o, _ = mvhd
        # box 起点 o 指向 size(4)+type(4)；mvhd 内容从 o+8 开始
        ver = data[o + 8]
        if ver == 0:
            ts, dur = struct.unpack(">II", data[o + 20:o + 28])
        else:
            ts, dur = struct.unpack(">IQ", data[o + 28:o + 40])
        if ts:
            out["duration_s"] = round(dur / ts, 2)
    if stsd:
        o, _ = stsd
        # 稳妥做法：直接在 stsd 之后找 codec 四字节标签，再按 VisualSampleEntry 布局取宽高
        for codec in (b"avc1", b"avc3", b"hvc1", b"hev1", b"av01", b"vp09", b"mp4v"):
            a = data.find(codec, o + 8, o + 4096)
            if a < 0:
                continue
            # VisualSampleEntry: size(4) fmt(4) resv(6) dri(2) resv(16) -> w(2) h(2)
            w = struct.unpack(">H", data[a + 28:a + 30])[0]
            h = struct.unpack(">H", data[a + 30:a + 32])[0]
            if w and h:
                out["width"], out["height"] = w, h
            break
    if not out["width"] and tkhd:
        o, _ = tkhd
        base = o + 4 + 4 + 4 + 4 + 4 + 4 + 4 + 2 + 2 + 2 + 2 + 36
        out["width"] = struct.unpack(">I", data[base:base + 4])[0] >> 16
        out["height"] = struct.unpack(">I", data[base + 4:base + 8])[0] >> 16
    return out

if __name__ == "__main__":
    import sys, json
    print(json.dumps(probe(sys.argv[1]), ensure_ascii=False))
