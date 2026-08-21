"""Per-show overrides for how many episodes to keep and how old they can be.

The global episodes_per_feed quota is count-based only: a show with an old
backlog can have its quota permanently occupied by episodes that are ancient
by publish date but were only downloaded a couple of days ago, starving out
genuinely new episodes forever. keep_episodes lets a show use a different
quota than the server default, and max_age_days adds a hard cutoff on publish
date that applies regardless of quota.
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
NOW = 1_700_000_000


def make_feed(title, keep_episodes=None, max_age_days=None):
    return db.execute(
        "INSERT INTO feeds (url, title, added_at, keep_episodes, max_age_days) "
        "VALUES (?,?,?,?,?)",
        (f"https://x/{title}.rss", title, db.now(), keep_episodes, max_age_days),
    )


def add_episode(feed_id, n, state, published):
    return db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, source_url, state, "
        "attempts, error, created_at, file_path, downloaded_at) "
        "VALUES (?,?,?,?,?,?,0,'',?,?,?)",
        (feed_id, f"g{feed_id}-{n}", f"Ep {n}", published,
         f"https://x/{n}.mp3", state, db.now(),
         "/tmp/x.mp3" if state == "ready" else "", db.now() if state == "ready" else 0),
    )


print("-- a show can raise its own quota above the server default --")
f1 = make_feed("BigQuota", keep_episodes=5)
for n in range(5):
    add_episode(f1, n, "pending", NOW - n * 100)

run(media.download_pending())
check("all 5 became ready, not capped at the server default of 3",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f1,))["n"] == 5)

print("\n-- a show can lower its own quota below the server default --")
f2 = make_feed("SmallQuota", keep_episodes=1)
for n in range(3):
    add_episode(f2, n, "pending", NOW - n * 100)

run(media.download_pending())
check("only 1 ready, not the server default of 3",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f2,))["n"] == 1)

print("\n-- max_age_days keeps an old backlog episode out of the quota entirely --")
f3 = make_feed("OldBacklog", max_age_days=30)
ancient = add_episode(f3, 0, "ready", db.now() - 400 * 86400)  # published ~13 months ago
recent1 = add_episode(f3, 1, "pending", db.now() - 5 * 86400)
recent2 = add_episode(f3, 2, "pending", db.now() - 2 * 86400)

run(media.download_pending())
row = db.query_one("SELECT state, file_path FROM episodes WHERE ref_id = ?", (ancient,))
check("the ancient ready episode was expired even though quota wasn't full",
      row["state"] == "expired", row["state"])
check("its file was removed from disk", row["file_path"] == "")
check("the two recent episodes were downloaded",
      db.query_one("SELECT COUNT(*) n FROM episodes WHERE feed_id=? AND state='ready'",
                   (f3,))["n"] == 2)

print("\n-- max_age_days blocks a stale pending episode from ever being attempted --")
f4 = make_feed("NeverAttempt", max_age_days=10)
stale_pending = add_episode(f4, 0, "pending", db.now() - 100 * 86400)

run(media.download_pending())
row = db.query_one("SELECT state FROM episodes WHERE ref_id = ?", (stale_pending,))
check("it was skipped rather than downloaded", row["state"] == "skipped", row["state"])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
