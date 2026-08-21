"""Episodes wrongly stuck as 'skipped' by the old quota-counting bug need a
one-time reset on upgrade, since 'skipped' rows are otherwise never
reconsidered by any later poll -- deleting or retrying some other episode on
the same show does nothing for them.
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


print("-- a stuck 'skipped' episode is revived and downloaded on the first reset --")
f1 = make_feed("Victim")
for n in range(3):
    add_episode(f1, n, "ready", db.now() - n * 100)
victim = add_episode(f1, 3, "skipped", db.now())  # newest of all, wrongly skipped

count = media.reset_stale_skips()
check("reset touched exactly the one skipped episode", count == 1, str(count))
row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (victim,))
check("it is 'pending' again, not still 'skipped'", row["state"] == "pending", row["state"])

run(media.download_pending())
row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (victim,))
check("the corrected download pass picked it up and downloaded it",
      row["state"] == "ready", row["state"])

print("\n-- the reset only ever runs once --")
add_episode(f1, 4, "skipped", db.now())
count2 = media.reset_stale_skips()
check("second call is a no-op", count2 == 0, str(count2))

print("\n-- a genuinely surplus episode reset by the migration settles back to skipped --")
f2 = make_feed("GenuineSurplus")
for n in range(3):
    add_episode(f2, n, "ready", db.now() - n * 100)
surplus = add_episode(f2, 3, "skipped", db.now() - 10_000)  # oldest of the four, legitimately surplus

db.execute(
    "UPDATE episodes SET state = 'pending' WHERE ref_id = ?", (surplus,)
)  # simulate what reset_stale_skips would have done to it
run(media.download_pending())
row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (surplus,))
check("the corrected pass puts it right back to 'skipped' -- it's still surplus",
      row["state"] == "skipped", row["state"])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
