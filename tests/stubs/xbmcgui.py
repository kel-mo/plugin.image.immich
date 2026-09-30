"""Kodi stub: dialogs print, record in LOG and answer from ANSWERS."""
NOTIFICATION_ERROR = 'error'; INPUT_ALPHANUM = 0; ALPHANUM_HIDE_INPUT = 2
ANSWERS = {'select': [], 'yesno': [], 'input': []}
LOG = []
class Dialog:
    def notification(self, h, m, i=None, t=0): LOG.append(('notify', m)); print('NOTIFY', m)
    def select(self, h, items, preselect=-1, **kw): LOG.append(('select', h, items, preselect)); print('SELECT', h, items, 'pre', preselect); return ANSWERS['select'].pop(0) if ANSWERS['select'] else -1
    def yesno(self, h, m, **kw): LOG.append(('yesno', m)); print('YESNO', m); return ANSWERS['yesno'].pop(0) if ANSWERS['yesno'] else False
    def textviewer(self, h, t, **kw): LOG.append(('text', t)); print('TEXT', t)
    def input(self, h, d='', **kw): return ANSWERS['input'].pop(0) if ANSWERS['input'] else ''
    def ok(self, h, m): print('OK', m)
class _Tag:
    def __getattr__(self, n): return lambda *a, **k: None
class ListItem:
    def __init__(self, label='', label2='', path='', offscreen=False): self.label = label; self.label2 = label2; self.mime = ''; self.art = {}; self.props = {}; self.ctx = []; self.path = path
    def setArt(self, a): self.art.update(a)
    def setLabel(self, l): self.label = l
    def setLabel2(self, l): self.label2 = l
    def setProperty(self, k, v): self.props[k] = v
    def addContextMenuItems(self, c): self.ctx += c
    def setMimeType(self, m): self.mime = m
    def __getattr__(self, n): return lambda *a, **k: _Tag()
class DialogProgress:
    def create(self, *a): pass
    def update(self, *a): pass
    def iscanceled(self): return False
    def close(self): pass
class WindowDialog:
    def __init__(self, *a, **k): pass
    def addControl(self, c): pass
    def show(self): pass
    def close(self): pass
class ControlImage:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return lambda *a, **k: None
class ControlLabel(ControlImage): pass
class ControlTextBox(ControlImage): pass
class ControlButton(ControlImage): pass
class Window:
    PROPS = {}
    def __init__(self, i=0): pass
    def getProperty(self, k): return Window.PROPS.get(k, '')
    def setProperty(self, k, v): Window.PROPS[k] = v
    def clearProperty(self, k): Window.PROPS.pop(k, None)
class WindowXMLDialog(WindowDialog):
    def __init__(self, *a, **k): self.props = {}
    def setProperty(self, k, v): self.props[k] = v
    def getProperty(self, k): return self.props.get(k, '')
    def getControl(self, i): return ControlImage()
