"""Failed episodes must not become permanent library clutter.

The first retry implementation computed free slots per feed: a feed already
holding its quota of ready episodes got skipped entirely, so anything that had
failed could never be retried and displayed a stale error forever.
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


def add_episode(feed_id, n, state, attempts=0, error=""):
    return db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, source_url, state, "
        "attempts, error, created_at, file_path, downloaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (feed_id, f"g{feed_id}-{n}", f"Ep {n}", 1_700_000_000 - n * 100,
         f"https://x/{n}.mp3", state, attempts, error, db.now(),
         "/tmp/x.mp3" if state == "ready" else "", db.now() if state == "ready" else 0),
    )


print("-- a stale error inside a quota-met feed is retired, not shown forever --")
f1 = make_feed("AtQuota")
for n in range(3):
    add_episode(f1, n, "ready")
stale = add_episode(f1, 3, "error", error="ffprobe failed: .3.raw: No such file or directory")

run(media.download_pending())
row = db.query_one("SELECT state, error FROM episodes WHERE ref_id = ?", (stale,))
check("surplus failure no longer shows as an error", row["state"] == "skipped", row["state"])
check("its stale message is cleared", row["error"] == "", row["error"])
check("the 3 ready episodes are untouched",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f1,))["n"] == 3)

print("\n-- a failure the feed actually needs IS retried --")
f2 = make_feed("BelowQuota")
add_episode(f2, 0, "ready")
needed = add_episode(f2, 1, "error", error="ffmpeg failed: could not open")
add_episode(f2, 2, "pending")

run(media.download_pending())
row = db.query_one("SELECT state, attempts FROM episodes WHERE ref_id = ?", (needed,))
check("needed failure was retried and recovered", row["state"] == "ready", row["state"])
check("feed reached its quota of 3",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f2,))["n"] == 3)

print("\n-- a permanently broken episode does not starve the feed --")
async def always_fail(url, dest):
    raise RuntimeError("gone")

f3 = make_feed("BrokenNewest")
broken = add_episode(f3, 0, "pending")
for n in range(1, 5):
    add_episode(f3, n, "pending")

original = media._download
media._download = always_fail
# Burn through the newest episode's attempt budget.
for _ in range(media.MAX_ATTEMPTS):
    run(media.process_episode(broken))
media._download = original

run(media.download_pending())
check("exhausted episode is left alone",
      db.query_one("SELECT state FROM episodes WHERE ref_id=?", (broken,))["state"] == "error")
check("older episodes filled the quota anyway",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f3,))["n"] == 3,
      str(db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'", (f3,))["n"]))

print("\n-- the Retry button clears the error and the attempt budget --")
before = db.query_one("SELECT state, attempts FROM episodes WHERE ref_id=?", (broken,))
media.retry_failed(broken)
after = db.query_one("SELECT state, attempts, error FROM episodes WHERE ref_id=?", (broken,))
check("state back to pending", after["state"] == "pending", after["state"])
check("attempts reset to 0", after["attempts"] == 0, str(after["attempts"]))
check("error text cleared", after["error"] == "")
check("it really was exhausted beforehand", before["attempts"] >= media.MAX_ATTEMPTS)

print("\n-- retry-all only touches failures --")
db.execute("UPDATE episodes SET state='error', error='boom' WHERE ref_id=?", (stale,))
ready_before = db.query_one("SELECT COUNT(*) n FROM episodes WHERE state='ready'")["n"]
n = media.retry_failed()
check("every failure requeued", n >= 1, str(n))
check("ready episodes untouched",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE state='ready'")["n"] == ready_before)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
