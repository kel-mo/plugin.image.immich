# -*- coding: utf-8 -*-
"""Fanart that fits: a picture far from 16:9 sits whole and sharp on a blurred, darkened copy of itself,
instead of being cropped to fill the screen. Pixel mode is for game screenshots: scaled by whole numbers,
so their pixels stay sharp, onto a screen-sized canvas; stretched to the picture's aspect, as a TV did, and given
a look: scanlines, or a CRT's lines, phosphor stripes and glow."""
import io

SCREEN = 16 / 9
SLACK = 0.1                              # how far from 16:9 a picture may be and still fill the screen as it is
DIM = 0.5                                # brightness of the blurred copy
BLUR = (96, 54)                          # the copy is blurred this small, then stretched: soft, and quick
CANVAS = (1920, 1080)                    # pixel mode's canvas
LOOKS = {'scanlines': lambda im, k: scanlines(im, k, 0.35),
         'crt': lambda im, k: bloom(contrast(grille(scanlines(im, k, 0.4), 0.2), 1.08), 0.3)}


def compose(data, pixel=False, aspect=None, look=None):
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
        if aspect and 1 < w / h < aspect - 0.05:          # a console's narrow frame, shown wide on a 4:3 set
            k = min(round(CANVAS[0] / aspect) // h, CANVAS[1] // h)
            im = im.resize((round(h * k * aspect), h * k), Image.NEAREST) if k >= 1 else ImageOps.contain(im, CANVAS, Image.LANCZOS)
        else:
            im = im.resize((w * k, h * k), Image.NEAREST) if k >= 1 else ImageOps.contain(im, CANVAS, Image.LANCZOS)
        if look in LOOKS:
            im = LOOKS[look](im, max(k, 1))
        w, h = im.size
        size = CANVAS
    else:
        size = (round(h * SCREEN), h) if w / h < SCREEN else (w, round(w / SCREEN))   # the picture's own size: never enlarged
    back = ImageOps.fit(im, BLUR, Image.LANCZOS).filter(ImageFilter.GaussianBlur(3)).resize(size, Image.BICUBIC)
    back = ImageEnhance.Brightness(back).enhance(DIM)
    back.paste(im, ((size[0] - w) // 2, (size[1] - h) // 2))
    out = io.BytesIO()
    back.save(out, 'JPEG', quality=88, subsampling=0 if look in LOOKS else -1)   # full chroma keeps the stripes
    return out.getvalue()


# ------------------------------------------------------------------- looks
def scanlines(im, k, strength):
    """One darker line at the foot of each of the picture's rows, as the gap between a CRT beam's passes."""
    from PIL import Image, ImageChops
    if k < 2:
        return im
    w, h = im.size
    col = Image.new('L', (1, h), 255)
    for y in range(k - 1, h, k):
        col.putpixel((0, y), round(255 * (1 - strength)))
    return ImageChops.multiply(im, col.resize((w, h), Image.NEAREST).convert('RGB'))


def grille(im, strength):
    """Columns lit red, green and blue in turn, as a Trinitron's phosphor stripes."""
    from PIL import Image, ImageChops
    w, h = im.size
    f = round(255 * (1 - strength))
    tints = [(255, f, f), (f, 255, f), (f, f, 255)]
    row = Image.new('RGB', (w, 1))
    row.putdata([tints[x % 3] for x in range(w)])
    return ImageChops.multiply(im, row.resize((w, h), Image.NEAREST))


def contrast(im, amount):
    from PIL import ImageEnhance
    return ImageEnhance.Contrast(im).enhance(amount)


def bloom(im, amount):
    """A little glow bleeding from bright pixels, as phosphors did."""
    from PIL import ImageChops, ImageEnhance, ImageFilter
    return ImageChops.add(im, ImageEnhance.Brightness(im.filter(ImageFilter.GaussianBlur(2))).enhance(amount))
