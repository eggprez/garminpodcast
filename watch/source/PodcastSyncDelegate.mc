using Toybox.Communications;
using Toybox.Lang;
using Toybox.Media;
using Toybox.PersistedContent;
using Toybox.WatchUi;

//! Pulls the episode list from the server, then downloads whatever is missing.
//!
//! Downloads run strictly one at a time. The media cache and the radio both
//! behave better that way, and it means a single `mCurrent` field is enough to
//! carry context into the download callback.
class PodcastSyncDelegate extends Communications.SyncDelegate {

    hidden var mQueue;      // episode dictionaries still to fetch
    hidden var mCurrent;    // the one in flight
    hidden var mTotal;
    hidden var mDone;
    hidden var mSucceeded;
    hidden var mLastError;
    hidden var mCancelled;

    function initialize() {
        SyncDelegate.initialize();
        mQueue = [];
        mCurrent = null;
        mTotal = 0;
        mDone = 0;
        mSucceeded = 0;
        mLastError = 0;
        mCancelled = false;
    }

    //! Offer to sync whenever the server is configured. The watch surfaces this
    //! as the "sync needed" hint in the music menu.
    function isSyncNeeded() {
        return Config.isConfigured();
    }

    function onStartSync() {
        if (!Config.isConfigured()) {
            finish(WatchUi.loadResource(Rez.Strings.SyncErrNoConfig));
            return;
        }

        // Free space before pulling anything new down.
        Store.purgeExpired();

        Communications.notifySyncProgress(0);

        var url = Config.serverUrl() + "/api/v1/episodes";
        Communications.makeWebRequest(
            url,
            { "limit" => Config.episodeLimit() },
            {
                :method => Communications.HTTP_REQUEST_METHOD_GET,
                :headers => Config.authHeaders(),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
            },
            method(:onEpisodeList)
        );
    }

    //! The user backed out of the sync. Any in-flight callback sees mCancelled
    //! and returns without touching the queue or reporting again.
    function onStopSync() {
        mCancelled = true;
        mQueue = [];
        Communications.notifySyncComplete(null);
    }

    function onEpisodeList(responseCode as Lang.Number, data as Lang.Dictionary or Lang.String or PersistedContent.Iterator or Null) as Void {
        if (mCancelled) {
            return;
        }
        if (responseCode == 401 || responseCode == 403) {
            finish(WatchUi.loadResource(Rez.Strings.SyncErrAuth));
            return;
        }
        if (responseCode != 200 || !(data instanceof Lang.Dictionary)) {
            finish(WatchUi.loadResource(Rez.Strings.SyncErrNetwork)
                + " (" + responseCode.toString() + ")");
            return;
        }

        var episodes = data["eps"];
        if (!(episodes instanceof Lang.Array) || episodes.size() == 0) {
            finish(WatchUi.loadResource(Rez.Strings.SyncErrNoEpisodes));
            return;
        }

        for (var i = 0; i < episodes.size(); i++) {
            var episode = episodes[i];
            if (episode instanceof Lang.Dictionary && !Store.hasServerId(episode["i"])) {
                mQueue.add(episode);
            }
        }

        mTotal = mQueue.size();
        mDone = 0;
        if (mTotal == 0) {
            finish(null);  // already up to date
            return;
        }
        downloadNext();
    }

    hidden function downloadNext() {
        if (mCancelled) {
            return;
        }
        if (mQueue.size() == 0) {
            // Every download failing points at something systemic (storage
            // full, server trouble) and is worth surfacing to the user.
            if (mTotal > 0 && mSucceeded == 0) {
                finish(WatchUi.loadResource(Rez.Strings.SyncErrNetwork)
                    + " (" + mLastError.toString() + ")");
            } else {
                finish(null);
            }
            return;
        }

        mCurrent = mQueue[0];
        mQueue = mQueue.slice(1, null);

        var url = Config.serverUrl() + "/api/v1/media/" + mCurrent["i"].toString();
        Communications.makeWebRequest(
            url,
            null,
            {
                :method => Communications.HTTP_REQUEST_METHOD_GET,
                :headers => Config.authHeaders(),
                // The server normalises everything to MP3 precisely so this
                // declaration always matches the response's Content-Type.
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_AUDIO,
                :mediaEncoding => Media.ENCODING_MP3
            },
            method(:onAudioDownloaded)
        );
    }

    //! `data` is a Media.ContentRef the system created for the cached audio.
    //! Its id is assigned by the system, so this is the only moment we can
    //! learn it - hence storing the mapping to our server id right here.
    function onAudioDownloaded(responseCode as Lang.Number, data as Lang.Dictionary or Lang.String or PersistedContent.Iterator or Null) as Void {
        if (mCancelled) {
            return;
        }

        if (responseCode == 200 && data != null) {
            var episode = mCurrent;
            var title = episode["t"];
            var show = episode["n"];
            // An audio response is a Media.ContentRef, which is absent from the
            // SDK's declared callback type, so widen through Object to narrow.
            var ref = (data as Lang.Object) as Media.ContentRef;
            Store.add(
                ref.getId(),
                title instanceof Lang.String ? title : "Episode",
                show instanceof Lang.String ? show : "",
                episode["d"] instanceof Lang.Number ? episode["d"] : 0,
                episode["i"]
            );
            mSucceeded++;
        } else {
            // One bad episode should not abandon the rest of the queue; the
            // run only reports failure if nothing at all got through.
            mLastError = responseCode;
        }

        mDone++;
        Communications.notifySyncProgress((mDone * 100) / mTotal);
        mCurrent = null;
        downloadNext();
    }

    hidden function finish(errorMessage) {
        mCurrent = null;
        mQueue = [];
        Communications.notifySyncComplete(errorMessage);
    }
}
