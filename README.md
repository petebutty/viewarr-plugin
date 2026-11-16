# Viewarr plugin for Dispatcharr

House settings for [Viewarr](https://github.com/petebutty/viewarr), the Android TV app for Dispatcharr.

**Outside addresses**: the home address and outside address of each of your servers (Dispatcharr, Navidrome,
Seerr, Jellyfin, addons such as AIOStreams / AIOMetadata…). Every Viewarr signed in to this Dispatcharr reads them
when it starts, so changing an address here updates every TV, including friends' and family's in other houses. A TV
that can't reach a home address uses its outside one.

## Install

Dispatcharr → Plugins → Import, and choose `viewarr-plugin-<version>.zip` from the releases. Enable it, then set
the addresses here or in Viewarr (Settings → Profiles → Away addresses; admins only).

## Privacy

Dispatcharr lets every signed-in user read plugin settings, so this plugin only ever holds addresses: never
passwords, API keys or addon links (those stay encrypted in each person's own Viewarr setup).
