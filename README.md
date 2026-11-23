# Viewarr plugin for Dispatcharr

The server half of [Viewarr](https://github.com/petebutty/viewarr), the Android TV app for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

Viewarr works without this plugin, but then every TV does all the work itself. With the plugin, Dispatcharr does that work **once for the whole house** and leaves the answers in each person's own Dispatcharr storage. Each TV just reads them, so:

- the TVs are quicker, which helps most on weaker boxes;
- sport recordings and live scores keep going with every TV switched off.

If the plugin is missing or stops, the TVs go back to doing the work themselves. Nothing breaks.

## What it does

### Every 5 minutes

- **Best stream first.** For every channel with more than one stream, the best stream is put first in Dispatcharr, so every app and TV in the house starts on it. See [Best stream first](#best-stream-first).
- **Measuring streams (at night).** Every stream's real picture size and codec, measured with ffprobe in a quiet window so the ranking uses facts rather than name tags. See [Measuring streams](#measuring-streams).
- **Just added.** Once a day it reads what arrived on each streaming service in the UK over the last fortnight (older films newly added, and new seasons of shows), from JustWatch's web feed, and hands it to every TV for Viewarr's "<service> · Just added" rows. TMDB can't do this: it knows which service has a title, not when it arrived.
- **Reports from the TVs.** Wrong guide, guide times out, dead stream, wrong logo or name, reported from Viewarr, are fixed straight away and can be undone. See [Reports from the TV](#reports-from-the-tv).
- **Live now.** It goes through your guide and sports and event groups, and keeps only real live sport:
  - entries the guide marks as live;
  - event channels from 30 minutes before the start;
  - no filler, no "No fixtures today" and no stream-name channels.

  Each TV then applies its own choices: groups, sports, leagues and "Only what I follow".
- **Where every followed game is on.** It collects the teams and leagues anyone in the house follows, then gets their fixtures and scores from ESPN. For each game it works out which of your channels shows it (see [UK football on TV](#uk-football-on-tv)). This feeds Viewarr's Sports page, the game cards and **Watch**.
- **Sport recordings.** For **Record all &lt;team&gt;**, or **Record** on a game the guide doesn't list yet, it creates the recording in Dispatcharr as soon as the guide lists the game. This works even with every TV off.
- **IPTV progress sync (optional, for people without Trakt).** It keeps someone's IPTV watch progress in step with their own Emby, Jellyfin or Plex, in both directions, matched by TMDB id. Finish a film on IPTV and it's ticked in Plex.
  - It only touches titles they've played on IPTV.
  - Each person switches it on for themselves in Viewarr → Settings → Playback → Watch progress.
  - If you use Trakt, leave it off: Trakt already does this.

### Every 45 seconds while a followed game is live

- **Score alerts and live scores:** kick-off, goals, half-time, full time and the result, with sport-aware wording ("Tip-off", "Puck drop", "TOUCHDOWN"). Basketball gets no alert for every basket.
- **Alerts are off for everyone** until they choose a team in Viewarr → Settings → Sports. Each TV shows only the alerts its own person switched on.
- The same scores feed Viewarr's news ticker.

### Every second, while a TV watches live TV

- **Live timeshift:** pause, rewind and fast-forward any live channel, not only ones with catch-up. While a TV watches a channel, ffmpeg copies it from Dispatcharr's own proxy (which shares the provider connection with everyone watching, so no extra connection) into a sliding HLS playlist of the last couple of hours, under `/data/logos/viewarr-ts/<random token>/` (see [where the buffer is kept](#live-timeshift-where-the-buffer-is-kept)). Every TV on that channel shares the one copy. Live TV plays as usual; only a pause or rewind moves the TV onto the copy. A TV asks through its own Dispatcharr app data (`viewarr_timeshift_req`), which the plugin checks every second, and reads the answer from `viewarr_timeshift`. The copy stops, and its files are deleted, as soon as no TV wants it (at once on a channel change, or a minute after a TV goes quiet). Each person can switch it off in Viewarr → Settings → Live TV.

### Twice a day (optional, off by default)

- **TV guide (Sky).** Sky's own listings for your region, matched to your channels, written as an XMLTV guide and added to Dispatcharr as the guide source **Viewarr: Sky guide**. Nothing in your channels' guide mapping changes until you say so. See [TV guide (Sky)](#tv-guide-sky).
- **Channel logos.** Fills in missing or broken channel logos (or replaces them all, if you choose) from Sky's own logos for channels the TV guide matched, then from the [tv-logos](https://github.com/tv-logo/tv-logos) collection for the rest of your lineup. Once a day and after each guide fetch; every change can be put back. See [Channel logos](#channel-logos).

### When asked

- **Backups** of an admin's whole Viewarr setup (Viewarr → Settings → Users & profiles → Backups). The settings are saved, and sign-ins are encrypted with the admin's own Dispatcharr key. The newest 30 are kept, in Dispatcharr's plugin data folder, reachable only by admins.
- **Sports service status** and **Refresh sports now** buttons on the plugin's page in Dispatcharr (a refresh is queued: the service runs the round within a minute).
- **Preview stream order** (shows what would change now, changing nothing) and **Put stream order back**, **Undo a change**, **Recent changes from reports** and **Measure streams now** buttons (see below).
- **TV guide status**, **Fetch the TV guide now** and **Compare guide with another source** buttons (see [TV guide (Sky)](#tv-guide-sky)).
- **Channel logos status** and **Preview channel logos** buttons (see [Channel logos](#channel-logos)).

## Best stream first

Dispatcharr plays a channel's streams in their saved order and only moves on when one fails, so the first stream is the one everybody gets. With **Best stream first** on (it is by default), the plugin ranks the streams of every channel that has more than one, every 5 minutes, and puts the best first:

- **Quality tier**, from the resolution Dispatcharr measured when it has one (see [Measuring streams](#measuring-streams)), else from the stream's name: FHD / 1080, or no tag at all, scores 3; HD / 720 scores 2; SD / 576 / 480 scores 1. 4K / UHD / 2160 scores 0.5 and goes last, because most lines can't carry it. An untagged name counts as full HD because a main provider's streams are often untagged and usually 1080p; the measured picture overrides the name either way.
- **Health**, from what the TVs saw. Each TV keeps the failures and the last good minute of every stream it played, in its user's Dispatcharr storage, and the plugin merges them for the house. A failure in the last day, or three in a week, costs 10 points; a good minute in the last week earns 1; a stream Dispatcharr marks stale costs 20. A **dead** report from a TV counts as a failure.
- **Score** = health + tier × 2. Ties keep the current order.

A channel is only moved when its best stream beats the current first by a whole point, and at most once an hour, so the order doesn't flap. The order each channel had before the plugin first touched it is remembered, and **Put stream order back** restores every channel at once.

Left alone: channels with a single stream, and channels in the groups [Event Channel Managarr](https://github.com/PiratesIRC/Dispatcharr-Event-Channel-Managarr-Plugin) manages (it orders its own). To pin one channel, add its id to the `keep` list in `_data/viewarr/streamorder.json`.

## Measuring streams

Dispatcharr only records a stream's picture size and codec (`stream_stats`, shown in Viewarr's Sources menu) when the stream plays through its proxy, so most streams are never measured and Best stream first has to trust the names. With **Measure streams at night** on (the default), the plugin measures them itself:

- **When:** only inside the **Measuring window** (default 01:00-07:00, in the time zone set in Dispatcharr's own settings), one stream at a time, a few seconds each. Empty window: never on a schedule.
- **What:** every stream linked to a real channel whose stats are missing or more than 30 days old. Event Channel Managarr's groups are never measured.
- **Order:** the channel groups in **Measure these channel groups first** (default: Entertainment, Movies, Sports, News, Kids, Music, UK Regional, Club TV), in that order, each group finished before the next; then any other group alphabetically, unless **Measure only the listed groups** is on. Within a group, the **Provider to measure first** (default: the one with the most left) comes first, in channel-number order.
- **Your lines:** the provider counts every connection, including this one. With two or more lines, a probe only runs while one line would still be left free for watching; with a single line, only while nobody is watching. After each probe there's a short pause (longer after a failure), because providers keep counting a connection for a moment after it closes.
- **How:** `ffprobe` with the account's user agent (or Dispatcharr's default), a 20-second limit, reading the first video and audio stream. The result is written exactly as Dispatcharr writes its own (`resolution`, `source_fps`, `video_codec`, `pixel_format`, `audio_codec`, `audio_channels`, `sample_rate`, `stream_type`).
- **Failures:** a stream that can't be measured is left alone for a week; after three failures it counts as a failing stream for Best stream first.

The log says which group it is working through ("Measuring: Entertainment, 212 left"), and **Sports service status** shows tonight's count. **Measure streams now** queues one batch of up to 20 at any time of day: it starts within a minute and takes about 2½ minutes at most, still respecting your lines (the result shows in **Sports service status**).

## Reports from the TV

Anyone watching can report a channel from Viewarr. The plugin reads everybody's reports every 5 minutes and, with **Act on reports from the TVs** on (the default), applies them straight away, no approval step:

| Report | What happens |
|---|---|
| **Wrong guide** with the right entry chosen on the TV | The channel's guide is set to that entry. |
| **Wrong guide** without a choice | A guide entry with the same name (ignoring HD / FHD / SD / "UK -" and the like) in a *different* source is used, if there is exactly one. Otherwise the report goes on the **needs a pick** list that Viewarr shows admins. |
| **Guide times out** by so many minutes | A house-wide offset for that channel, sent to every TV. Nothing changes in Dispatcharr. |
| **Dead stream** | A failure for that stream, which Best stream first uses in the same round. |
| **Wrong logo** | The logo of another of the channel's streams, when one differs. |
| **Wrong name** | The channel is renamed. |
| **Other** | Written to the log only. |

Every change goes to a change log with who reported it and what was there before. Admins undo one from Viewarr, or on the plugin's page with **Recent changes from reports** (shows the last 10 with their ids) and **Undo a change** (paste the id into **Change to undo**, save, press the button). The last 200 changes are kept in `_data/viewarr/reports.json`.

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

## Live timeshift: where the buffer is kept

Live timeshift needs disk space on your Dispatcharr server while someone watches: about **3 GB per hour of buffer for each channel being watched** (an HD channel; less for SD). With the defaults (2 hours, up to 3 channels at once) that's up to about **18 GB**, all deleted as soon as nobody's watching. The plugin won't start a buffer with less than 10 GB free.

You don't type a path: **Live timeshift: buffer folder** offers the two places your Dispatcharr already has, so it can't point anywhere that doesn't exist.

| Choice | Where the files really are |
|---|---|
| **Automatic** (default) | Picks for you: the TV recordings folder when `/data/recordings` is mapped to its own disk or share with more free space, otherwise Dispatcharr's data folder. **TV guide status / Sports service status** shows which it chose and the free space of each. |
| **Dispatcharr's data folder** | Inside whatever you mapped to `/data` in your Dispatcharr container (for example `/mnt/user/appdata/dispatcharr` on Unraid, or the `./data` folder next to your compose file), in `logos/viewarr-ts`. |
| **TV recordings folder** | Dispatcharr's `/data/recordings`, where it saves recordings, in a hidden `.viewarr-timeshift` folder (so Plex, Jellyfin and Emby don't pick it up). By default that's a folder inside `/data`; if you've mapped a separate share or disk to `/data/recordings`, the buffer goes there. |

The folder changes once no channel is being buffered.

### Keeping recordings (and the buffer) on another disk or share

Dispatcharr always records into `/data/recordings` inside its container. To put recordings on another disk or share, map it to that path, then choose **TV recordings folder** if you want the timeshift buffer there too.

- **Unraid:** Docker → Dispatcharr → Edit → **Add another Path**: Container Path `/data/recordings`, Host Path your share (for example `/mnt/user/TV recordings`), Access Mode Read/Write. Apply.
- **Docker compose:** add a volume to the Dispatcharr service and recreate it:

  ```yaml
  services:
    dispatcharr:
      volumes:
        - ./data:/data
        - /path/to/your/recordings:/data/recordings
  ```

Tips:

- **Fast storage is best** (an SSD or cache pool): the buffer is written constantly while someone watches.
- **Unraid "use cache: yes" shares:** the mover can move older buffer pieces to the array while someone is watching late at night, and rewinding that far would then fail. Use Dispatcharr's data folder, or set the share to cache only, if that happens.
- Not on the Docker image: as long as `/data` (and `/data/recordings`, if you use it) are mapped to real folders, nothing is written inside the container itself.

## TV guide (Sky)

An optional guide for your channels made straight from Sky's own listings (the ones its website and apps use), with no separate grabber to run. Off until you switch on **TV guide (Sky)**.

**What it does**

- **Twice a day** (04:30 and 16:30, in the time zone set in Dispatcharr's own settings), straight away when switched on, and a few minutes after you change its settings, it reads Sky's channel list for your region.
- **Matching.** It matches the channels in your lineup (enabled in a channel profile) to Sky's by name: case, accents, "HD" / "FHD", provider prefixes such as "UK:", "&" / "and", Sky's shortened names ("SkySpMainEvHD" is Sky Sports Main Event), known renames (Sky One is Sky Showcase) and region words ("BBC One Scotland", STV, UTV). "+1", UHD and numbers must agree exactly, and two different regions never match. Exact names are taken; close names (fuzzy, with a threshold) are taken too, more readily when your channel number is the same as Sky's. Overrides win over all of it.
- **Listings** for the matched Sky channels only, for **TV guide: days ahead** (up to 8). Politely: two requests at a time with a pause after each, up to three tries with growing waits, and a longer pause if Sky says to slow down. Whatever fails keeps its last good listings; one log line per run gives the counts.
- **The guide file**: `/data/logos/viewarr-epg/guide.xml`, served by Dispatcharr at `/logos/viewarr-epg/guide.xml`, written to a temporary file and renamed, so it's never read half-written. Channel ids are `sky.<Sky service id>` (stable); each programme has its title, episode title, description, category, picture, season and episode (`xmltv_ns`) when Sky gives them, and new, live, HD and subtitles flags.
- **The guide source**: it makes (once) an XMLTV guide source in Dispatcharr called **Viewarr: Sky guide**, pointing at the file through Dispatcharr's own web server inside its container, and refreshes it after every write.
- **The review list**: what matched (and how sure), and the channels that didn't but nearly did, in **TV guide status** and in every user's app data (`viewarr_guide`), so Viewarr can show it. With [channel logos](#channel-logos) on, each matched channel also says where its logo comes from (`"logo": "sky"`, `"tv-logos"` or `"kept"`), and **TV guide status** gives the logo counts.
- **Channel logos** (optional): Sky's logo for each matched channel, if you switch on **Channel logos from Sky**. See [Channel logos](#channel-logos).

**Settings**

| Setting | What it does |
|---|---|
| **TV guide (Sky)** | Off by default. On: fetches and keeps the guide source up to date. Off: nothing is fetched, and channels pointed at this guide go back to the guide they had. The source and file are left in place (delete the source in Dispatcharr if you like). |
| TV guide: listings source | Sky (UK). More sources can plug in later (see below). |
| **TV guide: Sky region** | London, Scotland, Scotland: Border, Wales, Northern Ireland, Channel Islands, and English regions (North West, North East, East, West, West Midlands). The national channels are the same everywhere; the region decides BBC One, ITV / STV / UTV and Sky's channel numbers. |
| TV guide: days ahead | 1 to 8, default 8. |
| **Point matched channels at this guide** | Off by default. On: every matched channel uses **Viewarr: Sky guide** in Dispatcharr. The guide each had before is kept in `_data/viewarr/guide_backup.json` and put back when this, or the TV guide, is switched off (unless someone has chosen another guide for that channel by hand since). Channels with a hand-set guide override in Dispatcharr are left alone. |
| **TV guide: channel overrides** | `Dispatcharr channel name = Sky channel name`, separated by `;` or new lines. Use the Sky name as **TV guide status** shows it, or its id (`sky.4033`). `Channel = -` never matches that channel. |
| **Channel logos from Sky** | Off by default. Fill in missing logos, or use for every channel. See [Channel logos](#channel-logos). |

**Moving over from another Sky grabber**

1. Switch on **TV guide (Sky)** with your region, and leave **Point matched channels at this guide** off. Your channels keep their current guide; the new one sits beside it.
2. Press **TV guide status**: fix anything unmatched or wrongly matched with overrides (for example Sky's pop-up film channels, such as `Sky Cinema Hits = MockingjayHD`).
3. Press **Compare guide with another source** (optionally with the parameter `source`, e.g. `Sky (iptv-org)`): for each matched channel, the share of the next 24 hours' programmes with the same title starting within 5 minutes in both guides.
4. Happy? Switch on **Point matched channels at this guide**. Within a couple of minutes every matched channel uses it.
5. After a few days, the old grabber (for example an iptv-org/epg container) and its guide source can be removed. Switching the setting off again puts every channel back as it was.

## Channel logos

Optional, off until you set **Channel logos from Sky**. It gives your channels logos from two sources, in order:

1. **Sky's own logo**, for every channel the [TV guide](#tv-guide-sky) matched to a Sky channel (only while the TV guide is on). Sky's logos (`https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/500/500/skychb<sid>.png`) are transparent PNGs, mostly white, made for Sky's dark menus, so they suit Viewarr's dark screens. (No separate dark or light version exists at that address; 500 px is plenty for a TV.)
2. **[tv-logos](https://github.com/tv-logo/tv-logos)** for the other channels in your lineup, matched by name inside the country you choose, with the same name matching as the guide (exact names first; a close name only when it's clearly ahead of the next one). Its logos are 512 px PNGs made for dark backgrounds. The plain version is used; a horizontal (`hz`) or `white` one only when there's no plain one; versions for light backgrounds are skipped. The country's folder listing is read once a day (two GitHub requests) and kept; the logos themselves are linked from `raw.githubusercontent.com` and Dispatcharr fetches them itself. tv-logos is free for personal use (see its README); please credit it if you pass logos on.
3. Anything neither has is **left alone**.

**Which channels.** Sky: the channels the guide matched. tv-logos: the channels in your lineup (enabled in a channel profile, not radio), leaving out channels Dispatcharr made automatically from a provider's groups (24/7 and event channels), groups whose names start "24/7" and names that look like event slots ("Event 3", "PPV 12", "20:00", "A vs B").

**Missing** means: no logo; a logo whose address no longer works (the file is gone, an error comes back, or it isn't a picture); or a stand-in ("placeholder", "no logo" in its name or address). Each logo address is checked with one tiny request and the answer kept (a working one for 30 days, a broken one for a day); at most 400 checks a run.

**When.** Once a day (05:15), a couple of minutes after you change these settings, and a few minutes after each TV guide fetch. It works with the TV guide off too (tv-logos only then). One log line per run: `guide: logos — N filled, M replaced, K put back`.

**Safe to try.** Before any channel's logo changes, its earlier logo is written to `_data/viewarr/logo_backup.json`, with the logo set here. Switching **Channel logos from Sky** off (or a source no longer giving that channel a logo, for example turning the TV guide off for Sky's) puts the earlier logo back, unless someone has changed that channel's logo by hand since: then it's left as it is and never touched again. Channels with a logo override in Dispatcharr are never touched. An existing Dispatcharr logo with the same address is reused rather than duplicated. **Preview channel logos** shows what would change without changing anything (parameters `mode` = `missing` / `all` and `sources` = `sky_tvlogos` / `sky` / `tvlogos` to try other settings).

| Setting | What it does |
|---|---|
| **Channel logos from Sky** | **Off** (default): nothing changes, and logos set here go back. **Fill in missing logos**: only channels with no logo or a broken one. **Use for every channel**: every channel a source has a logo for gets it. |
| Channel logos: sources | **Sky, then tv-logos** (default), **Sky only** (needs the TV guide on), or **tv-logos only**. |
| Channel logos: country | The tv-logos country folder: United Kingdom (default), Ireland, United States, Canada, Australia, Germany, France, Spain, Italy, Netherlands. |

### Adding a listings source

Listings sources plug in. Each is a class in `viewarr/guide.py` derived from `ListingsSource`, with:

- `key`, `label`, `epg_name` (its Dispatcharr guide source) and `channel_prefix` (its stable XMLTV channel ids);
- `services(settings, http)`: the region's or lineup's channels as `{"id", "name", "number", "logo", "hd"}`;
- `schedules(settings, ids, days, http, cache)`: the programmes of those channels, keeping the last good copy in `cache` for anything that fails;
- optionally `match_names(service)` for the source's abbreviations and renames.

Add it to `SOURCES` and to the `guide_source` options in `plugin.json`. Matching, the XMLTV file, the Dispatcharr guide source, the channel mapping, the review list and the comparison are shared. A documented stub for **Schedules Direct (US / Canada)** is there already and can't be chosen yet (Dispatcharr also has its own built-in Schedules Direct guide source).

## Install

1. Dispatcharr → **Plugins** → **Import**, and choose `viewarr-plugin-<version>.zip` from the [releases](https://github.com/petebutty/viewarr-plugin/releases).
2. **Enable** it. Dispatcharr asks you to approve what it uses:
   - **persistent service:** it runs every few minutes;
   - **outbound network:** it reads ESPN, TMDB, the Sky schedule (and Sky's listings for the TV guide, and the tv-logos listing for channel logos, if you switch them on) and the UK listings;
   - **user data:** it reads each person's Viewarr setup, stream health and reports, and leaves their answers there.

That's it. Nobody needs to set anything up in Viewarr: each TV finds the plugin by itself. Each person's follows, Live now groups and alert choices reach the plugin through their synced Viewarr setup.

## Settings (all optional)

| Setting | What it does |
|---|---|
| **Live now** | On by default. Works out Live now for the house. Off: Live now is off for the house and Viewarr hides it. |
| **Live now: extra channel groups to search** | The plugin always searches groups whose names sound like sport (Sports, Football, Events, Box Office, EPL…) and Event Channel Managarr's groups. Add others here, such as your provider's event-feed groups. Easier: switch a group on in Viewarr → Settings → Live TV → Live now groups, and the plugin searches it from the next round. |
| **Followed games and where they're on** | On by default. Fixtures of every followed team and league (ESPN) and the channel for each game (the guide, Sky's schedule, UK football TV listings). Off: no fixtures or listings fetched here; the Sports page places games from each TV's own guide only. Sport recordings and score alerts need it on. |
| **Sport recordings** | On by default. "Record all &lt;team&gt;" and Record on games not yet in the guide, made on the server with the TVs off. Off: Viewarr hides Record all &lt;team&gt; and record-when-listed (recording a game already in the guide still works). |
| **Score alerts and live scores** | On by default. The 45-second score checks for alerts and the ticker's Live scores. Off: the stored alerts are emptied and Viewarr hides score alerts and Live scores. |
| **Event channels: only with a listing** | On by default. Sends the TVs the plugin's event channels (`quiet_channels` in `viewarr_house`), so Viewarr can hide those with nothing listed. Off: an empty list is sent, every event channel is shown and Viewarr hides the option. |
| **Just added (JustWatch)** | On by default. The daily JustWatch fetch for the "&lt;service&gt; · Just added" rows. Off: the stored lists are removed and Viewarr hides the rows. |
| **IPTV progress sync (Plex, Jellyfin, Emby)** | On by default. The house-wide switch for IPTV progress sync; each person still switches it on in Viewarr. Off: no syncing, and Viewarr hides the option. |
| **Best stream first** | On by default. Puts each channel's best stream first in Dispatcharr; see [Best stream first](#best-stream-first). |
| **Act on reports from the TVs** | On by default. Applies reports from Viewarr straight away; see [Reports from the TV](#reports-from-the-tv). |
| **Groups never reported** | Groups that are off air most of the time (club TV). Their streams are never marked failing or measured. Comma-separated. |
| **Change to undo (id)** | Only for the **Undo a change** button on Dispatcharr's page. Viewarr sends the id itself. |
| **Measure streams at night** | On by default. Measures streams with ffprobe inside the window; see [Measuring streams](#measuring-streams). |
| **Measuring window (local time)** | `HH:MM-HH:MM`, default `01:00-07:00`. Empty: never on a schedule. |
| **Measure these channel groups first** | Channel groups in priority order; the rest follow alphabetically. |
| **Measure only the listed groups** | Off by default. On: skip every other group. |
| **Provider to measure first** | M3U account name; empty means the one with the most left. |
| **Live timeshift (pause and rewind live TV)** | On by default. Keeps a buffer of each channel being watched; see above. Off: Viewarr hides it. |
| Live timeshift: channels | All channels (default), or only channels without catch-up (those keep using the provider's catch-up). |
| **Live timeshift: buffer length** | How far back a TV can rewind: 30 minutes to 4 hours, default 2 hours. An HD channel takes about 3 GB an hour on `/data` while it's watched. |
| **Live timeshift: channels at once** | Default 3. Beyond it a TV has catch-up (if the channel has it) or live only. |
| Live timeshift: buffer folder | Automatic (default: suggests the best of the two), Dispatcharr's data folder, or the TV recordings folder (`/data/recordings/.viewarr-timeshift`, hidden from Plex/Jellyfin). Changes once nothing is being buffered. |
| **Backups** | On by default. Off: Viewarr hides Backups and the plugin refuses them; saved ones are kept. |
| **TV guide (Sky)** and its options | Off by default. See [TV guide (Sky)](#tv-guide-sky). |
| **Channel logos from Sky**, its sources and country | Off by default. See [Channel logos](#channel-logos). |

Every function can be switched on or off, and its options set, from Viewarr itself (Settings → Plugins → Viewarr, admins). The plugin tells the TVs which functions are on (`functions` in `viewarr_server`); while one is off it's off for the whole house and Viewarr hides it. Only with no plugin at all do the TVs do these jobs themselves, as before.

## Polite to the sources

- **UK listings:** one page, fetched at most every 6 hours. After a "too many requests" or "unavailable" reply it waits an hour. A failed or empty fetch keeps the last good listings.
- **ESPN:** only the leagues and teams someone follows. Live scores are checked only while a followed game is on.
- **TMDB:** about 250 titles a round. Each title is looked up once and remembered.
- **Sky listings (TV guide, when on):** twice a day, one request for the channel list and one per 20 matched channels per day (about 60 for 130 channels and 8 days), two at a time with a pause after each. Failures are retried at most three times with growing waits; whatever still fails keeps its last good listings.
- **Channel logos (when on):** the tv-logos country listing at most once a day (two GitHub API requests); each logo address checked once with a tiny request and remembered (30 days if it worked, a day if not), a quarter of a second apart, at most 400 a run.
- **User-Agent:** every request identifies itself with a link to this repo.

## Requirements

- Dispatcharr 0.34 or newer.
- The latest Viewarr for every feature. Older versions use what they understand.

## Licence

MIT. See [LICENSE](LICENSE).


**Groups never reported.** Club TV channels and event channels are off air most of the time by nature, so being unplayable isn't a fault: the plugin never marks their streams as failing, never measures them, and a dead-stream report about one is logged only. The TVs get the same list and don't report them automatically. Set the groups in *Groups never reported* (event groups are always included).
