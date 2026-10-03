# -*- coding: utf-8 -*-
"""Fanart that fits: a picture far from 16:9 sits whole and sharp on a blurred, darkened copy of itself,
instead of being cropped to fill the screen. Pixel mode is for game screenshots: scaled by whole numbers,
so their pixels stay square, onto a screen-sized canvas."""
import io

SCREEN = 16 / 9
SLACK = 0.1                              # how far from 16:9 a picture may be and still fill the screen as it is
DIM = 0.5                                # brightness of the blurred copy
BLUR = (96, 54)                          # the copy is blurred this small, then stretched: soft, and quick
CANVAS = (1920, 1080)                    # pixel mode's canvas


def compose(data, pixel=False):
    """The fanart as JPEG bytes; the picture as it came when it fills the screen already or PIL is missing."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ImportError:
        return data
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    w, h = im.size
    if abs(w / h - SCREEN) < SLACK and (not pixel or h >= CANVAS[1]):
        return data
    if pixel:
        k = min(CANVAS[0] // w, CANVAS[1] // h)
        im = im.resize((w * k, h * k), Image.NEAREST) if k >= 1 else ImageOps.contain(im, CANVAS, Image.LANCZOS)
        w, h = im.size
        size = CANVAS
    else:
        size = (round(h * SCREEN), h) if w / h < SCREEN else (w, round(w / SCREEN))   # the picture's own size: never enlarged
    back = ImageOps.fit(im, BLUR, Image.LANCZOS).filter(ImageFilter.GaussianBlur(3)).resize(size, Image.BICUBIC)
    back = ImageEnhance.Brightness(back).enhance(DIM)
    back.paste(im, ((size[0] - w) // 2, (size[1] - h) // 2))
    out = io.BytesIO()
    back.save(out, 'JPEG', quality=88)
    return out.getvalue()
