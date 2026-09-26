# -*- coding: utf-8 -*-
"""Sign-in: a phone opens a page served by Kodi (via QR code) and pastes an API key or signs in."""
import html
import os
import secrets
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template
from urllib.parse import parse_qs

import xbmc
import xbmcgui

from . import kodi, qr
from .api import MIN_SERVER, SCOPES, ApiError, AuthError, ImmichClient, clean_url

TIMEOUT = 600
ACTION_CANCEL = {9, 10, 13, 92}  # parent dir, previous menu, stop, nav back
REQUIRED = {'asset.read', 'asset.view', 'album.read', 'user.read'}


def is_signed_in():
    return bool(kodi.setting('server_url') and kodi.setting('api_key'))


def device_name():
    return xbmc.getInfoLabel('System.FriendlyName') or 'Kodi'


def key_name():
    return 'Kodi ({})'.format(device_name())


def lan_ip():
    """Address the phone can reach; the UDP connect sends nothing."""
    ip = xbmc.getIPAddress() if hasattr(xbmc, 'getIPAddress') else ''
    if ip and not ip.startswith('127.'):
        return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def verify(server, api_key=None, email=None, password=None):
    """Returns (server_url, api_key, user name) or raises ApiError with a message for the phone."""
    server = clean_url(server)
    if not server:
        raise ApiError(kodi.L(30601))
    client = ImmichClient(server, '')
    version = client.server_version()
    if version < MIN_SERVER:
        raise ApiError(kodi.L(30621, '.'.join(map(str, version))))
    if not api_key:
        if not (email and password):
            raise ApiError(kodi.L(30622))
        try:
            token = client.login(email, password)['accessToken']
        except AuthError:
            raise ApiError(kodi.L(30623))
        try:
            api_key = client.create_key(token, key_name())['secret']
        finally:
            try:
                client.logout(token)
            except ApiError:
                pass
    client.api_key = api_key.strip()
    try:
        perms = set((client.key_info() or {}).get('permissions') or [])  # needs no scope
    except AuthError:
        raise ApiError(kodi.L(30624))
    missing = REQUIRED - perms if 'all' not in perms else set()
    if missing:
        raise ApiError(kodi.L(30625, ', '.join(sorted(missing))))
    me = client.me()
    return server, client.api_key, me.get('name') or me.get('email') or ''


def store(server, api_key, name):
    kodi.set_setting('server_url', server)
    kodi.set_setting('api_key', api_key)
    kodi.set_setting('username', name)


def sign_out():
    kodi.set_setting('api_key', '')
    kodi.set_setting('username', '')
    kodi.notify(kodi.L(30609))


# ------------------------------------------------------------------- phone
def page(server, message='', ok=False, open_password=False):
    with open(kodi.data_file('signin.html'), encoding='utf-8') as f:
        tpl = Template(f.read())
    msg = '<div class="msg {}">{}</div>'.format('ok' if ok else 'err', html.escape(message)) if message else ''
    return tpl.safe_substitute(
        device=html.escape(kodi.L(30626, device_name())), message=msg, server=html.escape(server or ''),
        keys_url=html.escape((clean_url(server) or '') + '/user-settings?isOpen=api-keys'),
        scopes=html.escape(' '.join(SCOPES)), key_name=html.escape(key_name()),
        open_password='open' if open_password else '')


def done_page(name):
    return ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<body style="font:18px system-ui;background:#0e121a;color:#e8ebf2;padding:2rem">'
            '<h1>{}</h1><p>{}</p></body>').format(html.escape(kodi.L(30604, name)), html.escape(kodi.L(30627)))


class SignInServer(HTTPServer):
    allow_reuse_address = True
    timeout = 0.25

    def __init__(self):
        super().__init__(('', 0), Handler)
        self.token = secrets.token_urlsafe(6)
        self.result = None

    @property
    def url(self):
        return 'http://{}:{}/{}'.format(lan_ip(), self.server_address[1], self.token)


class Handler(BaseHTTPRequestHandler):
    timeout = 5                                 # idle or slow clients must not stall the dialog

    def log_message(self, fmt, *args):
        kodi.debug('sign-in page: ' + fmt % args)

    def _send(self, code, body):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        if self.path.split('?')[0].strip('/') != self.server.token:
            self._send(404, 'Not found')
            return False
        return True

    def do_GET(self):
        if self._allowed():
            self._send(200, page(kodi.fresh_setting('server_url')))

    def do_POST(self):
        if not self._allowed():
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 16384:
            self._send(400, 'Bad request')
            return
        form = {k: v[0].strip() for k, v in parse_qs(self.rfile.read(length).decode('utf-8', 'replace')).items()}
        server = form.get('server', '')
        try:
            result = verify(server, form.get('api_key'), form.get('email'), form.get('password'))
        except ApiError as e:
            kodi.log('sign-in failed: {}'.format(e), xbmc.LOGWARNING)
            self._send(200, page(server, str(e), open_password=bool(form.get('email'))))
            return
        self.server.result = result
        self._send(200, done_page(result[2]))


# ------------------------------------------------------------------ dialog
class SignInDialog(xbmcgui.WindowDialog):
    def __init__(self, qr_png, background_png, url):
        super().__init__()
        self.cancelled = False
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, background_png))
        self.addControl(xbmcgui.ControlImage(100, 140, 440, 440, qr_png, aspectRatio=2))
        x, w = 600, 620
        self.addControl(xbmcgui.ControlLabel(x, 140, w, 40, kodi.L(30600), font='font14', textColor='0xFFFFFFFF'))
        self.addControl(xbmcgui.ControlLabel(x, 200, w, 30, kodi.L(30602), font='font13', textColor='0xFFCCCCCC'))
        self.addControl(xbmcgui.ControlLabel(x, 235, w, 30, url, font='font12', textColor='0xFFFF9A5C'))
        info = xbmcgui.ControlTextBox(x, 290, w, 90, font='font12', textColor='0xFFCCCCCC')
        self.addControl(info)
        info.setText(kodi.L(30607))
        self.addControl(xbmcgui.ControlLabel(x, 400, w, 30, kodi.L(30603), font='font13', textColor='0xFFCCCCCC'))
        self.addControl(xbmcgui.ControlLabel(x, 440, w, 30, kodi.L(30608), font='font12', textColor='0xFF888888'))

    def onAction(self, action):
        if action.getId() in ACTION_CANCEL:
            self.cancelled = True
            self.close()


def sign_in():
    try:
        server = SignInServer()
    except OSError as e:
        kodi.log('sign-in server failed: {}'.format(e), xbmc.LOGERROR)
        kodi.error(kodi.L(30610, e))
        return False
    url = server.url
    kodi.log('sign-in page at {}'.format(url.rsplit('/', 1)[0]))
    tmp = kodi.ensure_dir(kodi.PROFILE)
    qr_png = qr.make(url, os.path.join(tmp, 'signin_qr.png')) or ''
    background = qr.solid(os.path.join(tmp, 'signin_bg.png'), (14, 18, 26))
    dialog = SignInDialog(qr_png, background, url)
    dialog.show()
    monitor = xbmc.Monitor()
    deadline = time.time() + TIMEOUT
    try:
        while not server.result and not dialog.cancelled and time.time() < deadline:
            server.handle_request()
            if monitor.waitForAbort(0.05):      # also runs onAction callbacks
                break
    finally:
        server.server_close()
        dialog.close()
        del dialog
    if not server.result:
        return False
    store(*server.result)
    kodi.notify(kodi.L(30604, server.result[2]))
    return True


def enter_key():
    """Remote-only fallback: type the server address and an API key."""
    dialog = xbmcgui.Dialog()
    server = dialog.input(kodi.L(30101), kodi.fresh_setting('server_url') or 'https://',
                          type=xbmcgui.INPUT_ALPHANUM)
    if not server or server.strip() == 'https://':
        return False
    key = dialog.input(kodi.L(30105), type=xbmcgui.INPUT_ALPHANUM)
    if not key:
        return False
    try:
        result = verify(server, key)
    except ApiError as e:
        kodi.error(str(e))
        return False
    store(*result)
    kodi.notify(kodi.L(30604, result[2]))
    return True
