using Toybox.Application;
using Toybox.Lang;

//! Reads the values the user typed into Garmin Connect Mobile, plus the
//! tunables that are not worth exposing as settings.
module Config {

    //! Downloads older than this are removed from the watch on the next sync
    //! or browse. Two days, per the app's design.
    const RETENTION_SECONDS = 172800;

    //! Ignore a stored position below this - restarting a barely-played
    //! episode is less annoying than resuming three seconds in.
    const MIN_RESUME_SECONDS = 30;

    //! Treat an episode as finished when this close to the end, so it does not
    //! come back offering to resume the closing credits.
    const COMPLETE_MARGIN_SECONDS = 45;

    function getProp(key, fallback) {
        var value = null;
        try {
            value = Application.Properties.getValue(key);
        } catch (e) {
            value = null;
        }
        return value == null ? fallback : value;
    }

    //! Server origin with any trailing slash removed, so callers can safely
    //! concatenate paths that begin with "/".
    function serverUrl() {
        var url = getProp("serverUrl", "");
        if (!(url instanceof Lang.String)) {
            return "";
        }
        url = trim(url);
        while (url.length() > 0 && url.substring(url.length() - 1, url.length()).equals("/")) {
            url = url.substring(0, url.length() - 1);
        }
        return url;
    }

    function apiToken() {
        var token = getProp("apiToken", "");
        return token instanceof Lang.String ? trim(token) : "";
    }

    function episodeLimit() {
        var limit = getProp("episodeLimit", 10);
        if (!(limit instanceof Lang.Number) || limit < 1) {
            return 10;
        }
        return limit > 50 ? 50 : limit;
    }

    function isConfigured() {
        return serverUrl().length() > 0 && apiToken().length() > 0;
    }

    //! Authorization header shared by every request. The token never appears in
    //! a URL, so it stays out of proxy and server access logs.
    function authHeaders() {
        return { "Authorization" => "Bearer " + apiToken() };
    }

    function trim(text) {
        var start = 0;
        var end = text.length();
        while (start < end && text.substring(start, start + 1).equals(" ")) {
            start++;
        }
        while (end > start && text.substring(end - 1, end).equals(" ")) {
            end--;
        }
        return text.substring(start, end);
    }
}
