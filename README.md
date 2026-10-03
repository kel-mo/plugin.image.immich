<img src="resources/icon.png" alt="Immich for Kodi icon" width="128" align="right">

# Immich for Kodi

View the photos and videos of your self-hosted [Immich](https://immich.app)
server on Kodi. Browse *On this day*, the timeline, people and pets, places,
albums and favourites, or search by description ("beach at sunset"). Or sit
back and watch them as a slideshow that crossfades and slowly pans and zooms
across each photo.

View only: the add-on never changes anything on the server.

Needs Immich 2.0 or newer, and Kodi 22.

## Install

1. In Kodi, turn on *Settings → System → Add-ons → Unknown sources*.
2. Download the `repository.kelmo-<version>.zip` linked at the top of
   <https://kel-mo.github.io/repository.kelmo/>.
3. *Add-ons → Install from zip file* and pick that zip.
4. *Install from repository → kel-mo Add-on Repository → Picture add-ons →
   Immich → Install*.

## Sign in

1. In Immich, open *Account settings → API keys* and create a key. It needs
   at least `asset.read`, `asset.view`, `album.read` and `user.read`. Add
   `person.read` for people, `memory.read` for *On this day* and
   `asset.download` for full resolution photos.
2. Open Immich under *Pictures* in Kodi and choose *Sign in*, or open the
   add-on settings.
3. Enter your server address and the API key.

## Slideshow

Pick *Play slideshow* at the top of any list of photos, or from the context
menu of a year, month, person, place or album. *Shuffle everything* plays random photos from the whole
library. During the slideshow:

| Key | Does |
|---|---|
| Left / Right | Previous / next |
| Select, Play, Pause | Pause or resume |
| Info | Caption, caption and photo details, or nothing |
| Back | Close |

Time per photo, crossfade, pan and zoom, shuffle and videos are in the
add-on settings. *Smooth zoom* stops fine detail such as sand or leaves from
shimmering while photos zoom; it needs a Kodi build with mipmapped textures
(the `mipmap` texture attribute) and looks worse without one.

## On the home screen

- Estuary: open *On this day* (or anything else) in the add-on, open its
  context menu and choose *Add to favourites*. It then shows under Home →
  Favourites.
- Skins with custom widgets: use `plugin://plugin.image.immich/?action=today`
  for a row of today's memories.
- The menu shows one of your photos with the folder's name over it, a new one
  each day. Turn it off with *Photo tiles* in the add-on's Browsing settings.

---

GPL-2.0-or-later. The thumbhash decoder is ported from
[evanw/thumbhash](https://github.com/evanw/thumbhash) (MIT). Not affiliated with the
Immich project.
