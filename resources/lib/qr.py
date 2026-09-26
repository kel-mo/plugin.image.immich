# -*- coding: utf-8 -*-
"""Render a QR code to PNG with pyqrcode's module matrix and a tiny zlib PNG writer."""
import struct
import zlib

from . import kodi


def _chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def write_png(path, rows, palette=((255, 255, 255), (0, 0, 0))):
    """rows: iterable of iterables of palette indices (0/1)."""
    rows = [list(r) for r in rows]
    height, width = len(rows), len(rows[0])
    raw = bytearray()
    for r in rows:
        raw.append(0)
        for v in r:
            raw.extend(palette[v])
    png = b'\x89PNG\r\n\x1a\n'
    png += _chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    png += _chunk(b'IDAT', zlib.compress(bytes(raw), 9))
    png += _chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)
    return path


def make(text, path, scale=8, border=3):
    """Returns the PNG path, or None when pyqrcode is unavailable."""
    try:
        import pyqrcode                             # script.module.pyqrcode dependency
    except ImportError as e:
        kodi.log('pyqrcode not available, skipping QR code: {}'.format(e))
        return None
    code = pyqrcode.create(text, error='M').code
    size = len(code)
    rows = []
    blank = [0] * ((size + 2 * border) * scale)
    for _ in range(border * scale):
        rows.append(blank)
    for line in code:
        row = [0] * (border * scale)
        for v in line:
            row.extend([1 if v else 0] * scale)
        row.extend([0] * (border * scale))
        for _ in range(scale):
            rows.append(row)
    for _ in range(border * scale):
        rows.append(blank)
    return write_png(path, rows)


def solid(path, rgb):
    return write_png(path, [[0]], palette=(tuple(rgb), (0, 0, 0)))
