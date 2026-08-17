"""Regression test for concurrent download passes.

Adding several feeds at once fired one download_pending() task per feed. They
all queried the same pending rows and shared one scratch path per episode, so
the first to finish deleted the file the others were still reading, producing
"ffprobe failed: /data/audio/.N.raw: No such file or directory".
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
    PODCAST_EPISODES_PER_FEED="5",
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from server import db, media
from server.config import settings

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# --- stub out the network and ffmpeg, keeping the real file handling ---------
downloads = []


async def fake_download(url, dest):
    downloads.append(dest.name)
    await asyncio.sleep(0.05)  # long enough for passes to overlap
    dest.write_bytes(b"fake audio bytes")


async def fake_probe(path):
    if not path.exists():
        raise RuntimeError(f"ffprobe failed: {path}: No such file or directory")
    return {"codec": "mp3", "bitrate_kbps": 128, "channels": 2, "duration": 1800}


async def fake_encode(src, dest, title, artist, reencode):
    if not src.exists():
        raise RuntimeError(f"ffmpeg failed: Error opening input file {src}")
    dest.write_bytes(b"encoded")


media._download = fake_download
media.probe = fake_probe
media._encode = fake_encode

db.connect()
feed_id = db.execute(
    "INSERT INTO feeds (url, title, added_at) VALUES (?,?,?)",
    ("https://x/f.rss", "Race Test", db.now()),
)
for i in range(5):
    db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, source_url, "
        "state, created_at) VALUES (?,?,?,?,?, 'pending', ?)",
        (feed_id, f"g{i}", f"Episode {i}", 1_700_000_000 + i, f"https://x/{i}.mp3", db.now()),
    )

print("-- 8 concurrent download passes over 5 episodes --")
results = asyncio.get_event_loop().run_until_complete(
    asyncio.gather(*[media.download_pending() for _ in range(8)])
)

rows = db.query("SELECT ref_id, state, attempts, error FROM episodes ORDER BY ref_id")
ready = [r for r in rows if r["state"] == "ready"]
errored = [r for r in rows if r["state"] == "error"]

check("all 5 episodes reached 'ready'", len(ready) == 5,
      f"ready={len(ready)} errored={[(r['ref_id'], r['error'][:60]) for r in errored]}")
check("no episode was attempted more than once",
      all(r["attempts"] == 1 for r in rows),
      str([(r["ref_id"], r["attempts"]) for r in rows]))
check("each episode downloaded exactly once", len(downloads) == 5, f"{len(downloads)} downloads")
check("scratch files all cleaned up",
      list(settings.audio_dir.glob(".*.raw")) == [])
check("total reported equals episodes downloaded", sum(results) == 5, str(results))

print("\n-- process_episode is safe even without the pass-level lock --")
# The asyncio.Lock only serialises callers inside one process. Under
# `uvicorn --workers 2` the passes run in separate processes and the lock buys
# nothing, so the DB claim and the per-attempt scratch path have to hold on
# their own. Calling process_episode directly bypasses the lock and exercises
# exactly that.
db.execute("INSERT INTO feeds (id, url, title, added_at) VALUES (99,?,?,?)",
           ("https://x/direct.rss", "Direct", db.now()))
direct = db.execute(
    "INSERT INTO episodes (feed_id, guid, title, published, source_url, state, created_at) "
    "VALUES (99,?,?,?,?, 'pending', ?)",
    ("d1", "Direct Episode", 1_700_000_000, "https://x/d.mp3", db.now()),
)
downloads.clear()
outcomes = asyncio.get_event_loop().run_until_complete(
    asyncio.gather(*[media.process_episode(direct) for _ in range(6)])
)
row = db.query_one("SELECT state, attempts, error FROM episodes WHERE ref_id = ?", (direct,))
check("exactly one of 6 racing calls claimed the episode",
      sum(1 for o in outcomes if o) == 1, str(outcomes))
check("it downloaded only once", len(downloads) == 1, f"{len(downloads)} downloads")
check("episode is 'ready', not clobbered into 'error'",
      row["state"] == "ready", f"{row['state']}: {row['error'][:80]}")
check("attempts incremented once", row["attempts"] == 1, str(row["attempts"]))

print("\n-- failures are retried, then given up on --")
media._download = fake_download  # keep
async def always_fail(url, dest):
    raise RuntimeError("network is down")
media._download = always_fail

bad_feed = db.execute("INSERT INTO feeds (url, title, added_at) VALUES (?,?,?)",
                      ("https://x/bad.rss", "Bad", db.now()))
bad = db.execute(
    "INSERT INTO episodes (feed_id, guid, title, published, source_url, state, created_at) "
    "VALUES (?,?,?,?,?, 'pending', ?)",
    (bad_feed, "bad1", "Broken", 1_700_000_000, "https://x/bad.mp3", db.now()),
)

for _ in range(5):
    asyncio.get_event_loop().run_until_complete(media.download_pending())

row = db.query_one("SELECT state, attempts FROM episodes WHERE ref_id = ?", (bad,))
check("failed episode stops at MAX_ATTEMPTS",
      row["attempts"] == media.MAX_ATTEMPTS, f"attempts={row['attempts']}")
check("failed episode is left in 'error'", row["state"] == "error", row["state"])

print("\n-- interrupted downloads are requeued on restart --")
db.execute("UPDATE episodes SET state = 'downloading' WHERE ref_id = ?", (bad,))
media.reset_interrupted()
check("stranded 'downloading' row returned to 'pending'",
      db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (bad,))["state"] == "pending")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
