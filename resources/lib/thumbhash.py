# -*- coding: utf-8 -*-
"""Render an Immich thumbhash (https://evanw.github.io/thumbhash/) as a blurred PNG backdrop."""
import base64
import os
import struct
import zlib
from math import cos, pi


def _channel(data, start, index, nx, ny, scale):
    ac = []
    for cy in range(ny):
        cx = 0 if cy else 1
        while cx * ny < nx * (ny - cy):
            ac.append((((data[start + (index >> 1)] >> ((index & 1) << 2)) & 15) / 7.5 - 1) * scale)
            index += 1
            cx += 1
    return ac, index


def decode(text, long_side=64):
    """Returns (width, height, rows of RGB bytes). The DCT is continuous, so any size works."""
    data = base64.b64decode(text)
    header24 = data[0] | (data[1] << 8) | (data[2] << 16)
    header16 = data[3] | (data[4] << 8)
    l_dc = (header24 & 63) / 63
    p_dc = ((header24 >> 6) & 63) / 31.5 - 1
    q_dc = ((header24 >> 12) & 63) / 31.5 - 1
    l_scale = ((header24 >> 18) & 31) / 31
    has_alpha = header24 >> 23
    p_scale = ((header16 >> 3) & 63) / 63
    q_scale = ((header16 >> 9) & 63) / 63
    landscape = header16 >> 15
    lx = max(3, (5 if has_alpha else 7) if landscape else header16 & 7)
    ly = max(3, header16 & 7 if landscape else (5 if has_alpha else 7))
    start = 6 if has_alpha else 5
    l_ac, index = _channel(data, start, 0, lx, ly, l_scale)
    p_ac, index = _channel(data, start, index, 3, 3, p_scale * 1.25)
    q_ac, index = _channel(data, start, index, 3, 3, q_scale * 1.25)

    ratio = lx / ly if (lx and ly) else 1.0
    w = long_side if ratio > 1 else max(1, round(long_side * ratio))
    h = max(1, round(long_side / ratio)) if ratio > 1 else long_side
    rows = []
    for y in range(h):
        fy = [cos(pi / h * (y + 0.5) * c) for c in range(max(ly, 3))]
        row = bytearray()
        for x in range(w):
            fx = [cos(pi / w * (x + 0.5) * c) for c in range(max(lx, 3))]
            lum, p, q = l_dc, p_dc, q_dc
            j = 0
            for cy in range(ly):
                cx = 0 if cy else 1
                fy2 = fy[cy] * 2
                while cx * ly < lx * (ly - cy):
                    lum += l_ac[j] * fx[cx] * fy2
                    j += 1
                    cx += 1
            j = 0
            for cy in range(3):
                cx = 0 if cy else 1
                fy2 = fy[cy] * 2
                while cx < 3 - cy:
                    f = fx[cx] * fy2
                    p += p_ac[j] * f
                    q += q_ac[j] * f
                    j += 1
                    cx += 1
            b = lum - 2 / 3 * p
            r = (3 * lum - b + q) / 2
            g = r - q
            row += bytes(max(0, min(255, int(255 * v))) for v in (r, g, b))
        rows.append(bytes(row))
    return w, h, rows


def write_png(path, width, height, rows):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    raw = b''.join(b'\0' + r for r in rows)
    tmp = path + '.tmp'                         # a half-written file must not pass for done
    with open(tmp, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    os.replace(tmp, path)
    return path
