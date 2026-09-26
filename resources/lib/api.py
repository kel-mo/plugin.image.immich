# -*- coding: utf-8 -*-
"""Minimal Immich REST client built on urllib (no external dependencies)."""
import json
import socket
import ssl
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import xbmc

from . import kodi

MIN_SERVER = (2, 0, 0)
TIMEOUT = 30
PAGE = 1000

# read-only scopes for keys we create; also listed on the sign-in page
SCOPES = ['asset.read', 'asset.view', 'asset.download', 'album.read', 'person.read', 'memory.read',
          'map.read', 'tag.read', 'partner.read', 'timeline.read', 'stack.read', 'user.read']


class ApiError(Exception):
    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(ApiError):
    pass


def parse_version(data):
    return tuple(int(data.get(k) or 0) for k in ('major', 'minor', 'patch'))


def clean_url(url):
    url = (url or '').strip().rstrip('/')
    if url.endswith('/api'):
        url = url[:-4]
    if url and not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url


_monitor = None


def aborting():
    """True once Kodi is shutting down; every request checks it so no call outlives teardown."""
    global _monitor
    if _monitor is None:
        _monitor = xbmc.Monitor()
    return _monitor.abortRequested()


class ImmichClient:
    def __init__(self, base_url=None, api_key=None, timeout=TIMEOUT):
        self.base_url = clean_url(base_url if base_url is not None else kodi.setting('server_url'))
        self.api_key = api_key if api_key is not None else kodi.setting('api_key')
        self.timeout = timeout
        self.ssl_ctx = ssl.create_default_context()

    # ------------------------------------------------------------------ core
    def url(self, path, **params):
        clean = {k: v for k, v in params.items() if v is not None and v != ''}
        query = urlencode(clean, doseq=True)
        return '{}/api{}{}'.format(self.base_url, path, '?' + query if query else '')

    def headers(self, token=None, extra=None):
        h = {'Accept': 'application/json',
             'User-Agent': '{}/{}'.format(kodi.ADDON_ID, kodi.ADDON_VERSION)}
        if token:
            h['Authorization'] = 'Bearer ' + token
        elif self.api_key:
            h['x-api-key'] = self.api_key
        if extra:
            h.update(extra)
        return h

    def request(self, method, path, params=None, body=None, token=None, timeout=None):
        """token: a session token that replaces the API key (sign-in only)."""
        if not self.base_url:
            raise ApiError(kodi.L(30601))
        if aborting():
            raise ApiError('cancelled')
        url = self.url(path, **(params or {}))
        data = None
        extra = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            extra['Content-Type'] = 'application/json'
        req = Request(url, data=data, headers=self.headers(token, extra), method=method)
        kodi.debug('{} {}'.format(method, url))
        try:
            resp = urlopen(req, timeout=timeout or self.timeout, context=self.ssl_ctx)
        except HTTPError as e:
            detail = None
            try:
                detail = json.loads(e.read().decode('utf-8', 'replace')).get('message')
            except (ValueError, AttributeError):
                pass
            if isinstance(detail, list):
                detail = '; '.join(map(str, detail))
            if e.code in (401, 403):
                raise AuthError(kodi.L(30615), e.code, detail)
            raise ApiError('HTTP {} for {}: {}'.format(e.code, path, detail or e.reason), e.code, detail)
        except (URLError, socket.timeout, OSError) as e:
            raise ApiError('{}: {}'.format(kodi.L(30614), e))
        payload = resp.read()
        resp.close()
        if not payload:
            return None
        try:
            return json.loads(payload.decode('utf-8'))
        except ValueError:
            raise ApiError('{}: non-JSON response for {}'.format(kodi.L(30614), path))

    def get(self, path, **params):
        return self.request('GET', path, params=params)

    def post(self, path, body=None, **params):
        return self.request('POST', path, params=params, body=body if body is not None else {})

    # ---------------------------------------------------------------- server
    def server_version(self):
        return parse_version(self.get('/server/version') or {})

    def me(self):
        return self.get('/users/me')

    def key_info(self):
        return self.get('/api-keys/me')

    # ---------------------------------------------------------------- sign-in
    def login(self, email, password):
        return self.request('POST', '/auth/login', body={'email': email, 'password': password})

    def create_key(self, token, name):
        return self.request('POST', '/api-keys', body={'name': name, 'permissions': SCOPES}, token=token)

    def logout(self, token):
        return self.request('POST', '/auth/logout', body={}, token=token)

    # ---------------------------------------------------------------- library
    def timeline_buckets(self, **filters):
        return self.get('/timeline/buckets', **_filters(filters))

    def timeline_bucket(self, time_bucket, **filters):
        cols = self.get('/timeline/bucket', timeBucket=time_bucket, **_filters(filters)) or {}
        return [from_bucket(row) for row in _rows(cols)]

    def albums(self):
        return self.get('/albums') or []

    def album(self, album_id):
        return self.get('/albums/{}'.format(quote(album_id)))

    def search(self, order='desc', limit=None, **filters):
        """All assets matching /search/metadata filters, following nextPage."""
        out = []
        page = 1
        while page:
            body = dict(filters, page=page, size=PAGE, order=order, withExif=True)
            res = (self.post('/search/metadata', body) or {}).get('assets') or {}
            out += [from_asset(a) for a in res.get('items') or []]
            if limit and len(out) >= limit:
                return out[:limit]
            page = int(res['nextPage']) if res.get('nextPage') else None
        return out

    def random(self, size=250):
        return [from_asset(a) for a in self.post('/search/random', {'size': size, 'withExif': True}) or []]

    # ------------------------------------------------------------------ media
    def media_url(self, path, **params):
        """Kodi fetches these itself; the API key rides along as a pipe header."""
        return '{}|x-api-key={}'.format(self.url(path, **params), quote(self.api_key or '', safe=''))

    def thumb_url(self, asset_id, size='thumbnail'):
        return self.media_url('/assets/{}/thumbnail'.format(asset_id), size=size, edited='true')

    def video_url(self, asset_id):
        return self.media_url('/assets/{}/video/playback'.format(asset_id))

    def original_url(self, asset_id):
        return self.media_url('/assets/{}/original'.format(asset_id))


def _filters(filters):
    out = {'visibility': 'timeline', 'withStacked': 'true'}
    out.update({k: ('true' if v is True else 'false' if v is False else v) for k, v in filters.items()})
    return out


def _rows(cols):
    """Timeline buckets come back column-wise; turn them into one dict per asset."""
    ids = cols.get('id') or []
    keys = [k for k, v in cols.items() if isinstance(v, list) and len(v) == len(ids)]
    return [{k: cols[k][i] for k in keys} for i in range(len(ids))]


def _iso(text):
    try:
        return datetime.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return None


def _seconds(value):
    """Immich 3 sends milliseconds; 2.x sent '0:01:02.345'."""
    if isinstance(value, (int, float)):
        return int(value // 1000)
    try:
        h, m, s = (value or '').split(':')
        return int(int(h) * 3600 + int(m) * 60 + float(s))
    except ValueError:
        return 0


def from_bucket(row):
    taken = _iso(row.get('fileCreatedAt'))
    if taken is not None:
        taken += timedelta(hours=row.get('localOffsetHours') or 0)
    return {'id': row['id'], 'image': bool(row.get('isImage')), 'taken': taken,
            'ratio': row.get('ratio') or 1.0, 'duration': _seconds(row.get('duration')),
            'city': row.get('city'), 'country': row.get('country'),
            'live': row.get('livePhotoVideoId'), 'favorite': bool(row.get('isFavorite'))}


def from_asset(a):
    exif = a.get('exifInfo') or {}
    width, height = exif.get('exifImageWidth') or a.get('width'), exif.get('exifImageHeight') or a.get('height')
    if exif.get('orientation') in ('5', '6', '7', '8', 5, 6, 7, 8):
        width, height = height, width
    return {'id': a['id'], 'image': a.get('type') != 'VIDEO', 'taken': _iso(a.get('localDateTime')),
            'ratio': (width / height) if width and height else 1.0,
            'duration': _seconds(a.get('duration')), 'city': exif.get('city'),
            'country': exif.get('country'), 'live': a.get('livePhotoVideoId'),
            'favorite': bool(a.get('isFavorite'))}
