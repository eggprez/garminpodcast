using Toybox.Application;
using Toybox.Lang;
using Toybox.Media;
using Toybox.Time;

//! Persistent state for downloaded episodes and playback positions.
//!
//! Episodes are keyed by the *system-assigned* ContentRef id handed back by
//! the audio download, not by the server's episode id. That is the id the
//! media player reports in onSong() and the only one Media.getCachedContentObj
//! accepts, so it has to be the primary key here. The server id is kept
//! alongside it purely so a re-sync can tell what it already has.
module Store {

    const LIBRARY = "lib";
    const POSITIONS = "pos";

    // Field offsets within a library entry. An array costs noticeably less
    // memory than a dictionary once there are a couple of dozen episodes.
    enum {
        F_TITLE = 0,
        F_SHOW = 1,
        F_DURATION = 2,
        F_ADDED = 3,
        F_SERVER_ID = 4
    }

    function now() {
        return Time.now().value();
    }

    function getLibrary() {
        var lib = Application.Storage.getValue(LIBRARY);
        return lib instanceof Lang.Dictionary ? lib : {};
    }

    function setLibrary(lib) {
        Application.Storage.setValue(LIBRARY, lib);
    }

    function getPositions() {
        var pos = Application.Storage.getValue(POSITIONS);
        return pos instanceof Lang.Dictionary ? pos : {};
    }

    function add(systemId, title, show, duration, serverId) {
        var lib = getLibrary();
        lib[systemId] = [title, show, duration, now(), serverId];
        setLibrary(lib);
    }

    //! Server ids already on the watch, so a sync can skip them.
    function downloadedServerIds() {
        var lib = getLibrary();
        var keys = lib.keys();
        var ids = [];
        for (var i = 0; i < keys.size(); i++) {
            ids.add(lib[keys[i]][F_SERVER_ID]);
        }
        return ids;
    }

    function hasServerId(serverId) {
        var ids = downloadedServerIds();
        for (var i = 0; i < ids.size(); i++) {
            if (ids[i] == serverId) {
                return true;
            }
        }
        return false;
    }

    //! Library keys ordered newest download first.
    function sortedIds() {
        var lib = getLibrary();
        var keys = lib.keys();
        // Insertion sort: the list is at most a few dozen entries, and this
        // avoids allocating the intermediate arrays a merge sort would need.
        for (var i = 1; i < keys.size(); i++) {
            var key = keys[i];
            var added = lib[key][F_ADDED];
            var j = i - 1;
            while (j >= 0 && lib[keys[j]][F_ADDED] < added) {
                keys[j + 1] = keys[j];
                j--;
            }
            keys[j + 1] = key;
        }
        return keys;
    }

    function getPosition(systemId) {
        var pos = getPositions();
        var value = pos[systemId];
        return value instanceof Lang.Number ? value : 0;
    }

    function setPosition(systemId, seconds) {
        var pos = getPositions();
        pos[systemId] = seconds;
        Application.Storage.setValue(POSITIONS, pos);
    }

    function clearPosition(systemId) {
        var pos = getPositions();
        if (pos.hasKey(systemId)) {
            pos.remove(systemId);
            Application.Storage.setValue(POSITIONS, pos);
        }
    }

    //! Forget one episode and delete its audio from the media cache.
    function remove(systemId) {
        var lib = getLibrary();
        if (lib.hasKey(systemId)) {
            lib.remove(systemId);
            setLibrary(lib);
        }
        clearPosition(systemId);
        try {
            Media.deleteCachedItem(new Media.ContentRef(systemId, Media.CONTENT_TYPE_AUDIO));
        } catch (e) {
            // Already gone from the cache; the bookkeeping above is what matters.
        }
    }

    //! Delete anything downloaded more than Config.RETENTION_SECONDS ago.
    //! Returns how many episodes were removed.
    function purgeExpired() {
        var lib = getLibrary();
        var keys = lib.keys();
        var cutoff = now() - Config.RETENTION_SECONDS;
        var removed = 0;
        for (var i = 0; i < keys.size(); i++) {
            if (lib[keys[i]][F_ADDED] < cutoff) {
                remove(keys[i]);
                removed++;
            }
        }
        return removed;
    }

    //! Seconds until this episode is auto-deleted (0 once it is due).
    function secondsUntilExpiry(systemId) {
        var lib = getLibrary();
        if (!lib.hasKey(systemId)) {
            return 0;
        }
        var left = lib[systemId][F_ADDED] + Config.RETENTION_SECONDS - now();
        return left > 0 ? left : 0;
    }
}
