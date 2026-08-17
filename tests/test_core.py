"""Exercise the server's core logic without network or ffmpeg."""
import asyncio
import os
import sys
import pathlib
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(
    PODCAST_DATA_DIR=TMP,
    PODCAST_ADMIN_PASSWORD="test-pw",
    PODCAST_EPISODES_PER_FEED="3",
    PODCAST_RETENTION_DAYS="2",
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import feedparser
from server import db, feeds, media
from server.config import settings

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>The © Test Show</title>
  <image><url>https://x/img.png</url></image>
  <item>
    <title>Episode Three — naïve</title>
    <guid>guid-3</guid>
    <pubDate>Wed, 13 Aug 2025 10:00:00 GMT</pubDate>
    <itunes:duration>1:02:03</itunes:duration>
    <enclosure url="https://x/3.mp3" type="audio/mpeg" length="100"/>
  </item>
  <item>
    <title>Episode Two</title>
    <guid>guid-2</guid>
    <pubDate>Tue, 12 Aug 2025 10:00:00 GMT</pubDate>
    <itunes:duration>45:30</itunes:duration>
    <enclosure url="https://x/2.mp3" type="audio/mpeg" length="100"/>
  </item>
  <item>
    <title>Episode One</title>
    <guid>guid-1</guid>
    <pubDate>Mon, 11 Aug 2025 10:00:00 GMT</pubDate>
    <itunes:duration>3600</itunes:duration>
    <enclosure url="https://x/1.mp3" type="audio/mpeg" length="100"/>
  </item>
  <item>
    <title>No audio here</title>
    <guid>guid-0</guid>
    <pubDate>Sun, 10 Aug 2025 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

fails = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


print("\n-- duration parsing --")
check("HH:MM:SS", feeds.parse_duration("1:02:03") == 3723)
check("MM:SS", feeds.parse_duration("45:30") == 2730)
check("bare seconds", feeds.parse_duration("3600") == 3600)
check("empty -> 0", feeds.parse_duration(None) == 0)
check("garbage -> 0", feeds.parse_duration("abc") == 0)

print("\n-- text sanitising (Garmin ID3 safety) --")
check("copyright stripped", "©" not in feeds.clean_text("The © Show"))
check("non-ascii stripped", feeds.clean_text("naïve") == "naive")
check("empty -> Untitled", feeds.clean_text("©") == "Untitled")
check("length capped", len(feeds.clean_text("x" * 500)) == 120)

print("\n-- feed reconciliation --")
db.connect()
feed_id = db.execute("INSERT INTO feeds (url, added_at) VALUES (?, ?)",
                     ("https://x/feed.rss", db.now()))


async def fake_fetch(url):
    return feedparser.parse(RSS.encode())


feeds.fetch_feed = fake_fetch

n = asyncio.get_event_loop().run_until_complete(feeds.refresh_feed(feed_id))
check("3 audio episodes inserted (item without enclosure skipped)", n == 3)

rows = db.query("SELECT * FROM episodes ORDER BY published DESC")
check("newest first ordering", rows[0]["title"] == "Episode Three naive")
check("duration parsed into row", rows[0]["duration"] == 3723)
check("ref_id is a positive int (Garmin refId)", isinstance(rows[0]["ref_id"], int) and rows[0]["ref_id"] > 0)
check("feed title stored", db.query_one("SELECT title FROM feeds WHERE id=?", (feed_id,))["title"] == "The © Test Show")

# Re-running must not duplicate.
n2 = asyncio.get_event_loop().run_until_complete(feeds.refresh_feed(feed_id))
check("re-poll inserts nothing new", n2 == 0)
check("still exactly 3 episodes", len(db.query("SELECT 1 FROM episodes")) == 3)

print("\n-- transcode decision (mode=auto) --")
check("clean 96k mp3 -> stream copy",
      not media.needs_reencode({"codec": "mp3", "bitrate_kbps": 96, "channels": 2}))
check("320k mp3 -> re-encode",
      media.needs_reencode({"codec": "mp3", "bitrate_kbps": 320, "channels": 2}))
check("aac -> re-encode",
      media.needs_reencode({"codec": "aac", "bitrate_kbps": 64, "channels": 2}))
check("unknown bitrate -> re-encode",
      media.needs_reencode({"codec": "mp3", "bitrate_kbps": 0, "channels": 2}))

print("\n-- retention --")
ref = rows[0]["ref_id"]
path = settings.audio_dir / f"{ref}.mp3"
path.write_bytes(b"fake audio")
old = db.now() - 3 * 86400  # older than the 2-day test window
db.execute("UPDATE episodes SET state='ready', file_path=?, file_size=10, downloaded_at=? "
           "WHERE ref_id=?", (str(path), old, ref))
removed = media.purge_expired()
check("expired file deleted from disk", not path.exists())
check("row marked expired", db.query_one("SELECT state FROM episodes WHERE ref_id=?", (ref,))["state"] == "expired")

orphan = settings.audio_dir / "9999.mp3"
orphan.write_bytes(b"orphan")
media.purge_expired()
check("orphan file swept", not orphan.exists())

print("\n-- api token --")
from server.auth import get_api_token, regenerate_api_token, check_admin_password
t1 = get_api_token()
check("token generated and stable", t1 and t1 == get_api_token())
check("rotation changes it", regenerate_api_token() != t1)
check("correct password accepted", check_admin_password("test-pw"))
check("wrong password rejected", not check_admin_password("nope"))

print("\n-- zero-config startup (no env vars at all) --")
import subprocess
probe = """
import sys, tempfile, pathlib
sys.path.insert(0, %r)
import os
os.environ["PODCAST_DATA_DIR"] = tempfile.mkdtemp()
from server.config import settings
assert settings.admin_user == "admin", settings.admin_user
assert settings.admin_password == "changeme"
assert settings.uses_default_password is True
assert settings.retention_days == 14
assert settings.episodes_per_feed == 5
assert settings.transcode_mode == "auto"
assert len(settings.secret_key) > 20
# The generated key must persist across a second load.
key_file = settings.data_dir / "secret.key"
assert key_file.exists(), "secret.key not written"
assert key_file.read_text().strip() == settings.secret_key
assert oct(key_file.stat().st_mode)[-3:] == "600", oct(key_file.stat().st_mode)
print("OK")
""" % str(pathlib.Path(__file__).resolve().parent.parent)

clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PODCAST_")}
clean_env["PATH"] = os.environ.get("PATH", "")
res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=clean_env)
check("starts with no PODCAST_* env vars and sane defaults",
      res.returncode == 0 and "OK" in res.stdout)
if res.returncode != 0:
    print("    " + (res.stderr.strip().splitlines() or ["?"])[-1])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
