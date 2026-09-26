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
class Player: pass
