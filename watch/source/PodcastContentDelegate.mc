using Toybox.Lang;
using Toybox.Media;

//! Bridges the media player and our stored playback positions.
class PodcastContentDelegate extends Media.ContentDelegate {

    //! Positions are only flushed once playback has moved this far since the
    //! last write. PLAYBACK_NOTIFY fires often, and persisting on every tick
    //! would mean thousands of flash writes per episode.
    hidden const WRITE_THRESHOLD = 10;

    hidden var mIterator;
    hidden var mLastWritten;

    function initialize(args) {
        ContentDelegate.initialize();

        var startId = null;
        var startPos = 0;
        if (args instanceof Lang.Dictionary) {
            startId = args["id"];
            startPos = args["pos"];
        }

        mLastWritten = {};
        mIterator = new PodcastIterator(startId, startPos);
    }

    function getContentIterator() {
        return mIterator;
    }

    //! playbackPosition is elapsed seconds, or a PLAYBACK_POSITION_* constant
    //! when the player cannot report a real offset - hence the type check.
    function onSong(contentRefId, songEvent, playbackPosition) {
        if (songEvent == Media.SONG_EVENT_COMPLETE) {
            Store.clearPosition(contentRefId);
            mLastWritten.remove(contentRefId);
            return;
        }

        if (!(playbackPosition instanceof Lang.Number) || playbackPosition < 0) {
            return;
        }

        // Pausing, stopping or skipping away is the user's last known point,
        // so record it regardless of how little has elapsed.
        var isCheckpoint = songEvent == Media.SONG_EVENT_PAUSE
            || songEvent == Media.SONG_EVENT_STOP
            || songEvent == Media.SONG_EVENT_SKIP_NEXT
            || songEvent == Media.SONG_EVENT_SKIP_PREVIOUS;

        var previous = mLastWritten[contentRefId];
        if (!isCheckpoint && previous instanceof Lang.Number) {
            var moved = playbackPosition - previous;
            if (moved < WRITE_THRESHOLD && moved > -WRITE_THRESHOLD) {
                return;
            }
        }

        Store.setPosition(contentRefId, playbackPosition);
        mLastWritten[contentRefId] = playbackPosition;
    }
}
