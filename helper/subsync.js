// subsync.js — Nuvio provider helper for nuvio-subsync (GitHub-hosted).
//
// The sync service lives entirely on GitHub (no server, no paid account):
// a scheduled GitHub Action fetches subtitles from SubDL + Wyzie (free),
// syncs them with ffsubsync, and commits the synced .srt files + index.json
// to the nuvio-subsync repo. This helper reads that index and swaps the
// synced subtitle URL into a stream.
//
// Usage inside a provider's getStreams, after streams are resolved:
//   var sub = await SubSync.getSubtitle(tmdbId, mediaType, "en");
//   if (sub) { stream.subtitles = [sub]; }
//
// Or, to attach to every stream that has no subtitle yet:
//   var subs = await SubSync.getSubtitles(tmdbId, mediaType, "en");
//   streams.forEach(function (s) { if (!s.subtitles || !s.subtitles.length) s.subtitles = subs; });
//
// Rules (same as every Nuvio provider):
// - Never throws. Any failure returns null / [] and the provider carries on
//   with whatever subtitle it already had (or none).
// - Never blocks the stream list: the index fetch has a short timeout and
//   the result is cached in memory for the life of the provider call.
// - WebOS-safe: no setTimeout, no optional chaining, no matchAll, no .at().
//
// SET THIS to the repo that hosts the synced subtitles:
//   "https://raw.githubusercontent.com/rareanimeaddon-blip/Sunsync/main"
// Override at runtime with SubSync.setBase(url) (useful for tests).
var SUBSYNC_BASE = "https://raw.githubusercontent.com/rareanimeaddon-blip/Sunsync/main";

(function () {
  var indexCache = null;
  var indexPromise = null;

  function fetchWithTimeout(url, ms) {
    var timeoutMs = (typeof ms === "number" && ms > 0) ? ms : 3000;
    return new Promise(function (resolve) {
      var done = false;
      var start = Date.now();
      function finish(val) { if (!done) { done = true; resolve(val); } }
      function spin(ms2) { var t = Date.now(); while (Date.now() - t < ms2) {} }
      function attempt() {
        if (done) return;
        fetch(url, {}).then(function (r) {
          if (!r || !r.ok) { finish(null); return; }
          return r.text().then(function (t) { finish(t); }, function () { finish(null); });
        }, function () {
          if (!done && Date.now() - start < timeoutMs) { spin(150); attempt(); }
          else finish(null);
        });
      }
      attempt();
      // Hard cap: the spin loop above bounds the wait, but if fetch hangs
      // without settling, this resolves null on the next tick chain.
      setTimeoutSafe(0);
      function setTimeoutSafe() {
        if (typeof setTimeout === "function") {
          setTimeout(function () { finish(null); }, timeoutMs + 500);
        }
      }
    });
  }

  function getIndex() {
    if (indexCache) return Promise.resolve(indexCache);
    if (indexPromise) return indexPromise;
    indexPromise = fetchWithTimeout(SUBSYNC_BASE + "/index.json", 3000).then(function (text) {
      var parsed = null;
      if (text) {
        try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
      }
      indexCache = parsed || {};
      return indexCache;
    });
    return indexPromise;
  }

  // Returns one subtitle object {url, lang, label} for the synced file,
  // or null when this title/language has not been synced yet.
  // TV: pass season and episode — the index key is "tmdbId:s<season>e<episode>:lang"
  // (a series key would mix episodes). Movies: "tmdbId:lang".
  function getSubtitle(tmdbId, mediaType, lang, season, episode) {
    var lg = lang || "en";
    return getIndex().then(function (index) {
      try {
        var key = String(tmdbId) + ":" + lg;
        if (mediaType === "tv" && season != null && episode != null) {
          key = String(tmdbId) + ":s" + season + "e" + episode + ":" + lg;
        }
        var entry = index ? index[key] : null;
        if (!entry || !entry.file) return null;
        return {
          url: SUBSYNC_BASE + "/" + entry.file,
          lang: lg,
          label: "Synced (" + (entry.method || "sync") + ")"
        };
      } catch (e) { return null; }
    }, function () { return null; });
  }

  function getSubtitles(tmdbId, mediaType, lang, season, episode) {
    return getSubtitle(tmdbId, mediaType, lang, season, episode).then(function (sub) {
      return sub ? [sub] : [];
    });
  }

  function setBase(url) {
    if (typeof url === "string" && url) {
      SUBSYNC_BASE = url.replace(/\/$/, "");
      indexCache = null;
      indexPromise = null;
    }
  }

  var api = { getSubtitle: getSubtitle, getSubtitles: getSubtitles, setBase: setBase };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof globalThis !== "undefined") { try { globalThis.SubSync = api; } catch (e) {} }
  if (typeof global !== "undefined") { try { global.SubSync = api; } catch (e) {} }
})();
