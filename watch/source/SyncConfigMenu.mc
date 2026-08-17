using Toybox.Communications;
using Toybox.Lang;
using Toybox.PersistedContent;
using Toybox.WatchUi;

//! Shown from the provider's sync settings. Reports what is on the watch and
//! lets the user confirm the server is reachable before starting a real sync.
class SyncConfigMenu extends WatchUi.Menu2 {

    function initialize() {
        Menu2.initialize({ :title => WatchUi.loadResource(Rez.Strings.AppName) });

        var configured = Config.isConfigured();
        addItem(new WatchUi.MenuItem(
            WatchUi.loadResource(Rez.Strings.SyncStatus),
            configured
                ? WatchUi.loadResource(Rez.Strings.SyncReady)
                : WatchUi.loadResource(Rez.Strings.SyncNotConfigured),
            :status,
            null));

        addItem(new WatchUi.MenuItem(
            WatchUi.loadResource(Rez.Strings.SyncOnDevice),
            episodeCountLabel(),
            :count,
            null));

        if (configured) {
            addItem(new WatchUi.MenuItem(
                WatchUi.loadResource(Rez.Strings.SyncCheckServer),
                null,
                :check,
                null));
        } else {
            addItem(new WatchUi.MenuItem(
                WatchUi.loadResource(Rez.Strings.SyncNotConfigured),
                WatchUi.loadResource(Rez.Strings.SyncNotConfiguredSub),
                :help,
                null));
        }
    }

    hidden function episodeCountLabel() {
        var count = Store.getLibrary().size();
        return count == 1 ? "1 episode" : count.toString() + " episodes";
    }

    //! Replace a row's sub-label in place and repaint.
    function updateSubLabel(id, text) {
        var index = findItemById(id);
        if (index >= 0) {
            var item = getItem(index);
            if (item != null) {
                item.setSubLabel(text);
                WatchUi.requestUpdate();
            }
        }
    }
}

class SyncConfigMenuDelegate extends WatchUi.Menu2InputDelegate {

    hidden var mMenu;

    function initialize(menu) {
        Menu2InputDelegate.initialize();
        mMenu = menu;
    }

    function onSelect(item) {
        if (item.getId() != :check) {
            return;
        }

        mMenu.updateSubLabel(:check, WatchUi.loadResource(Rez.Strings.SyncChecking));

        Communications.makeWebRequest(
            Config.serverUrl() + "/api/v1/ping",
            null,
            {
                :method => Communications.HTTP_REQUEST_METHOD_GET,
                :headers => Config.authHeaders(),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
            },
            method(:onPing)
        );
    }

    function onPing(responseCode as Lang.Number, data as Lang.Dictionary or Lang.String or PersistedContent.Iterator or Null) as Void {
        var text;
        if (responseCode == 200 && data instanceof Lang.Dictionary) {
            var available = data["eps"];
            text = (available instanceof Lang.Number ? available.toString() : "?")
                + " ready on server";
        } else if (responseCode == 401 || responseCode == 403) {
            text = WatchUi.loadResource(Rez.Strings.SyncErrAuth);
        } else {
            text = WatchUi.loadResource(Rez.Strings.SyncErrNetwork)
                + " (" + responseCode.toString() + ")";
        }
        mMenu.updateSubLabel(:check, text);
    }
}
