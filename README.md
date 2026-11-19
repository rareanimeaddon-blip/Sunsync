# Sunsync

Free subtitle auto-sync for Nuvio providers and Stremio add-ons. **No server, no paid account, no monthly bill.** Everything runs on GitHub.

## How it works

1. **Queue** — `queue.json` lists titles to sync (`tmdbId`, `type`, `season`/`episode`, `lang`).
2. **Action** — `.github/workflows/sync.yml` runs every 2 hours (and on manual dispatch). For each queued title it:
   - Fetches the best English subtitle from **SubDL** (free key) and **Wyzie** (free key) by TMDB/IMDb id.
   - Syncs with **ffsubsync**: subtitle-to-subtitle (SubDL aligned to Wyzie) by default; audio-based when a `streamUrl` is given in the queue entry.
   - Strips the Wyzie free-tier promo first cue.
   - Writes `synced/<tmdbId>-<lang>.srt` and updates `index.json`, then commits.
3. **Providers** — `helper/subsync.js` reads `index.json` from `raw.githubusercontent.com` and swaps the synced subtitle URL into a stream. It never throws and never blocks the stream list (3 s timeout, in-memory cache). If a title isn't synced yet, the provider just uses whatever subtitle it already had.

## Setup (one time)

1. This repo is **public** (GitHub Actions is free without minute limits on public repos). It lives at https://github.com/rareanimeaddon-blip/Sunsync.
2. Add these secrets (Settings → Secrets and variables → Actions):
   - `SUBDL_API_KEY`, `SUBDL_API_KEY2` — from subdl.com (free: 2,000 searches + 50 downloads/day per key)
   - `WYZIE_API_KEY`, `WYZIE_API_KEY2` — from sub.wyzie.io (free: 1,000 requests/day per key)
   - `TMDB_API_KEY` — the shared TMDB key already used by the providers
   - `OPENSUBTITLES_API_KEY` — **optional**. Your own free opensubtitles.com key (20 downloads/day). If not set, the service uses the public shared key from OpenSubtitles' own opensubs-cli (5 downloads/day) — enough for a fallback.
   - `SUBSOURCE_API_KEY` — **optional**. Your own free subsource.net key (create a free account at subsource.net → Profile → API Key). Without it SubSource is skipped; the service works fine on the other four sources.
3. Add titles to `queue.json` (or trigger the workflow manually — it reads the queue each run).

## Subtitle sources (coverage)

The service tries sources in order, so the most titles get a subtitle:

| Source | What it covers | Free limit | Key needed |
|---|---|---|---|
| SubDL | Movies + TV (season packs) | 50 downloads/day/key | Yes (2 keys) |
| Wyzie | Movies + TV episodes | 1,000 requests/day/key | Yes (2 keys) |
| OpenSubtitles.com | Biggest catalog (9M+), movies + TV, the fallback for titles the first two miss | 5/day shared key, 20/day free account | No (public shared key built in) |
| YIFY Subtitles | Movies only, 100+ English subs per big title | Unmetered (scrape, browser UA + Referer on download) | No |
| SubSource | The Subscene archive (2.9M+), movies + TV | Free account | Yes (free account key, optional) |

Each title is tried against the sources in order until it has a candidate + a reference (two subtitles are needed for a sync), so the metered sources are only spent where they matter. TV episodes are handled per-episode: the index key is `tmdbId:s<season>e<episode>:lang` (e.g. `1396:s1e1:en`), SubDL season packs are opened and the right episode file is picked (`01 Pilot.en.srt`), and Wyzie picks the episode-specific release (not the season pack). A wrong-episode subtitle is worse than none, so the pick logic filters by season/episode, not just by download count.

Not used: the unofficial opensubtitles.org scraper API (bipuldey19/Opensubtitles-Unofficial-API) — tested 2026-11-19, it runs but cannot fetch anything: opensubtitles.org now blocks non-browser clients with a bot gate ("Making sure you're not a bot!", 401). The official REST API above is the working OpenSubtitles path. Also not used: TheSubtitleDB and Podnapisi (unreachable from the test network), Addic7ed (search 503, flaky), TVsubtitles.net (TV-only scrape, complex; TV is already covered by Wyzie + SubDL + OpenSubtitles), Shooter/BSPlayer (hash-based — need the video file hash, which a stream URL doesn't give), Jimaku (Japanese only), SubX (needs a key, hosted on a personal dynamic-DNS server), and the language-specific providers (ASSRT, Zimuku, Napisy24, GreekSubs, etc.).

## Test proof (2026-11-19, xpass + Project Hail Mary)

- xpass resolves Project Hail Mary (TMDB 687163) → 4 streams.
- SubDL + Wyzie both return English subs and download fine.
- ffsubsync: a SubDL subtitle shifted +10 s, aligned to the Wyzie subtitle → recovered offset **-10.27 s**, fixed file within **0.3 s** of the original.
- Audio-based sync against the xpass stream is blocked from the test VM (segment hosts serve a tracking pixel / HTML page there, not video) — it must run on a normal network or on the Action runner. The subtitle-to-subtitle path needs no video and always works.

## Files

- `sync.py` — the sync engine (fetch + ffsubsync + index).
- `.github/workflows/sync.yml` — scheduled + manual Action.
- `queue.json` — titles waiting to be synced.
- `index.json` — `{"<tmdbId>:<lang>": {file, offset, drift, method, source, updated}}`.
- `synced/` — synced subtitle files, served via `raw.githubusercontent.com`.
- `helper/subsync.js` — Nuvio provider helper (paste into a provider).

## Limits (honest)

- A title gets its synced subtitle on the **next Action run** (up to 2 hours after being queued), not instantly. For instant on-demand sync, a live service (e.g. a free Hugging Face Space) is the upgrade path — the same `sync.py` runs there.
- ffsubsync gets 88–98% of lines right; 80% within 100 ms. Perceptually perfect for most titles, but literally 100% on every video is not physically possible (different cuts, garbage subs). The service never serves a worse subtitle than the original: if sync fails or confidence is low, the original is stored as-is.
- Free API caps: SubDL 50 downloads/day/key, Wyzie 1,000 requests/day/key. Two keys each are configured; rotate if one hits its cap.
