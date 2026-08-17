using Toybox.Lang;
using Toybox.Media;
using Toybox.WatchUi;

//! Asked every time an episode is selected: where should this start?
//!
//! Two ways to answer. "Resume" uses the position recorded by
//! PodcastContentDelegate. The "time left" entries let the user say how much of
//! the episode remains - useful when they last listened somewhere else - and
//! the start point is derived as (duration - remaining).
//!
//! Each menu item's identifier *is* the start offset in seconds, so selecting
//! one needs no further lookup.
class ResumeMenu extends WatchUi.Menu2 {

    function initialize(systemId) {
        var library = Store.getLibrary();
        var entry = library[systemId];
        var title = entry != null
            ? Util.ellipsize(entry[Store.F_TITLE], 22)
            : WatchUi.loadResource(Rez.Strings.MenuHowFar);

        Menu2.initialize({ :title => title });
        build(systemId, entry);
    }

    hidden function build(systemId, entry) {
        var duration = entry != null ? entry[Store.F_DURATION] : 0;
        var position = Store.getPosition(systemId);

        // Hide "Resume" when barely started, or when the last position was
        // effectively the end of the episode.
        var nearEnd = duration > 0 && position >= duration - Config.COMPLETE_MARGIN_SECONDS;
        if (position >= Config.MIN_RESUME_SECONDS && !nearEnd) {
            addItem(new WatchUi.MenuItem(
                WatchUi.loadResource(Rez.Strings.MenuResume),
                Util.formatTime(position),
                position,
                null));
        }

        addItem(new WatchUi.MenuItem(
            WatchUi.loadResource(Rez.Strings.MenuStartOver),
            WatchUi.loadResource(Rez.Strings.MenuStartOverSub),
            0,
            null));

        // Without a known duration there is nothing to subtract from, so the
        // time-left choices are meaningless and get left out.
        if (duration <= 0) {
            return;
        }

        // Offered as "time left" choices, filtered to those shorter than the
        // episode itself.
        var choices = [300, 600, 900, 1200, 1800, 2700, 3600];
        var timeLeftLabel = WatchUi.loadResource(Rez.Strings.MenuTimeLeft);
        for (var i = 0; i < choices.size(); i++) {
            var remaining = choices[i];
            if (remaining >= duration) {
                continue;
            }
            addItem(new WatchUi.MenuItem(
                timeLeftLabel,
                Util.formatApprox(remaining),
                duration - remaining,
                null));
        }
    }
}

class ResumeMenuDelegate extends WatchUi.Menu2InputDelegate {

    hidden var mSystemId;

    function initialize(systemId) {
        Menu2InputDelegate.initialize();
        mSystemId = systemId;
    }

    function onSelect(item) {
        var startPos = item.getId();
        if (!(startPos instanceof Lang.Number) || startPos < 0) {
            startPos = 0;
        }

        // Record the chosen point up front so it survives even if the player
        // never reports a position for this episode.
        if (startPos > 0) {
            Store.setPosition(mSystemId, startPos);
        } else {
            Store.clearPosition(mSystemId);
        }

        // Hands the app back to the system in playback mode; these args arrive
        // at PodcastApp.getContentDelegate().
        Media.startPlayback({ "id" => mSystemId, "pos" => startPos });
    }
}
