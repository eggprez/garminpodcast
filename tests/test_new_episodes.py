"""A feed already at its ready-episode quota must still pick up new episodes.

_download_pass() used to compute `ready` as a global count taken before
walking the candidates. Once a feed reached its quota, that count alone
satisfied the `ready >= target` break on the very first (newest) candidate,
so retire_unneeded() swept any brand-new pending episode straight into
'skipped' without ever attempting it -- it would never appear in the
library, and a later poll would not rediscover it as new either.
"""
import asyncio
import os
import pathlib
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(
    PODCAST_DATA_DIR=TMP,
    PODCAST_ADMIN_PASSWORD="test-pw",
    PODCAST_EPISODES_PER_FEED="3",
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import db, media

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def fake_download(url, dest):
    dest.write_bytes(b"audio")


async def fake_probe(path):
    return {"codec": "mp3", "bitrate_kbps": 128, "channels": 2, "duration": 600}


async def fake_encode(src, dest, title, artist, reencode):
    dest.write_bytes(b"encoded")


media._download = fake_download
media.probe = fake_probe
media._encode = fake_encode
run = asyncio.get_event_loop().run_until_complete

db.connect()


def make_feed(title):
    return db.execute("INSERT INTO feeds (url, title, added_at) VALUES (?,?,?)",
                      (f"https://x/{title}.rss", title, db.now()))


def add_episode(feed_id, n, state, published):
    return db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, source_url, state, "
        "attempts, error, created_at, file_path, downloaded_at) "
        "VALUES (?,?,?,?,?,?,0,'',?,?,?)",
        (feed_id, f"g{feed_id}-{n}", f"Ep {n}", published,
         f"https://x/{n}.mp3", state, db.now(),
         "/tmp/x.mp3" if state == "ready" else "", db.now() if state == "ready" else 0),
    )


print("-- a feed already at quota still fetches a genuinely new episode --")
f1 = make_feed("AtQuota")
base = 1_700_000_000
oldest = add_episode(f1, 0, "ready", base - 300)
add_episode(f1, 1, "ready", base - 200)
add_episode(f1, 2, "ready", base - 100)
new_ep = add_episode(f1, 3, "pending", base)  # newer than everything above

run(media.download_pending())

row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (new_ep,))
check("the new episode was downloaded, not skipped", row["state"] == "ready", row["state"])

oldest_row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (oldest,))
check("the oldest ready episode was retired to make room", oldest_row["state"] == "expired", oldest_row["state"])

check("ready count stays at the quota instead of growing",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f1,))["n"] == 3)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
