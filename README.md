# Viewarr plugin for Dispatcharr

The server half of [Viewarr](https://github.com/petebutty/viewarr), the Android TV app for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

Viewarr works without this plugin, but then every TV does all the work itself. With the plugin, Dispatcharr does that work **once for the whole house** and leaves the answers in each person's own Dispatcharr storage. Each TV just reads them, so:

- the TVs are quicker, which helps most on weaker boxes;
- sport recordings and live scores keep going with every TV switched off.

If the plugin is missing or stops, the TVs go back to doing the work themselves. Nothing breaks.

## What it does

### Every 5 minutes

- **Live now.** It goes through your guide and sports and event groups, and keeps only real live sport:
  - entries the guide marks as live;
  - event channels from 30 minutes before the start;
  - no filler, no "No fixtures today" and no stream-name channels.

  Each TV then applies its own choices: groups, sports, leagues and "Only what I follow".
- **Where every followed game is on.** It collects the teams and leagues anyone in the house follows, then gets their fixtures and scores from ESPN. For each game it works out which of your channels shows it (see [UK football on TV](#uk-football-on-tv)). This feeds Viewarr's Sports page, the game cards and **Watch**.
- **Sport recordings.** For **Record all &lt;team&gt;**, or **Record** on a game the guide doesn't list yet, it creates the recording in Dispatcharr as soon as the guide lists the game. This works even with every TV off.
- **Title logos.** With a TMDB key, it finds the show and film logos for the next day's guide, about 250 new titles a round. Every TV gets them without looking them up itself.
- **Watch sync (optional, for people without Trakt).** It keeps someone's IPTV watch progress in step with their own Emby, Jellyfin or Plex, in both directions, matched by TMDB id. Finish a film on IPTV and it's ticked in Plex.
  - It only touches titles they've played on IPTV.
  - Each person switches it on for themselves in Viewarr → Settings → Playback → Watch progress.
  - If you use Trakt, leave it off: Trakt already does this.

### Every 45 seconds while a followed game is live

- **Score alerts and live scores:** kick-off, goals, half-time, full time and the result, with sport-aware wording ("Tip-off", "Puck drop", "TOUCHDOWN"). Basketball gets no alert for every basket.
- **Alerts are off for everyone** until they choose a team in Viewarr → Settings → Sports. Each TV shows only the alerts its own person switched on.
- The same scores feed Viewarr's news ticker.

### When asked

- **Backups** of an admin's whole Viewarr setup (Viewarr → Settings → Users & profiles → Backups). The settings are saved, and sign-ins are encrypted with the admin's own Dispatcharr key. The newest 30 are kept, in Dispatcharr's plugin data folder, reachable only by admins.
- **Sports service status** and **Refresh sports now** buttons on the plugin's page in Dispatcharr.

## UK football on TV

The plugin has UK football TV listings built in, so Viewarr knows which channel shows a match **weeks before your guide lists it**. For each game it looks, in order, at:

1. **Your Dispatcharr guide:** the programme on a channel that names both teams.
2. **UK football TV listings** from [live-footballontv.com](https://www.live-footballontv.com/): every televised match for the next 30 days, on Sky Sports, TNT Sports, Premier Sports, BBC, ITV, Amazon Prime Video and the rest. The listings channel is then matched to the channel in your lineup.
3. **The Sky TV schedule** for Sky Sports, TNT Sports, Premier Sports, BBC, ITV, Channel 4, Channel 5, S4C, BBC Alba, MUTV and LFCTV.
4. **Event channels** named after the match, such as your provider's event feeds or Event Channel Managarr's channels.
5. **The club's own channel** (for example `SPFL - CELTIC TV`, MUTV, LFC TV) for league games that aren't on UK TV, such as Scottish Saturday 3pm kick-offs.

Games caught by the Saturday 3pm blackout are marked **Not on UK TV**, rather than pointing at the wrong channel.

Leagues covered: Scottish Premiership, Championship and cups; Premier League, EFL and cups; Champions, Europa and Conference League; internationals; the big European leagues; MLS; rugby union and league; NBA, NFL, NHL and MLB; F1, golf, tennis and UFC.

You don't need the [UK Sport on TV](https://github.com/petebutty/uk-sport-on-tv) plugin for Viewarr. Use it as well if you want the matches written into Dispatcharr's guide for every other app too.

## Install

1. Dispatcharr → **Plugins** → **Import**, and choose `viewarr-plugin-<version>.zip` from the [releases](https://github.com/petebutty/viewarr-plugin/releases).
2. **Enable** it. Dispatcharr asks you to approve what it uses:
   - **persistent service:** it runs every few minutes;
   - **outbound network:** it reads ESPN, TMDB, the Sky schedule and the UK listings;
   - **user data:** it reads each person's Viewarr setup and leaves their answers there.

That's it. Nobody needs to set anything up in Viewarr: each TV finds the plugin by itself. Each person's follows, Live now groups and alert choices reach the plugin through their synced Viewarr setup.

## Settings (all optional)

| Setting | What it does |
|---|---|
| **Live now: extra channel groups to search** | The plugin always searches groups whose names sound like sport (Sports, Football, Events, Box Office, EPL…) and Event Channel Managarr's groups. Add others here, such as your provider's event-feed groups. Easier: switch a group on in Viewarr → Settings → Live TV → Live now groups, and the plugin searches it from the next round. |
| **TMDB key (for title logos)** | Your TMDB API key or read token. Without it, each TV looks up title logos itself. Note: any signed-in Dispatcharr user can read plugin settings, so use a key you don't mind sharing with your household. |

## Polite to the sources

- **UK listings:** one page, fetched at most every 6 hours. After a "too many requests" or "unavailable" reply it waits an hour. A failed or empty fetch keeps the last good listings.
- **ESPN:** only the leagues and teams someone follows. Live scores are checked only while a followed game is on.
- **TMDB:** about 250 titles a round. Each title is looked up once and remembered.
- **User-Agent:** every request identifies itself with a link to this repo.

## Requirements

- Dispatcharr 0.34 or newer.
- The latest Viewarr for every feature. Older versions use what they understand.

## Licence

MIT. See [LICENSE](LICENSE).
