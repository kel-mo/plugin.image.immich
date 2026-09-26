<img src="resources/icon.png" alt="Immich for Kodi icon" width="128" align="right">

# Immich for Kodi

View the photos and videos of your self-hosted [Immich](https://immich.app)
server on Kodi. Browse the timeline and albums, or sit back and watch them as a
slideshow that crossfades and slowly pans and zooms across each photo.

View only: the add-on never changes anything on the server.

Needs Immich 2.0 or newer, and Kodi 21 or 22.

## Install

1. In Kodi, turn on *Settings → System → Add-ons → Unknown sources*.
2. Download the `repository.kelmo-<version>.zip` linked at the top of
   <https://kel-mo.github.io/repository.kelmo/>.
3. *Add-ons → Install from zip file* and pick that zip.
4. *Install from repository → kel-mo Add-on Repository → Picture add-ons →
   Immich → Install*.

## Sign in

1. In Immich, open *Account settings → API keys* and create a key. It needs
   at least `asset.read`, `asset.view`, `album.read` and `user.read`, plus
   `asset.download` if you want full resolution photos.
2. Open Immich under *Pictures* in Kodi and choose *Sign in*, or open the
   add-on settings.
3. Enter your server address and the API key.

## Slideshow

Pick *Play slideshow* in any month or album, or from the context menu of a
year, month or album. *Shuffle everything* plays random photos from the whole
library. During the slideshow:

| Key | Does |
|---|---|
| Left / Right | Previous / next |
| Select, Play, Pause | Pause or resume |
| Info | Show or hide date and place |
| Back | Close |

Time per photo, crossfade, pan and zoom, shuffle and videos are in the
add-on settings.

---

GPL-2.0-or-later. Not affiliated with the Immich project.
