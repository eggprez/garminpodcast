using Toybox.Application;
using Toybox.Media;
using Toybox.WatchUi;

//! Audio content provider entry point.
//!
//! The system launches this app in one of three modes and asks for a different
//! object each time:
//!   sync mode     -> getSyncDelegate()              (downloads episodes)
//!   browse mode   -> getPlaybackConfigurationView() (pick what to play)
//!   playback mode -> getContentDelegate(args)       (feeds the media player)
class PodcastApp extends Application.AudioContentProviderApp {

    function initialize() {
        AudioContentProviderApp.initialize();
    }

    function getSyncDelegate() {
        return new PodcastSyncDelegate();
    }

    //! `args` is whatever was handed to Media.startPlayback() - for us, the
    //! episode the user chose and the position they chose to start from.
    function getContentDelegate(args) {
        return new PodcastContentDelegate(args);
    }

    function getPlaybackConfigurationView() {
        // Browsing is the most frequent entry point, so it is a natural place
        // to enforce the 2-day retention without waiting for a sync.
        Store.purgeExpired();
        var menu = new BrowseMenu();
        return [menu, new BrowseMenuDelegate()];
    }

    function getSyncConfigurationView() {
        var menu = new SyncConfigMenu();
        return [menu, new SyncConfigMenuDelegate(menu)];
    }

    function getProviderIconInfo() {
        return new Media.ProviderIconInfo(Rez.Drawables.LauncherIcon, 0x4CBB17);
    }
}
