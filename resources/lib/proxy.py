# -*- coding: utf-8 -*-
"""Local media proxy: Kodi fetches from 127.0.0.1 and the proxy adds the API key, so the key stays out
of Kodi's logs and texture cache."""
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from collections import OrderedDict
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import xbmc
import xbmcgui

from . import api, backdrop, kodi

PORT = 52283                             # fixed so Kodi's texture cache keeps its addresses
TRIES = 10
PROPERTY = '{}.proxy'.format(kodi.ADDON_ID)   # Home window property holding the port
DONE = '{}.done'.format(kodi.ADDON_ID)       # Home window property: photos fully sent, as id:size@time
FAILED = '{}.failed'.format(kodi.ADDON_ID)   # Home window property: photos that failed, as id:size@time:why
DONE_KEEP = 8
PHOTO = re.compile(r'/api/assets/([0-9a-f-]+)/thumbnail$')
TIMEOUT = 4                              # per socket wait: a relay under way ends inside Kodi's five-second wait at exit
CHUNK = 64 * 1024
_done_lock = threading.Lock()
PORTRAITS = 50                           # recent photos a person's fanart is picked from
FANART = re.compile(r'/fanart/(person/)?([0-9a-f-]+)$')     # a preview composed to 16:9, see backdrop.py
CLIP = re.compile(r'/clip/([0-9a-f-]+)$')         # a moving photo's clip, slowed down, see retime()
CLIP_SPEED = 0.6                         # moving photos' clips last a second or two, too quick to follow
CLIP_MAX = 64 * 1024 * 1024
CLIPS_KEPT = 2                           # Kodi reads a clip in several ranges
VIEWED = re.compile(r'[?&]size=(preview|fullsize)(&|$)')   # the viewer's photos, not list thumbnails
FULLSIZE = re.compile(r'[?&]size=fullsize(&|$)')
PHOTOS_KEPT = 4                          # recent photos sent again while Immich can't be reached
PHOTOS_BYTES = 64 * 1024 * 1024
AWAY = {401, 403, 429, 502, 503, 504}    # Immich out of reach, or turning every photo away
UNDECODABLE = {'image/avif'}             # originals LibreELEC's Kodi can't show over http
RANGE = re.compile(r'bytes=(\d*)-(\d*)$')
WHOLE = re.compile(r'bytes 0-(\d+)/(\d+)$')    # a 206 that is the whole file: Kodi asks for bytes=0-
ALLOWED = re.compile(r'/api/(assets/[0-9a-f-]+/(thumbnail|original|video/playback)|people/[0-9a-f-]+/thumbnail)$')
REQUEST_HEADERS = ('Range', 'If-None-Match', 'If-Modified-Since')
RESPONSE_HEADERS = ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'Cache-Control', 'ETag',
                    'Last-Modified')


def address():
    """The running proxy's base address, or None while the service isn't up."""
    port = xbmcgui.Window(10000).getProperty(PROPERTY)
    return 'http://127.0.0.1:{}'.format(port) if port else None


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'   # on Windows it lets a second server share the port
    upstream = ('', '')                     # (base_url, api_key)

    def __init__(self, port):
        super().__init__(('127.0.0.1', port), Handler)
        self.hosts = {'127.0.0.1:{}'.format(port), 'localhost:{}'.format(port)}
        self.ssl_ctx = ssl.create_default_context()
        self.open = set()                       # connections under way, cut when Kodi quits
        self.open_lock = threading.Lock()
        self.clips = OrderedDict()              # asset id -> (content type, retimed bytes)
        self.clips_lock = threading.Lock()
        self.photos = OrderedDict()             # request path -> (content type, bytes), the newest last
        self.photos_lock = threading.Lock()

    def keep(self, path, ctype, data):
        with self.photos_lock:
            self.photos[path] = (ctype, data)
            self.photos.move_to_end(path)
            while len(self.photos) > PHOTOS_KEPT or \
                    len(self.photos) > 1 and sum(len(d) for _, d in self.photos.values()) > PHOTOS_BYTES:
                self.photos.popitem(last=False)


class Handler(BaseHTTPRequestHandler):
    timeout = 5                              # a wait on Kodi's side this long means it has gone

    def setup(self):
        super().setup()
        with self.server.open_lock:
            self.server.open.add(self.connection)

    def finish(self):
        with self.server.open_lock:
            self.server.open.discard(self.connection)
        super().finish()

    def log_message(self, fmt, *args):
        kodi.debug('proxy: ' + fmt % args)

    def do_HEAD(self):
        self.relay()

    def do_GET(self):
        self.relay()

    def relay(self):
        path = self.path.split('?', 1)[0]
        if self.headers.get('Host') not in self.server.hosts:   # DNS rebinding
            return self.send_error(403)
        base_url, api_key = self.server.upstream
        if FANART.match(path) and base_url:
            person, asset_id = FANART.match(path).groups()
            return self.fanart(asset_id, base_url, api_key, bool(person))
        if CLIP.match(path) and base_url:
            return self.clip(CLIP.match(path).group(1), base_url, api_key)
        if not ALLOWED.match(path) or not base_url:
            return self.send_error(404)
        headers = {k: self.headers[k] for k in REQUEST_HEADERS if self.headers.get(k)}
        headers['x-api-key'] = api_key
        req = Request(base_url + self.path, headers=headers, method=self.command)
        try:
            resp = urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx)
            ctype = resp.headers.get_content_type()
            if resp.status == 200 and PHOTO.match(path) and FULLSIZE.search(self.path) and ctype in UNDECODABLE:
                kodi.debug('proxy: {} is {}, sending its preview'.format(path, ctype))
                resp.close()
                req = Request('{}{}?size=preview&edited=true'.format(base_url, path), headers=headers, method=self.command)
                resp = urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx)
        except HTTPError as e:                  # 304 and 416 included
            resp = e
        except (URLError, socket.timeout, OSError) as e:
            kodi.log('proxy: {} failed: {}'.format(path, e), xbmc.LOGWARNING)
            if not self.send_kept(path):
                announce(path, self.path, 'away')
                self.send_error(502)
            return
        why = failure(resp.status)
        if why in ('away', 'error') and self.send_kept(path):
            resp.close()
            return
        if why:
            announce(path, self.path, why)
        keep = [] if self.command == 'GET' and whole(resp) and PHOTO.match(path) and VIEWED.search(self.path) else None
        with resp:
            self.send_response(resp.status)
            for k in RESPONSE_HEADERS:
                if resp.headers.get(k):
                    self.send_header(k, resp.headers[k])
            self.end_headers()
            if self.command == 'GET':
                size = 0
                try:
                    while True:
                        reading = True
                        data = resp.read(CHUNK)
                        if not data or api.aborting():
                            break
                        reading = False
                        self.wfile.write(data)
                        size += len(data)
                        if keep is not None:
                            keep.append(data)
                            if size > PHOTOS_BYTES:
                                keep = None
                except (IncompleteRead, OSError):
                    if reading and resp.status in (200, 206):
                        announce(path, self.path, 'cut')
                    return                      # Kodi hangs up when it seeks, or the server drops out
                if resp.status in (200, 206):
                    announce(path, self.path)
                if keep is not None and not api.aborting():
                    self.server.keep(self.path, resp.headers.get('Content-Type'), b''.join(keep))

    def send_kept(self, path):
        """A recent photo again from its copy, while Immich can't send it; False without one."""
        with self.server.photos_lock:
            found = self.server.photos.get(self.path)
            if found:
                self.server.photos.move_to_end(self.path)
        if not found:
            return False
        kodi.debug('proxy: {} sent from its copy'.format(path))
        ctype, data = found
        if self.send_bytes(ctype or 'image/jpeg', data) and self.command == 'GET':
            announce(path, self.path)
        return True

    def send_bytes(self, ctype, data):
        """data, or the range of it asked for; False if it didn't get there."""
        start, end = 0, len(data) - 1
        asked = RANGE.match(self.headers.get('Range') or '')
        if asked and asked.group(1):
            start, end = int(asked.group(1)), min(int(asked.group(2) or end), end)
        elif asked and asked.group(2):                  # the last n bytes
            start = max(len(data) - int(asked.group(2)), 0)
        if start > end:
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */{}'.format(len(data)))
            self.end_headers()
            return False
        self.send_response(206 if asked else 200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        if asked:
            self.send_header('Content-Range', 'bytes {}-{}/{}'.format(start, end, len(data)))
        self.end_headers()
        if self.command == 'GET':
            try:
                self.wfile.write(data[start:end + 1])
            except OSError:
                return False
        return True

    def fanart(self, asset_id, base_url, api_key, person=False):
        """The asset's preview, or a photo of a person, composed to fit the screen."""
        try:
            if person:
                asset_id = self.portrait(asset_id, base_url, api_key)
            req = Request('{}/api/assets/{}/thumbnail?size=preview&edited=true'.format(base_url, asset_id),
                          headers={'x-api-key': api_key})
            with urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx) as resp:
                data = resp.read()
        except (HTTPError, URLError, socket.timeout, OSError, ValueError, LookupError) as e:
            kodi.log('proxy: fanart {} failed: {}'.format(asset_id, e), xbmc.LOGWARNING)
            return self.send_error(502)
        try:
            data = backdrop.compose(data)
        except (OSError, ValueError) as e:              # PIL raises OSError for unreadable images
            kodi.log('proxy: fanart {} not composed: {}'.format(asset_id, e), xbmc.LOGWARNING)
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg' if data[:2] == b'\xff\xd8' else 'image/png' if data[:4] == b'\x89PNG' else 'image/webp')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, max-age=86400')
        self.end_headers()
        if self.command == 'GET':
            try:
                self.wfile.write(data)
            except OSError:
                pass

    def clip(self, asset_id, base_url, api_key):
        """A moving photo's own clip, slowed to CLIP_SPEED; kept a while, as Kodi reads it in ranges."""
        with self.server.clips_lock:
            found = self.server.clips.get(asset_id)
        if not found:
            req = Request('{}/api/assets/{}/original'.format(base_url, asset_id), headers={'x-api-key': api_key})
            try:
                with urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx) as resp:
                    ctype, data = resp.headers.get('Content-Type') or 'video/mp4', resp.read(CLIP_MAX + 1)
            except (HTTPError, URLError, socket.timeout, OSError, IncompleteRead) as e:
                kodi.log('proxy: clip {} failed: {}'.format(asset_id, e), xbmc.LOGWARNING)
                return self.send_error(502)
            if len(data) > CLIP_MAX:
                return self.send_error(502)
            found = ctype, retime(data, CLIP_SPEED) or data
            with self.server.clips_lock:
                self.server.clips[asset_id] = found
                while len(self.server.clips) > CLIPS_KEPT:
                    self.server.clips.popitem(last=False)
        self.send_bytes(*found)

    def portrait(self, person_id, base_url, api_key):
        """Of their recent photos, the newest with the fewest people, so a group shot doesn't stand in for all.
        Looked up only when Kodi shows the person's fanart: a lookup each in the listing is too slow."""
        body = {'personIds': [person_id], 'type': 'IMAGE', 'visibility': 'timeline', 'size': PORTRAITS,
                'withPeople': True}
        req = Request(base_url + '/api/search/metadata', data=json.dumps(body).encode(), method='POST',
                      headers={'x-api-key': api_key, 'Content-Type': 'application/json'})
        with urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx) as resp:
            found = json.loads(resp.read())['assets']['items']
        return min(found, key=lambda a: len(a.get('people') or []))['id']


def boxes(data, start, end):
    """(type, body start, end) of the MP4 boxes in data[start:end]."""
    while start + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, start)
        head = 8
        if size == 1:
            size, head = struct.unpack_from('>Q', data, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < head or start + size > end:
            raise ValueError('bad box')
        yield kind, start + head, start + size
        start += size


def retime(data, speed):
    """The MP4 slowed to speed without re-encoding, or None if it can't be: each track's timescale shrinks, so its
    sample times stretch, and the durations kept in the movie's timescale grow to match. Fragmented files and ones
    with sound are left alone."""
    out = bytearray(data)
    tracks = []

    def stretch(at, wide):                              # a duration in the movie's timescale
        fmt = '>Q' if wide else '>I'
        value = struct.unpack_from(fmt, out, at)[0]
        if value != (0xFFFFFFFFFFFFFFFF if wide else 0xFFFFFFFF):
            struct.pack_into(fmt, out, at, round(value / speed))

    def walk(start, end):
        for kind, body, stop in boxes(out, start, end):
            v = out[body] if body < stop else 0
            if kind in (b'moof', b'mvex'):
                raise ValueError('fragmented')
            if kind in (b'moov', b'trak', b'mdia', b'edts'):
                walk(body, stop)
            elif kind == b'mvhd':
                stretch(body + (24 if v else 16), v)
            elif kind == b'tkhd':
                stretch(body + (28 if v else 20), v)
            elif kind == b'elst':
                for n in range(struct.unpack_from('>I', out, body + 4)[0]):
                    stretch(body + 8 + n * (20 if v else 12), v)
            elif kind == b'mdhd':
                at = body + (20 if v else 12)
                struct.pack_into('>I', out, at, max(round(struct.unpack_from('>I', out, at)[0] * speed), 1))
            elif kind == b'hdlr':
                tracks.append(bytes(out[body + 8:body + 12]))

    try:
        walk(0, len(out))
    except (ValueError, struct.error) as e:
        kodi.log('proxy: clip not retimed: {}'.format(e), xbmc.LOGWARNING)
        return None
    if b'vide' not in tracks or b'soun' in tracks:
        return None
    return bytes(out)


def whole(resp):
    """Whether Immich's answer carries the whole photo."""
    if resp.status == 200:
        return True
    found = resp.status == 206 and WHOLE.match(resp.headers.get('Content-Range') or '')
    return bool(found) and int(found.group(1)) + 1 == int(found.group(2))


def failure(status):
    """Why a photo failed, from Immich's answer: away, error (on the server), gone, or None when it didn't."""
    if status < 400:
        return None
    if status in AWAY:
        return 'away'
    return 'error' if status >= 500 else 'gone'


def announce(path, full, why=None):
    """Tell the viewer a photo has been sent in full, so it can fade it in, or why it couldn't be."""
    found = PHOTO.match(path)
    if not found:
        return
    size = re.search(r'[?&]size=([a-z]+)', full)
    name = '{}:{}'.format(found.group(1), size.group(1) if size else 'thumbnail')
    prop, entry = (FAILED, '{}@{:.3f}:{}'.format(name, time.time(), why)) if why else \
        (DONE, '{}@{:.3f}'.format(name, time.time()))
    with _done_lock:
        window = xbmcgui.Window(10000)
        kept = [e for e in window.getProperty(prop).split() if e.rpartition('@')[0] != name]
        window.setProperty(prop, ' '.join(kept[-(DONE_KEEP - 1):] + [entry]))


def start():
    for port in range(PORT, PORT + TRIES):
        try:
            server = Server(port)
            break
        except OSError:
            continue
    else:
        kodi.log('proxy: no free port from {}'.format(PORT), xbmc.LOGERROR)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    xbmcgui.Window(10000).setProperty(PROPERTY, str(port))
    kodi.log('proxy on port {}'.format(port))
    return server


def stop(server):
    """Right away: relays under way are cut, so no thread of ours outlives Kodi's wait."""
    xbmcgui.Window(10000).clearProperty(PROPERTY)
    server.shutdown()
    server.server_close()
    with server.open_lock:
        for conn in list(server.open):
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def forget_cached_photos():
    """Drop slideshow originals cached by earlier runs; Kodi keeps them at a fraction of their size."""
    found = kodi.jsonrpc('Textures.GetTextures', properties=['url'],
                         filter={'field': 'url', 'operator': 'contains', 'value': '/api/assets/'})
    photos = [t for t in (found or {}).get('textures') or []
              if t['url'].startswith('http://127.0.0.1:') and 'size=fullsize' in t['url']]
    for t in photos:
        kodi.jsonrpc('Textures.RemoveTexture', textureid=t['textureid'])
    if photos:
        kodi.log('proxy: forgot {} cached photos'.format(len(photos)))


def forget_keyed_textures():
    """Drop cached images from before the proxy; their addresses carry the API key."""
    found = kodi.jsonrpc('Textures.GetTextures', properties=['url'],
                         filter={'and': [{'field': 'url', 'operator': 'contains', 'value': '/api/'},
                                         {'field': 'url', 'operator': 'contains', 'value': '|x-api-key='}]})
    textures = (found or {}).get('textures') or []
    for t in textures:
        kodi.jsonrpc('Textures.RemoveTexture', textureid=t['textureid'])
    if textures:
        kodi.log('proxy: forgot {} cached images with the API key'.format(len(textures)))
