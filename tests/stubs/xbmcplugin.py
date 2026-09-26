"""Kodi stub: directory items land in ITEMS, end/resolve state in META."""
ITEMS = []; META = {}
SORT_METHOD_NONE, SORT_METHOD_LABEL_IGNORE_THE = 0, 1
def addDirectoryItem(h, url, li, isFolder=False): ITEMS.append((url, li, isFolder))
def endOfDirectory(h, ok=True, cacheToDisc=True): META['end'] = ok
def addSortMethod(h, m): pass
def setContent(h, c): META['content'] = c
def setPluginCategory(h, c): META['category'] = c
def setResolvedUrl(h, ok, li): META['resolved'] = (ok, li)
