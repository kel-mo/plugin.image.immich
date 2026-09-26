#!/usr/bin/env python3
"""Tiny fake Immich server for offline tests: python3 tests/mock_immich.py [port]

API key "test-key"; login kodi@example.com / secret. Images are generated PNGs, videos a test clip.
"""
import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY = 'test-key'
RATIOS = [1.5, 0.75, 1.333, 1.0, 2.4, 0.5625]
VIDEO = os.path.join('/tmp', 'mock-immich-clip.mp4')


def make_assets():
    assets, start = [], datetime(2026, 9, 20, 18, 0)
    for n in range(160):
        taken = start - timedelta(days=n * 7, hours=n % 5)
        aid = hashlib.md5(str(n).encode()).hexdigest()
        aid = '{}-{}-{}-{}-{}'.format(aid[:8], aid[8:12], aid[12:16], aid[16:20], aid[20:32])
        assets.append({'id': aid, 'n': n, 'video': n % 9 == 4, 'ratio': RATIOS[n % len(RATIOS)],
                       'taken': taken, 'city': ['Perth', 'Kyoto', 'Hobart', None][n % 4],
                       'country': ['Australia', 'Japan', 'Australia', None][n % 4]})
    return assets


ASSETS = make_assets()
BY_ID = {a['id']: a for a in ASSETS}
ALBUMS = [{'id': 'album-1', 'albumName': 'Holidays', 'members': ASSETS[0:40]},
          {'id': 'album-2', 'albumName': 'Garden', 'members': ASSETS[40:52]},
          {'id': 'album-3', 'albumName': 'Empty', 'members': []}]
PEOPLE = [{'id': 'person-{}'.format(i), 'name': name, 'isHidden': False, 'isFavorite': False,
           'members': [a for a in ASSETS if a['n'] % 5 == i]}
          for i, name in enumerate(['Alex', '', 'Sam', '', ''])]
FAVOURITES = [a for a in ASSETS if a['n'] % 5 == 0]


def filtered(body):
    items = ASSETS
    if body.get('albumIds'):
        items = next((al['members'] for al in ALBUMS if al['id'] == body['albumIds'][0]), [])
    if body.get('personIds'):
        items = next((p['members'] for p in PEOPLE if p['id'] == body['personIds'][0]), [])
    if body.get('city'):
        items = [a for a in items if a['city'] == body['city']]
    if body.get('isFavorite'):
        items = [a for a in items if a in FAVOURITES]
    return list(reversed(items)) if body.get('order') == 'asc' else items


def paged(items, body):
    page, size = int(body.get('page') or 1), int(body.get('size') or 250)
    chunk = items[(page - 1) * size:page * size]
    nxt = str(page + 1) if page * size < len(items) else None
    return {'assets': {'items': [asset_dto(a) for a in chunk], 'nextPage': nxt, 'count': len(chunk),
                       'total': len(items)}}


def bucket_key(a):
    return a['taken'].strftime('%Y-%m-01')


def columnar(items):
    return {'id': [a['id'] for a in items], 'isImage': [not a['video'] for a in items],
            'ratio': [a['ratio'] for a in items],
            'fileCreatedAt': [(a['taken'] - timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%S.000Z') for a in items],
            'localOffsetHours': [8 for _ in items],
            'duration': [5000 if a['video'] else None for a in items],
            'livePhotoVideoId': [None for _ in items], 'isFavorite': [a['n'] % 5 == 0 for a in items],
            'city': [a['city'] for a in items], 'country': [a['country'] for a in items]}


def asset_dto(a):
    w = 1440 if a['ratio'] >= 1 else int(1440 * a['ratio'])
    return {'id': a['id'], 'type': 'VIDEO' if a['video'] else 'IMAGE',
            'localDateTime': a['taken'].strftime('%Y-%m-%dT%H:%M:%S.000Z'), 'duration': 5000 if a['video'] else None,
            'width': w, 'height': int(w / a['ratio']), 'isFavorite': a['n'] % 5 == 0,
            'exifInfo': {'city': a['city'], 'country': a['country'],
                         'state': {'Perth': 'Western Australia', 'Hobart': 'Tasmania'}.get(a['city'])},
            'thumbhash': None}


def png(a, long_side):
    w, h = (long_side, int(long_side / a['ratio'])) if a['ratio'] >= 1 else (int(long_side * a['ratio']), long_side)
    seed = hashlib.md5(a['id'].encode()).digest()
    c1, c2 = seed[:3], seed[3:6]
    rows = []
    for y in range(h):
        t = y / max(h - 1, 1)
        base = bytes(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))
        if (y * 10 // h) % 3 == 1:
            stripe = bytes(255 - v for v in base)
            block = max(w // 16, 1)
            row = ((base * block + stripe * block) * (w // (2 * block) + 1))[:w * 3]
        else:
            row = base * w
        rows.append(b'\0' + row)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write('mock: ' + fmt % args + '\n')

    def send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self, q):
        return self.headers.get('x-api-key') == KEY or q.get('apiKey') == KEY

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == '/api/server/version':
            return self.send(200, {'major': 3, 'minor': 2, 'patch': 2})
        if not self.authed(q):
            return self.send(401, {'message': 'Authentication required'})
        items = ASSETS
        if q.get('albumId'):
            items = next((al['members'] for al in ALBUMS if al['id'] == q['albumId']), [])
        if q.get('isFavorite') == 'true':
            items = [a for a in items if a['n'] % 5 == 0]
        if p == '/api/users/me':
            return self.send(200, {'id': 'u1', 'name': 'Kodi Tester', 'email': 'kodi@example.com'})
        if p == '/api/api-keys/me':
            return self.send(200, {'id': 'k1', 'name': 'test', 'permissions': ['all']})
        if p == '/api/timeline/buckets':
            counts = {}
            for a in items:
                counts[bucket_key(a)] = counts.get(bucket_key(a), 0) + 1
            return self.send(200, [{'timeBucket': k, 'count': v} for k, v in sorted(counts.items(), reverse=True)])
        if p == '/api/timeline/bucket':
            return self.send(200, columnar([a for a in items if bucket_key(a) == q.get('timeBucket')]))
        if p == '/api/albums':
            return self.send(200, [{'id': al['id'], 'albumName': al['albumName'], 'assetCount': len(al['members']),
                                    'albumThumbnailAssetId': al['members'][0]['id'] if al['members'] else None,
                                    'shared': False, 'description': '',
                                    'startDate': al['members'][-1]['taken'].isoformat() if al['members'] else None,
                                    'endDate': al['members'][0]['taken'].isoformat() if al['members'] else None}
                                   for al in ALBUMS])
        if p == '/api/people':
            return self.send(200, {'people': [{k: v for k, v in pe.items() if k != 'members'} for pe in PEOPLE],
                                   'total': len(PEOPLE), 'hidden': 0, 'hasNextPage': False})
        if p == '/api/search/cities':
            seen = {}
            for a in ASSETS:
                if a['city'] and a['city'] not in seen:
                    seen[a['city']] = asset_dto(a)
            return self.send(200, list(seen.values()))
        if p == '/api/memories':
            return self.send(200, [{'id': 'm{}'.format(y), 'type': 'on_this_day', 'data': {'year': y},
                                    'memoryAt': '{}-09-26T00:00:00.000Z'.format(y),
                                    'assets': [asset_dto(a) for a in ASSETS[k:k + 3]]}
                                   for k, y in ((3, 2025), (60, 2024))])
        parts = p.split('/')
        if len(parts) == 5 and parts[2] == 'people' and parts[4] == 'thumbnail':
            pe = next((x for x in PEOPLE if x['id'] == parts[3]), None)
            if pe:
                return self.send(200, png(pe['members'][0], 250), 'image/png')
        if len(parts) == 4 and parts[2] == 'assets' and parts[3] in BY_ID:
            a = BY_ID[parts[3]]
            dto = asset_dto(a)
            dto['originalMimeType'] = 'image/avif' if a['n'] % 7 == 3 else 'image/jpeg'
            dto['originalFileName'] = 'IMG_{:04d}.jpg'.format(a['n'])
            dto['exifInfo'].update(make='Google', model='Pixel 9 Pro XL', fNumber=1.7, iso=100, exposureTime='1/120',
                                   lensModel='Pixel 9 Pro XL back camera 6.9mm f/1.68', focalLength=6.9)
            dto['people'] = [{'name': p['name']} for p in PEOPLE if a in p['members'] and p['name']]
            return self.send(200, dto)
        if len(parts) >= 5 and parts[2] == 'assets' and parts[3] in BY_ID:
            a = BY_ID[parts[3]]
            if parts[4] == 'thumbnail':
                return self.send(200, png(a, 250 if q.get('size') == 'thumbnail' else 1440), 'image/png')
            if parts[4] == 'video':
                if not os.path.exists(VIDEO):
                    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                                    'testsrc=duration=5:size=1280x720:rate=30', '-pix_fmt', 'yuv420p', VIDEO])
                with open(VIDEO, 'rb') as f:
                    return self.send(200, f.read(), 'video/mp4')
        return self.send(404, {'message': 'Not found'})

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        if u.path == '/api/auth/login':
            if body.get('email') == 'kodi@example.com' and body.get('password') == 'secret':
                return self.send(201, {'accessToken': 'session-token', 'name': 'Kodi Tester'})
            return self.send(401, {'message': 'Incorrect email or password'})
        if u.path == '/api/api-keys':
            if self.headers.get('Authorization') != 'Bearer session-token':
                return self.send(401, {'message': 'Authentication required'})
            return self.send(201, {'secret': KEY, 'apiKey': {'id': 'k1', 'name': body.get('name')}})
        if u.path == '/api/auth/logout':
            return self.send(200, {'successful': True})
        if not self.authed({}):
            return self.send(401, {'message': 'Authentication required'})
        if u.path == '/api/search/random':
            return self.send(200, [asset_dto(a) for a in random.sample(ASSETS, min(int(body.get('size') or 250),
                                                                                  len(ASSETS)))])
        if u.path == '/api/search/metadata':
            return self.send(200, paged(filtered(body), body))
        if u.path == '/api/search/smart':
            return self.send(200, paged([a for a in ASSETS if not a['video']], body))
        return self.send(404, {'message': 'Not found'})


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2283
    print('mock Immich on http://127.0.0.1:{}'.format(port))
    ThreadingHTTPServer(('', port), Handler).serve_forever()
