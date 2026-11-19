#!/usr/bin/env python3
"""
nuvio-subsync — fetch subtitles (SubDL + Wyzie, free keys) and sync them
with ffsubsync, then write the synced file + update index.json.

Runs on GitHub Actions (free, public repo) on a schedule and on manual
dispatch. No server, no paid service, no monthly bill.

Secrets (GitHub Actions secrets, never committed):
  SUBDL_API_KEY, SUBDL_API_KEY2   (subdl.com — free: 2,000 searches + 50 downloads/day/key)
  WYZIE_API_KEY, WYZIE_API_KEY2   (sub.wyzie.io — free: 1,000 requests/day/key)
  TMDB_API_KEY                    (shared public TMDB key, also in the providers)
  OPENSUBTITLES_API_KEY           (optional — your own free opensubtitles.com
                                   key for the 20/day free-account quota; the
                                   default is the public shared key from
                                   OpenSubtitles' own opensubs-cli, 5/day)
  SUBSOURCE_API_KEY               (optional — your own free subsource.net
                                   key; without it SubSource is skipped)

Sources, in priority order (each title tries them until it has a candidate
+ a reference — two subtitles are needed for a sync): SubDL, Wyzie,
OpenSubtitles.com (official REST API — the biggest catalog, 9M+), YIFY
Subtitles (movies only, free, no key), SubSource (the Subscene archive,
2.9M+, free account key). The unofficial opensubtitles.org scraper API
(bipuldey19/Opensubtitles-Unofficial-API) was tested 2026-11-19 and does
NOT work from a server: opensubtitles.org now blocks non-browser clients
with a bot gate ("Making sure you're not a bot!", 401). Do not add it.

Queue entry:
  {"tmdbId": 687163, "type": "movie", "season": null, "episode": null,
   "lang": "en", "imdbId": "tt12042730", "streamUrl": null,
   "streamReferer": null, "streamUA": null}

- streamUrl is optional. When present, audio-based sync is tried first
  (ffmpeg extracts an audio sample, ffsubsync aligns to it).
- When absent (the normal case), subtitle-to-subtitle sync is used:
  the SubDL subtitle is aligned to the Wyzie subtitle (or vice versa).
- If only one subtitle source returns anything, it is stored as-is
  (method "as-is") — never worse than having no subtitle.

Output:
  synced/<tmdbId>-<lang>.srt
  index.json: {"<tmdbId>:<lang>": {"file", "offset", "drift",
              "method", "source", "updated"}}
Providers read index.json from raw.githubusercontent.com and swap the
synced URL in. See helper/subsync.js.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(ROOT, "queue.json")
INDEX_PATH = os.path.join(ROOT, "index.json")
SYNCED_DIR = os.path.join(ROOT, "synced")

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
SUBDL_KEYS = [k for k in (os.environ.get("SUBDL_API_KEY", ""),
                          os.environ.get("SUBDL_API_KEY2", "")) if k]
WYZIE_KEYS = [k for k in (os.environ.get("WYZIE_API_KEY", ""),
                          os.environ.get("WYZIE_API_KEY2", "")) if k]
# OpenSubtitles.com official REST API. Free: 5 downloads/day anonymous,
# 20/day with a free account. The default key is the PUBLIC shared key
# shipped in OpenSubtitles' own open-source CLI (opensubs-cli, Config.ts:
# "shared for all users of opensubs-cli ... eliminates the need for users
# to create their own API keys"). Override with your own free key via the
# OPENSUBTITLES_API_KEY secret for the 20/day free-account quota.
OPENSUBTITLES_SHARED_KEY = "m4INUVkhtR9pUdfKKdWy3Js9XQrVlOei"
OPENSUBTITLES_KEY = os.environ.get("OPENSUBTITLES_API_KEY",
                                   OPENSUBTITLES_SHARED_KEY)
OPENSUBTITLES_UA = "nuvio-subsync/1.0"
# SubSource (subsource.net — the Subscene archive, 2.9M+ subtitles).
# Official REST API, needs a FREE account key (create at subsource.net →
# Profile → API Key). Optional: the service works without it.
SUBSOURCE_KEY = os.environ.get("SUBSOURCE_API_KEY", "")
# YIFY Subtitles — no key, movies only. Needs a browser User-Agent and a
# Referer (the detail page) on the download, or Cloudflare 403s it.
YIFY_BASE = "https://yifysubtitles.ch"
YIFY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

UA = {"User-Agent": "nuvio-subsync/1.0"}


def log(msg):
    print("[subsync] " + str(msg), flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url, timeout=30):
    return json.loads(http_get(url, timeout=timeout).decode("utf-8", "replace"))


# ---------------------------------------------------------------- TMDB

def tmdb_imdb_id(tmdb_id, media_type):
    """Resolve an IMDb id via TMDB external_ids (needed for Wyzie search)."""
    if not TMDB_KEY:
        return None
    kind = "movie" if media_type == "movie" else "tv"
    url = ("https://api.themoviedb.org/3/%s/%s/external_ids?api_key=%s"
           % (kind, tmdb_id, TMDB_KEY))
    try:
        data = http_get_json(url)
        return data.get("imdb_id")
    except Exception as e:
        log("tmdb external_ids failed for %s: %s" % (tmdb_id, e))
        return None


# ---------------------------------------------------------------- SubDL

def subdl_search(tmdb_id, media_type, season, episode, lang):
    """Search SubDL by TMDB id. Returns list of subtitle dicts.
    NOTE: the `type` param is REQUIRED for TV (type=tv) — without it a
    TV search returns nothing. Movies use type=movie."""
    params = ("tmdb_id=%s&languages=%s&type=%s"
              % (tmdb_id, lang, "tv" if media_type == "tv" else "movie"))
    if media_type == "tv" and season is not None:
        params += "&season=%s" % season
        if episode is not None:
            params += "&episode=%s" % episode
    for key in SUBDL_KEYS:
        try:
            url = "https://api.subdl.com/api/v1/subtitles?api_key=%s&%s" % (key, params)
            data = http_get_json(url)
            subs = data.get("subtitles", []) or []
            if subs:
                log("subdl: %d subs for tmdb %s" % (len(subs), tmdb_id))
                return subs
            log("subdl: no subs for tmdb %s (key ...%s)" % (tmdb_id, key[-4:]))
        except Exception as e:
            log("subdl search failed: %s" % e)
    return []


def subdl_pick(subs, season=None, episode=None):
    """Pick the best SubDL subtitle.
    Movies: prefer non-hearing-impaired, then a WEB-DL/BluRay release name.
    TV: SubDL returns SEASON PACKS (one zip with an .srt per episode).
    Prefer the pack for the requested season (the `season` field is the
    reliable signal — release names lie), then non-HI."""
    if not subs:
        return None
    pool = subs
    if season is not None:
        same_season = [s for s in pool if s.get("season") == season]
        if same_season:
            pool = same_season
        if episode is not None:
            ep_match = [s for s in pool
                        if s.get("episode") == episode]
            if ep_match:
                pool = ep_match
    non_hi = [s for s in pool if not s.get("hi")]
    pool = non_hi if non_hi else pool
    for s in pool:
        name = (s.get("release_name") or "").upper()
        if "WEB-DL" in name or "BLURAY" in name or "BLU-RAY" in name:
            return s
    return pool[0]


def subdl_download(sub, season=None, episode=None):
    """Download a SubDL subtitle zip, return the .srt text (or None).
    TV season packs contain one .srt per episode (named "01 Pilot.en.srt"
    etc.) — pick the file for the requested episode, not the largest."""
    url = sub.get("url") or ""
    if not url:
        return None
    if url.startswith("/"):
        url = "https://dl.subdl.com" + url
    # The API returns the url with an api_key query already; keep it.
    # If it has no key, add the first key.
    if "api_key=" not in url and SUBDL_KEYS:
        sep = "&" if "?" in url else "?"
        url = url + sep + "api_key=" + SUBDL_KEYS[0]
    try:
        raw = http_get(url, timeout=60)
    except Exception as e:
        log("subdl download failed: %s" % e)
        return None
    tmp = tempfile.mkdtemp(prefix="subdl-")
    try:
        zpath = os.path.join(tmp, "sub.zip")
        with open(zpath, "wb") as f:
            f.write(raw)
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".srt", ".vtt", ".ass", ".ssa"))]
            if not names:
                log("subdl zip has no subtitle file: %s" % z.namelist())
                return None
            # TV season pack: pick the file for the requested episode.
            if season is not None and episode is not None and len(names) > 1:
                ep_file = pick_episode_file(names, season, episode)
                if ep_file:
                    log("subdl pack: picked episode file %s" % ep_file)
                    data = z.read(ep_file)
                    return data.decode("utf-8", "replace")
            # Prefer .srt, then the largest file.
            names.sort(key=lambda n: (not n.lower().endswith(".srt"),
                                      -z.getinfo(n).file_size))
            data = z.read(names[0])
            return data.decode("utf-8", "replace")
    except Exception as e:
        log("subdl zip read failed: %s" % e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- Wyzie

def wyzie_search(imdb_id, lang, season=None, episode=None):
    """Search Wyzie by IMDb id. Returns list of subtitle dicts.
    NOTE: season/episode MUST be passed for TV — without them the search
    returns subtitles for the whole series and the pick can be a different
    episode entirely."""
    for key in WYZIE_KEYS:
        try:
            url = ("https://sub.wyzie.io/search?id=%s&language=%s&key=%s"
                   % (imdb_id, lang, key))
            if season is not None:
                url += "&season=%s" % season
                if episode is not None:
                    url += "&episode=%s" % episode
            data = http_get_json(url)
            subs = data if isinstance(data, list) else data.get("results", [])
            if subs:
                log("wyzie: %d subs for %s" % (len(subs), imdb_id))
                return subs
            log("wyzie: no subs for %s (key ...%s)" % (imdb_id, key[-4:]))
        except Exception as e:
            log("wyzie search failed: %s" % e)
    return []


def wyzie_pick(subs, season=None, episode=None):
    """Prefer highest quality score, non-hearing-impaired.
    TV: prefer a subtitle for THIS episode (release name carries S01E01 /
    1x01 / 101 / "Episode 01") over a whole-season pack (S01 / "season 1"),
    which is a different file. Among episode matches, highest downloads."""
    if not subs:
        return None
    pool = [s for s in subs if not s.get("isHearingImpaired")] or subs
    if season is not None and episode is not None:
        ep = [s for s in pool
              if episode_tag_match(s.get("release") or "", season, episode)]
        if ep:
            pool = ep

    def score(s):
        q = s.get("quality") or {}
        return q.get("score", 0), s.get("downloadCount", 0)
    return max(pool, key=score)


def episode_tag_match(name, season, episode):
    """True when a release/file name looks like it's for this episode."""
    n = name or ""
    if re.search(r"[Ss]%02d[Ee]%02d" % (season, episode), n):
        return True
    if re.search(r"\b%dx%02d\b" % (season, episode), n):
        return True
    if re.search(r"\b%d%02d\b" % (season, episode), n):
        return True
    if re.search(r"[Ee]pisode\s*0?%d\b" % episode, n):
        return True
    return False


def pick_episode_file(names, season, episode):
    """From a season-pack file list, find the file for one episode.
    Files are named like "01 Pilot.en.srt" (2-digit episode prefix) or
    "S01E01 ...". Returns the name or None."""
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if episode_tag_match(base, season, episode):
            return n
        if re.match(r"^0?%d[\s.\-_]" % episode, base):
            return n
    return None


def wyzie_download(sub):
    """Download a Wyzie subtitle via its direct link. Returns .srt text."""
    url = sub.get("url") or ""
    if not url:
        return None
    try:
        raw = http_get(url, timeout=60)
        return raw.decode("utf-8", "replace")
    except Exception as e:
        log("wyzie download failed: %s" % e)
        return None


# ---------------------------------------------------------------- OpenSubtitles.com

def opensubtitles_search(tmdb_id, media_type, season, episode, lang):
    """Search OpenSubtitles.com (official REST API) by TMDB id.
    Biggest catalog (9M+ subtitles) — the fallback that covers the titles
    SubDL/Wyzie miss. Returns list of {file_id, release, hearing_impaired,
    download_count}."""
    params = "tmdb_id=%s&languages=%s" % (tmdb_id, lang)
    if media_type == "tv" and season is not None:
        params += "&season=%s" % season
        if episode is not None:
            params += "&episode=%s" % episode
    url = "https://api.opensubtitles.com/api/v1/subtitles?%s" % params
    req = urllib.request.Request(url, headers={
        "Api-Key": OPENSUBTITLES_KEY,
        "User-Agent": OPENSUBTITLES_UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("opensubtitles search failed: %s" % e)
        return []
    out = []
    for item in data.get("data", []) or []:
        a = item.get("attributes", {}) or {}
        files = a.get("files", []) or []
        if not files:
            continue
        out.append({
            "file_id": files[0].get("file_id"),
            "release": a.get("release") or "",
            "hearing_impaired": bool(a.get("hearing_impaired")),
            "download_count": a.get("download_count") or 0,
        })
    if out:
        log("opensubtitles: %d subs for tmdb %s (total %s)"
            % (len(out), tmdb_id, data.get("total_count")))
    else:
        log("opensubtitles: no subs for tmdb %s" % tmdb_id)
    return out


def opensubtitles_pick(subs):
    """Prefer non-hearing-impaired, then most-downloaded."""
    if not subs:
        return None
    pool = [s for s in subs if not s.get("hearing_impaired")] or subs
    return max(pool, key=lambda s: s.get("download_count", 0))


def opensubtitles_download(sub):
    """Download an OpenSubtitles file. POST /download returns a temporary
    link; GET it for the .srt. Returns .srt text or None.
    NOTE: this is the metered half — 5/day anonymous on the shared key,
    20/day with a free account key. Only called when SubDL/Wyzie have
    nothing, so the quota lasts. Retries once on a transient 503/429."""
    file_id = sub.get("file_id")
    if not file_id:
        return None
    body = json.dumps({"file_id": file_id}).encode("utf-8")
    data = None
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://api.opensubtitles.com/api/v1/download", data=body,
            headers={"Api-Key": OPENSUBTITLES_KEY,
                     "User-Agent": OPENSUBTITLES_UA,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            log("opensubtitles download request failed (attempt %s): HTTP %s"
                % (attempt, e.code))
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                return None
        except Exception as e:
            log("opensubtitles download request failed: %s" % e)
            return None
    if not data:
        return None
    link = data.get("link")
    log("opensubtitles downloads remaining: %s" % data.get("remaining"))
    if not link:
        return None
    try:
        raw = http_get(link, timeout=60)
        return raw.decode("utf-8", "replace")
    except Exception as e:
        log("opensubtitles file fetch failed: %s" % e)
        return None


# ---------------------------------------------------------------- YIFY Subtitles

def yify_search(imdb_id, lang):
    """Search YIFY Subtitles (yifysubtitles.ch) by IMDb id. Movies only,
    no key. The movie page lists subtitle rows with language + detail link.
    Returns list of {id, slug, title, language}."""
    if lang != "en":
        return []
    url = YIFY_BASE + "/movie-imdb/" + imdb_id
    req = urllib.request.Request(url, headers={"User-Agent": YIFY_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        log("yify search failed: %s" % e)
        return []
    out = []
    for m in re.finditer(r'<tr data-id="(\d+)">(.*?)</tr>', html, re.S):
        row = m.group(2)
        lm = re.search(r'class="sub-lang">([^<]+)<', row)
        language = (lm.group(1).strip() if lm else "")
        hm = re.search(r'href="(/subtitles/[^"]+)"', row)
        if not hm:
            continue
        tm = re.search(r'<a href="/subtitles/[^"]+">(.*?)</a>', row, re.S)
        title = re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else ""
        out.append({"id": m.group(1), "slug": hm.group(1),
                    "title": title, "language": language})
    en = [s for s in out if s["language"].lower().startswith("english")]
    if en:
        log("yify: %d english subs for %s" % (len(en), imdb_id))
    else:
        log("yify: no english subs for %s" % imdb_id)
    return en


def yify_pick(subs):
    """Prefer a release-named subtitle (BluRay/WEB-DL/YIFY), else first."""
    if not subs:
        return None
    for s in subs:
        t = (s.get("title") or "").upper()
        if "BLURAY" in t or "BLU-RAY" in t or "WEB-DL" in t or "YIFY" in t:
            return s
    return subs[0]


def yify_download(sub):
    """Download a YIFY subtitle ZIP and extract the .srt.
    The download 403s without a Referer (the detail page) — Cloudflare.
    Returns .srt text or None."""
    slug = sub.get("slug") or ""
    if not slug:
        return None
    detail_url = YIFY_BASE + slug
    dl_url = YIFY_BASE + slug.replace("/subtitles/", "/subtitle/") + ".zip"
    req = urllib.request.Request(dl_url, headers={
        "User-Agent": YIFY_UA, "Referer": detail_url})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except Exception as e:
        log("yify download failed: %s" % e)
        return None
    tmp = tempfile.mkdtemp(prefix="yify-")
    try:
        zpath = os.path.join(tmp, "sub.zip")
        with open(zpath, "wb") as f:
            f.write(raw)
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".srt", ".vtt"))]
            if not names:
                log("yify zip has no subtitle file")
                return None
            names.sort(key=lambda n: (not n.lower().endswith(".srt"),
                                      -z.getinfo(n).file_size))
            return z.read(names[0]).decode("utf-8", "replace")
    except Exception as e:
        log("yify zip read failed: %s" % e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- SubSource

# SubSource wants the full language name ("english"), not the ISO code.
LANG_NAMES = {"en": "english", "es": "spanish", "fr": "french",
              "de": "german", "it": "italian", "pt": "portuguese",
              "nl": "dutch", "hi": "hindi", "ja": "japanese",
              "ko": "korean", "zh": "chinese", "ru": "russian",
              "ar": "arabic", "tr": "turkish", "pl": "polish"}


def subsource_search(imdb_id, tmdb_id, media_type, season, episode, lang):
    """Search SubSource (subsource.net, the Subscene archive) by IMDb id.
    Official REST API v1, needs a FREE account key (SUBSOURCE_API_KEY).
    Returns list of {subtitleId, language, releaseInfo}."""
    if not SUBSOURCE_KEY:
        return []
    headers = {"X-API-Key": SUBSOURCE_KEY,
               "User-Agent": "nuvio-subsync/1.0"}
    # 1. Find the title (prefer IMDb search, fall back to the first match).
    url = ("https://api.subsource.net/api/v1/movies/search"
           "?searchType=imdb&imdb=%s" % imdb_id)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("subsource search failed: %s" % e)
        return []
    movies = data.get("data") if isinstance(data, dict) else data
    if not isinstance(movies, list) or not movies:
        log("subsource: no title for %s" % imdb_id)
        return []
    want_type = "series" if media_type == "tv" else "movie"
    movie = None
    for m in movies:
        if str(m.get("tmdbId") or "") == str(tmdb_id):
            movie = m
            break
    if not movie:
        for m in movies:
            if str(m.get("type") or "").lower() in (
                    want_type, "tv", "tvseries", "show", "film", "movies"):
                movie = m
                break
    if not movie:
        movie = movies[0]
    movie_id = (movie.get("id") or movie.get("movieId")
                or movie.get("movie_id"))
    if not movie_id:
        log("subsource: title found but no movie id")
        return []
    # 2. List subtitles for that title. The language param takes the full
    #    name ("english"), not the ISO code ("en").
    ss_lang = LANG_NAMES.get(lang, lang)
    params = "movieId=%s&language=%s&limit=100" % (movie_id, ss_lang)
    if media_type == "tv" and season is not None:
        params += "&season=%s" % season
        if episode is not None:
            params += "&episode=%s" % episode
    url = "https://api.subsource.net/api/v1/subtitles?%s" % params
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("subsource subtitle list failed: %s" % e)
        return []
    subs = data.get("data") if isinstance(data, dict) else data
    if not isinstance(subs, list):
        subs = []
    if subs:
        log("subsource: %d subs for %s" % (len(subs), imdb_id))
    else:
        log("subsource: no subs for %s" % imdb_id)
    return subs


def subsource_pick(subs, season=None, episode=None):
    """Prefer an episode-specific release for TV, else first."""
    if not subs:
        return None
    pool = subs
    if season is not None and episode is not None:
        ep = [s for s in pool if episode_tag_match(
            s.get("releaseInfo") or "", season, episode)]
        if ep:
            pool = ep
    return pool[0]


def subsource_download(sub, season=None, episode=None):
    """Download a SubSource subtitle ZIP and extract the .srt.
    Season packs hold one .srt per episode — pick the right one."""
    sub_id = sub.get("subtitleId") or sub.get("id")
    if not sub_id:
        return None
    url = ("https://api.subsource.net/api/v1/subtitles/%s/download" % sub_id)
    req = urllib.request.Request(url, headers={
        "X-API-Key": SUBSOURCE_KEY, "User-Agent": "nuvio-subsync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except Exception as e:
        log("subsource download failed: %s" % e)
        return None
    tmp = tempfile.mkdtemp(prefix="subsource-")
    try:
        zpath = os.path.join(tmp, "sub.zip")
        with open(zpath, "wb") as f:
            f.write(raw)
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".srt", ".vtt"))
                     and not n.startswith("__MACOSX")]
            if not names:
                log("subsource zip has no subtitle file")
                return None
            if season is not None and episode is not None and len(names) > 1:
                ep_file = pick_episode_file(names, season, episode)
                if ep_file:
                    return z.read(ep_file).decode("utf-8", "replace")
            names.sort(key=lambda n: (not n.lower().endswith(".srt"),
                                      -z.getinfo(n).file_size))
            return z.read(names[0]).decode("utf-8", "replace")
    except Exception as e:
        log("subsource zip read failed: %s" % e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- SRT utils

def strip_wyzie_promo(text):
    """Free Wyzie downloads inject a promo first cue (00:00:00-00:00:06).
    Drop the first cue if it mentions the free plan / wyzie.io."""
    if not text:
        return text
    lines = text.splitlines()
    # Find the first cue block (number, timing, text lines).
    # Simplest robust check: if the first ~400 chars mention the promo,
    # drop everything up to the first blank line after the timing line.
    head = "\n".join(lines[:8]).lower()
    if "free plan" in head or "wyzie.io" in head:
        # Skip: cue number, timing line, promo text lines, blank line.
        i = 0
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        # cue number
        if i < len(lines):
            i += 1
        # timing line
        if i < len(lines) and "-->" in lines[i]:
            i += 1
        # promo text lines until blank
        while i < len(lines) and lines[i].strip() != "":
            i += 1
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        log("stripped wyzie promo first cue")
        return "\n".join(lines[i:])
    return text


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------------------------------------------------------------- ffsubsync

def find_ffs():
    for name in ("ffs", "ffsubsync", "subsync"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run_ffsubsync(reference, input_path, output_path):
    """Run ffsubsync. reference = subtitle file or audio file.
    Returns (offset_seconds, framerate_scale) or (None, None)."""
    ffs = find_ffs()
    if not ffs:
        log("ffsubsync binary not found on PATH")
        return None, None
    cmd = [ffs, reference, "-i", input_path, "-o", output_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("ffsubsync timed out")
        return None, None
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    m_off = re.search(r"offset seconds:\s*(-?[\d.]+)", out)
    m_fps = re.search(r"framerate scale factor:\s*([\d.]+)", out)
    offset = float(m_off.group(1)) if m_off else None
    fps = float(m_fps.group(1)) if m_fps else None
    if proc.returncode != 0 or not os.path.exists(output_path):
        log("ffsubsync failed (rc=%s): %s" % (proc.returncode, out[-500:]))
        return None, None
    log("ffsubsync: offset=%s fps_scale=%s" % (offset, fps))
    return offset, fps


def extract_audio_sample(stream_url, referer, ua, seconds=600):
    """Download the first N seconds of a stream's audio as 16kHz mono wav.
    Returns the wav path or None. Needs ffmpeg on PATH."""
    if not shutil.which("ffmpeg"):
        log("ffmpeg not found, skipping audio sync")
        return None
    tmp = tempfile.mkdtemp(prefix="aud-")
    wav = os.path.join(tmp, "sample.wav")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-allowed_segment_extensions", "ALL"]
    if referer:
        cmd += ["-referer", referer]
    if ua:
        cmd += ["-user_agent", ua]
    cmd += ["-i", stream_url, "-t", str(seconds),
            "-map", "0:a", "-ac", "1", "-ar", "16000", wav]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log("ffmpeg audio extract timed out")
        return None
    if proc.returncode != 0 or not os.path.exists(wav):
        log("ffmpeg audio extract failed: %s" % (proc.stderr or "")[-400:])
        return None
    log("audio sample: %s bytes" % os.path.getsize(wav))
    return wav


# ---------------------------------------------------------------- one title

def sync_title(entry):
    tmdb_id = entry["tmdbId"]
    media_type = entry.get("type", "movie")
    season = entry.get("season")
    episode = entry.get("episode")
    lang = entry.get("lang", "en")
    imdb_id = entry.get("imdbId") or tmdb_imdb_id(tmdb_id, media_type)
    # TV episodes get their own key/file — a series key would mix episodes.
    if media_type == "tv" and season is not None and episode is not None:
        ep_tag = "s%se%s" % (season, episode)
        key = "%s:%s:%s" % (tmdb_id, ep_tag, lang)
        fname = "%s-%s-%s.srt" % (tmdb_id, ep_tag, lang)
    else:
        key = "%s:%s" % (tmdb_id, lang)
        fname = "%s-%s.srt" % (tmdb_id, lang)

    log("=== %s (%s) tmdb=%s lang=%s ===" % (
        "movie" if media_type == "movie" else "S%sE%s" % (season, episode),
        imdb_id, tmdb_id, lang))

    # 1. Fetch subtitles from the free sources, in priority order.
    #    SubDL + Wyzie first (generous free quotas). OpenSubtitles only
    #    when we don't already have two sources — its download quota is
    #    5/day on the shared key, so spend it where it matters: titles
    #    SubDL/Wyzie miss, or as the reference that makes a sync possible.
    texts = []
    subdl_sub = subdl_pick(subdl_search(tmdb_id, media_type, season, episode, lang),
                           season, episode)
    if subdl_sub:
        subdl_text = subdl_download(subdl_sub, season, episode)
        if subdl_text:
            log("subdl subtitle: %d chars, release=%s"
                % (len(subdl_text), (subdl_sub.get("release_name") or "")[:60]))
            texts.append(("subdl", subdl_text))

    if imdb_id:
        wyzie_sub = wyzie_pick(wyzie_search(imdb_id, lang, season, episode),
                               season, episode)
        if wyzie_sub:
            wyzie_text = wyzie_download(wyzie_sub)
            if wyzie_text:
                wyzie_text = strip_wyzie_promo(wyzie_text)
                log("wyzie subtitle: %d chars, release=%s"
                    % (len(wyzie_text), (wyzie_sub.get("release") or "")[:60]))
                texts.append(("wyzie", wyzie_text))

    if len(texts) < 2:
        os_sub = opensubtitles_pick(
            opensubtitles_search(tmdb_id, media_type, season, episode, lang))
        if os_sub:
            os_text = opensubtitles_download(os_sub)
            if os_text:
                log("opensubtitles subtitle: %d chars, release=%s"
                    % (len(os_text), (os_sub.get("release") or "")[:60]))
                texts.append(("opensubtitles", os_text))

    # Fourth source: YIFY Subtitles (movies only, free, no key).
    if len(texts) < 2 and media_type == "movie" and imdb_id:
        yify_sub = yify_pick(yify_search(imdb_id, lang))
        if yify_sub:
            yify_text = yify_download(yify_sub)
            if yify_text:
                log("yify subtitle: %d chars, release=%s"
                    % (len(yify_text), (yify_sub.get("title") or "")[:60]))
                texts.append(("yify", yify_text))

    # Fifth source: SubSource (Subscene archive, free account key).
    if len(texts) < 2 and imdb_id and SUBSOURCE_KEY:
        ss_sub = subsource_pick(
            subsource_search(imdb_id, tmdb_id, media_type,
                             season, episode, lang), season, episode)
        if ss_sub:
            ss_text = subsource_download(ss_sub, season, episode)
            if ss_text:
                log("subsource subtitle: %d chars, release=%s"
                    % (len(ss_text), (ss_sub.get("releaseInfo") or "")[:60]))
                texts.append(("subsource", ss_text))

    if not texts:
        log("no subtitle found from any source — skipping")
        return None

    # Candidate = first source (usually SubDL, release-matched);
    # reference = second source when there is one (enables the sync).
    candidate_src, candidate_text = texts[0]
    reference_src, reference_text = texts[1] if len(texts) > 1 else (None, None)

    tmp = tempfile.mkdtemp(prefix="sync-")
    try:
        cand_path = os.path.join(tmp, "candidate.srt")
        write_text(cand_path, candidate_text)
        out_path = os.path.join(SYNCED_DIR, fname)
        os.makedirs(SYNCED_DIR, exist_ok=True)

        offset, fps, method = None, None, "as-is"

        # 2. Audio sync first when a stream URL is given.
        stream_url = entry.get("streamUrl")
        if stream_url:
            wav = extract_audio_sample(stream_url,
                                       entry.get("streamReferer"),
                                       entry.get("streamUA") or
                                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/124.0.0.0 Safari/537.36")
            if wav:
                offset, fps = run_ffsubsync(wav, cand_path, out_path)
                if offset is not None:
                    method = "audio"

        # 3. Subtitle-to-subtitle sync when audio sync did not run/succeed.
        if method == "as-is" and reference_text:
            ref_path = os.path.join(tmp, "reference.srt")
            write_text(ref_path, reference_text)
            offset, fps = run_ffsubsync(ref_path, cand_path, out_path)
            if offset is not None:
                method = "sub-to-sub"

        # 4. No sync possible — store the candidate as-is.
        if method == "as-is":
            shutil.copyfile(cand_path, out_path)
            log("stored as-is (no reference available)")

        result = {
            "file": "synced/" + fname,
            "offset": offset,
            "drift": fps,
            "method": method,
            "source": candidate_src + ("+" + reference_src if reference_src else ""),
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        log("done %s: %s" % (key, result))
        return key, result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main

def main():
    if not SUBDL_KEYS and not WYZIE_KEYS:
        log("WARNING: no SubDL/Wyzie keys in env — searches will fail")
    try:
        with open(QUEUE_PATH) as f:
            queue = json.load(f)
    except FileNotFoundError:
        queue = []
    try:
        with open(INDEX_PATH) as f:
            index = json.load(f)
    except FileNotFoundError:
        index = {}

    remaining = []
    changed = False
    for entry in queue:
        try:
            res = sync_title(entry)
        except Exception as e:
            log("ERROR syncing %s: %s" % (entry, e))
            res = None
        if res:
            key, result = res
            index[key] = result
            changed = True
        else:
            remaining.append(entry)

    if changed:
        with open(INDEX_PATH, "w") as f:
            json.dump(index, f, indent=2, sort_keys=True)
            f.write("\n")
    with open(QUEUE_PATH, "w") as f:
        json.dump(remaining, f, indent=2)
        f.write("\n")
    log("queue remaining: %d, index entries: %d" % (len(remaining), len(index)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
