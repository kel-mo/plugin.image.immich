"""Offline regression tests against the fake server; never the real one or the real Kodi settings."""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = tempfile.mkdtemp(prefix='immich-tests-')
tempfile.tempdir = SCRATCH                      # the stub profile goes in here
sys.dont_write_bytecode = True
sys.path[:0] = [os.path.join(ROOT, 'tests', 'stubs'), os.path.join(ROOT, 'tests'), ROOT]

import mock_immich


def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


class Quiet(mock_immich.Handler):
    def log_message(self, fmt, *args):
        pass


class Broken(BaseHTTPRequestHandler):
    """/api/401 and /api/403 refuse, /api/stall stops mid-body, anything else is cut off mid-chunk."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/401', '/api/403'):
            body = b'{"message": "Missing required permission: album.read"}'
            self.send_response(int(path[-3:]))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        if path == '/api/stall':
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.write(b'{"a"')
            self.wfile.flush()
            time.sleep(1.5)
            return
        self.send_header('Content-Type', 'image/png')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        self.wfile.write(b'10000\r\n' + b'x' * 5000)
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)


MOCK_SERVER, MOCK = serve(Quiet)
BROKEN_SERVER, BROKEN = serve(Broken)
HOME = os.environ.get('HOME')
os.environ['HOME'] = SCRATCH                    # hides the real Kodi profile from the stub
os.environ['IMMICH_URL'] = MOCK
os.environ['IMMICH_KEY'] = mock_immich.KEY

import xbmcaddon

from resources.lib import api, kodi, plugin, viewer

SCHEMES = {'immich': 'http://immich', 'immich:2283': 'http://immich:2283', 'photos.local': 'http://photos.local',
           'nas.lan:2283': 'http://nas.lan:2283', '192.168.1.5:2283': 'http://192.168.1.5:2283',
           '127.0.0.1': 'http://127.0.0.1', '169.254.1.1': 'http://169.254.1.1',
           '[fe80::1]:2283': 'http://[fe80::1]:2283', 'fe80::1': 'http://fe80::1',
           '8.8.8.8': 'https://8.8.8.8', '8.8.8.8:2283': 'https://8.8.8.8:2283',
           '[2606:4700::1111]:2283': 'https://[2606:4700::1111]:2283',
           'photos.example.com': 'https://photos.example.com',
           'photos.example.com:2283': 'https://photos.example.com:2283',
           'http://8.8.8.8:2283/': 'http://8.8.8.8:2283', 'https://nas.lan/api': 'https://nas.lan'}


def tearDownModule():
    for server in (MOCK_SERVER, BROKEN_SERVER):
        server.shutdown()
        server.server_close()
    if HOME is not None:
        os.environ['HOME'] = HOME
    shutil.rmtree(SCRATCH, ignore_errors=True)


def make_viewer(assets):
    return viewer.Viewer('script-immich-viewer.xml', ROOT, 'default', '1080i',
                         client=api.ImmichClient(), assets=assets, start=0, autoplay=True)


class CutOff:
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self):
        raise IncompleteRead(b'')

    def close(self):
        self.closed = True


class Harness(unittest.TestCase):
    def test_fake_server_only(self):
        self.assertTrue(xbmcaddon._S.startswith(SCRATCH))
        self.assertEqual((kodi.setting('server_url'), kodi.setting('api_key')), (MOCK, mock_immich.KEY))
        self.assertEqual(api.ImmichClient().server_version(), (3, 2, 2))


class ReadErrors(unittest.TestCase):
    def test_truncated_body(self):
        with self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k').get('/assets')

    def test_stalled_body(self):
        with self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k', timeout=0.5).get('/stall')

    def test_response_closed(self):
        resp = CutOff()
        with mock.patch.object(api, 'urlopen', return_value=resp), self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k').get('/assets')
        self.assertTrue(resp.closed)

    def test_callers_degrade(self):
        probe = plugin.Probe()
        self.assertIs(probe(api.ImmichClient(BROKEN, 'k').albums, True, False), False)
        self.assertTrue(probe.offline)
        window = make_viewer([])
        window.raws, window.quick = {}, api.ImmichClient(BROKEN, 'k')
        self.assertIsNone(window.raw({'id': 'c4ca4238-a0b9-2382-0dcc-509a6f75849b'}))


if __name__ == '__main__':
    unittest.main()
