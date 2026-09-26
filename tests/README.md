# Offline tests

`stubs/` replaces Kodi's `xbmc*` modules so the add-on can run outside Kodi. Settings come from
the flatpak Kodi profile, or from `IMMICH_URL` and `IMMICH_KEY`; the add-on profile is a temp
directory.

```
PYTHONPATH=tests/stubs:. python3 -c 'from resources.lib import plugin; \
    plugin.run(["plugin://x/", "1", "?action=timeline"])'
```

Listed items are in `xbmcplugin.ITEMS`, dialogs answer from `xbmcgui.ANSWERS`.
