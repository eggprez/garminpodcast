using Toybox.Lang;
using Toybox.Media;

//! Feeds downloaded episodes to the system media player, newest first.
//!
//! The resume point is delivered by returning a Media.ActiveContent instead of
//! a plain Content for the very first track handed over - ActiveContent is the
//! only way to tell the player where to begin. It is applied exactly once, so
//! skipping back to the episode later starts it from the top rather than
//! silently jumping to the old position again.
class PodcastIterator extends Media.ContentIterator {

    hidden var mPlaylist;
    hidden var mIndex;
    hidden var mStartPos;
    hidden var mStartApplied;

    function initialize(startId, startPos) {
        ContentIterator.initialize();
        mPlaylist = Store.sortedIds();
        mIndex = 0;
        mStartPos = startPos instanceof Lang.Number && startPos > 0 ? startPos : 0;
        mStartApplied = false;

        if (startId != null) {
            for (var i = 0; i < mPlaylist.size(); i++) {
                if (mPlaylist[i] == startId) {
                    mIndex = i;
                    break;
                }
            }
        }
    }

    function get() {
        return contentAt(mIndex, true);
    }

    function next() {
        if (mIndex >= mPlaylist.size() - 1) {
            return null;
        }
        mIndex++;
        return contentAt(mIndex, false);
    }

    function previous() {
        if (mIndex <= 0) {
            return null;
        }
        mIndex--;
        return contentAt(mIndex, false);
    }

    function peekNext() {
        return contentAt(mIndex + 1, false);
    }

    function peekPrevious() {
        return contentAt(mIndex - 1, false);
    }

    function canSkip() {
        return mPlaylist.size() > 1;
    }

    function shuffling() {
        return false;
    }

    //! Null leaves the player on its defaults, which is what a podcast wants:
    //! no shuffle, no repeat.
    function repeatMode() {
        return null;
    }

    function getPlaybackProfile() {
        return null;
    }

    hidden function contentAt(index, allowResume) {
        if (index < 0 || index >= mPlaylist.size()) {
            return null;
        }

        var ref = new Media.ContentRef(mPlaylist[index], Media.CONTENT_TYPE_AUDIO);
        var content = Media.getCachedContentObj(ref);
        if (content == null) {
            // The cache and our bookkeeping have drifted apart; drop the entry
            // so the list stops offering an episode that cannot be played.
            Store.remove(mPlaylist[index]);
            return null;
        }

        if (allowResume && !mStartApplied && mStartPos > 0) {
            mStartApplied = true;
            return new Media.ActiveContent(ref, content.getMetadata(), mStartPos);
        }
        return content;
    }
}
