using Toybox.Lang;

module Util {

    //! "1:02:03" past an hour, otherwise "2:03".
    function formatTime(seconds) {
        if (!(seconds instanceof Lang.Number) || seconds < 0) {
            seconds = 0;
        }
        var hours = seconds / 3600;
        var minutes = (seconds % 3600) / 60;
        var secs = seconds % 60;
        if (hours > 0) {
            return hours.format("%d") + ":" + minutes.format("%02d") + ":" + secs.format("%02d");
        }
        return minutes.format("%d") + ":" + secs.format("%02d");
    }

    //! Coarser wording for durations the user only needs a feel for.
    function formatApprox(seconds) {
        if (!(seconds instanceof Lang.Number) || seconds <= 0) {
            return "0 min";
        }
        if (seconds < 3600) {
            var mins = seconds / 60;
            return (mins < 1 ? 1 : mins).format("%d") + " min";
        }
        var hours = seconds / 3600;
        var rem = (seconds % 3600) / 60;
        if (rem == 0) {
            return hours.format("%d") + " hr";
        }
        return hours.format("%d") + " hr " + rem.format("%d") + " min";
    }

    //! Truncate for a menu row so long episode titles do not overflow.
    function ellipsize(text, maxChars) {
        if (!(text instanceof Lang.String)) {
            return "";
        }
        if (text.length() <= maxChars) {
            return text;
        }
        return text.substring(0, maxChars - 1) + "...";
    }
}
