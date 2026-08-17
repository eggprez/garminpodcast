using Toybox.Lang;
using Toybox.WatchUi;

//! The episode picker shown when the user configures playback for this
//! provider. Selecting an episode opens the resume prompt rather than starting
//! playback immediately.
class BrowseMenu extends WatchUi.Menu2 {

    function initialize() {
        Menu2.initialize({ :title => WatchUi.loadResource(Rez.Strings.MenuEpisodes) });
        build();
    }

    hidden function build() {
        var ids = Store.sortedIds();
        var library = Store.getLibrary();

        if (ids.size() == 0) {
            addItem(new WatchUi.MenuItem(
                WatchUi.loadResource(Rez.Strings.MenuNoEpisodes),
                WatchUi.loadResource(Rez.Strings.MenuNoEpisodesSub),
                :empty,
                null));
            return;
        }

        for (var i = 0; i < ids.size(); i++) {
            var id = ids[i];
            var entry = library[id];
            addItem(new WatchUi.MenuItem(
                Util.ellipsize(entry[Store.F_TITLE], 34),
                subLabelFor(id, entry),
                id,
                null));
        }
    }

    //! Shows how much is left when an episode is part-played, otherwise its
    //! length - with a warning once auto-delete is within a day.
    hidden function subLabelFor(id, entry) {
        var duration = entry[Store.F_DURATION];
        var position = Store.getPosition(id);
        var text;

        if (position >= Config.MIN_RESUME_SECONDS && duration > position) {
            text = Util.formatApprox(duration - position) + " left";
        } else if (duration > 0) {
            text = Util.formatApprox(duration);
        } else {
            text = entry[Store.F_SHOW];
        }

        var expiresIn = Store.secondsUntilExpiry(id);
        if (expiresIn > 0 && expiresIn < 86400) {
            text += " - expires soon";
        }
        return text;
    }
}

class BrowseMenuDelegate extends WatchUi.Menu2InputDelegate {

    function initialize() {
        Menu2InputDelegate.initialize();
    }

    function onSelect(item) {
        var id = item.getId();
        if (id == :empty) {
            return;
        }
        WatchUi.pushView(new ResumeMenu(id), new ResumeMenuDelegate(id), WatchUi.SLIDE_LEFT);
    }
}
