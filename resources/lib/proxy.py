# -*- coding: utf-8 -*-
"""Local media proxy: Kodi fetches from 127.0.0.1 and the proxy adds the API key, so the key stays out
of Kodi's logs and texture cache."""
import os
import re
import socket
import ssl
import threading
import time
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
DONE_KEEP = 8
PHOTO = re.compile(r'/api/assets/([0-9a-f-]+)/thumbnail$')
TIMEOUT = 4                              # per socket wait: a relay under way ends inside Kodi's five-second wait at exit
CHUNK = 64 * 1024
_done_lock = threading.Lock()
FANART = re.compile(r'/fanart/([0-9a-f-]+)$')     # a preview composed to 16:9, see backdrop.py
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


class Handler(BaseHTTPRequestHandler):
    timeout = 60

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
            return self.fanart(FANART.match(path).group(1), base_url, api_key)
        if not ALLOWED.match(path) or not base_url:
            return self.send_error(404)
        headers = {k: self.headers[k] for k in REQUEST_HEADERS if self.headers.get(k)}
        headers['x-api-key'] = api_key
        req = Request(base_url + self.path, headers=headers, method=self.command)
        try:
            resp = urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx)
        except HTTPError as e:                  # 304 and 416 included
            resp = e
        except (URLError, socket.timeout, OSError) as e:
            kodi.log('proxy: {} failed: {}'.format(path, e), xbmc.LOGWARNING)
            return self.send_error(502)
        with resp:
            self.send_response(resp.status)
            for k in RESPONSE_HEADERS:
                if resp.headers.get(k):
                    self.send_header(k, resp.headers[k])
            self.end_headers()
            if self.command == 'GET':
                try:
                    while True:
                        data = resp.read(CHUNK)
                        if not data or api.aborting():
                            break
                        self.wfile.write(data)
                except (IncompleteRead, OSError):
                    return                      # Kodi hangs up when it seeks, or the server drops out
                if resp.status in (200, 206):
                    announce(path, self.path)


    def fanart(self, asset_id, base_url, api_key):
        """The asset's preview, composed to fit the screen."""
        req = Request('{}/api/assets/{}/thumbnail?size=preview&edited=true'.format(base_url, asset_id),
                      headers={'x-api-key': api_key})
        try:
            with urlopen(req, timeout=TIMEOUT, context=self.server.ssl_ctx) as resp:
                data = resp.read()
        except (HTTPError, URLError, socket.timeout, OSError) as e:
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


def announce(path, full):
    """Tell the viewer a photo has been sent in full, so it can fade it in."""
    found = PHOTO.match(path)
    if not found:
        return
    size = re.search(r'[?&]size=([a-z]+)', full)
    name = '{}:{}'.format(found.group(1), size.group(1) if size else 'thumbnail')
    with _done_lock:
        window = xbmcgui.Window(10000)
        kept = [e for e in window.getProperty(DONE).split() if e.rpartition('@')[0] != name]
        window.setProperty(DONE, ' '.join(kept[-(DONE_KEEP - 1):] + ['{}@{:.3f}'.format(name, time.time())]))


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
    xbmcgui.Window(10000).clearProperty(PROPERTY)
    server.shutdown()
    server.server_close()


def forget_cached(url):
    """Drop Kodi's cached copy of an image, so it loads in full through the proxy again."""
    found = kodi.jsonrpc('Textures.GetTextures', properties=['url'],
                         filter={'field': 'url', 'operator': 'is', 'value': url})
    for t in (found or {}).get('textures') or []:
        kodi.jsonrpc('Textures.RemoveTexture', textureid=t['textureid'])


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
