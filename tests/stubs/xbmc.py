"""Kodi stub: logs and builtins are printed and recorded in BUILTINS."""
LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3
BUILTINS = []
def log(msg, level=0): print('LOG', level, msg)
def executebuiltin(s, wait=False): BUILTINS.append(s); print('BUILTIN', s)
def executeJSONRPC(s): return '{"result": {}}'
def getCondVisibility(s): return False
class Monitor:
    def abortRequested(self): return False
    def waitForAbort(self, t=0): return False
def getInfoLabel(s): return 'Test Kodi'
def getRegion(k): return {'dateshort': '%d/%m/%Y', 'time': '%H:%M:%S'}.get(k, '')
def getLocalizedString(i): return {21: 'January', 29: 'September', 28: 'August'}.get(i, 'Month%d' % i)
class Player:
    def isPlaying(self): return False
    def isPlayingVideo(self): return False
    def play(self, *a, **k): pass
    def stop(self): pass
def getSkinDir(): return 'skin.estuary'
