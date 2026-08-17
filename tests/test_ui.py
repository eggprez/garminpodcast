"""The show library, per-show detail, and the runtime poll-interval setting."""
import os
import pathlib
import sys
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(
    PODCAST_DATA_DIR=TMP,
    PODCAST_ADMIN_USER="admin",
    PODCAST_ADMIN_PASSWORD="s3cret",
    PODCAST_SECRET_KEY="0" * 32,
    PODCAST_EPISODES_PER_FEED="5",
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from server import db, prefs
from server.config import settings
from server.main import app

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


with TestClient(app) as client:
    art = pathlib.Path(TMP) / "artwork" / "1.jpg"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

    f1 = db.execute(
        "INSERT INTO feeds (url, title, author, image_url, artwork_path, added_at, last_checked) "
        "VALUES (?,?,?,?,?,?,?)",
        ("https://x/a.rss", "The Bulwark Daily", "The Bulwark",
         "https://x/art.jpg", str(art), db.now(), db.now()),
    )
    f2 = db.execute(
        "INSERT INTO feeds (url, title, author, added_at) VALUES (?,?,?,?)",
        ("https://x/b.rss", "No Art Show", "Someone Else", db.now()),
    )
    for i in range(3):
        db.execute(
            "INSERT INTO episodes (feed_id, guid, title, published, duration, state, "
            "file_path, file_size, created_at, downloaded_at) "
            "VALUES (?,?,?,?,?, 'ready', '/tmp/x.mp3', ?, ?, ?)",
            (f1, f"g{i}", f"Bulwark Episode {i}", 1_700_000_000 - i * 100, 1800,
             10_000_000, db.now(), db.now()),
        )
    db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, state, error, attempts, created_at) "
        "VALUES (?,?,?,?, 'error', ?, 1, ?)",
        (f1, "gbad", "Broken Episode", 1_699_000_000, "ffmpeg failed: nope", db.now()),
    )
    # A retired episode must not clutter the show page.
    db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, state, created_at) "
        "VALUES (?,?,?,?, 'skipped', ?)",
        (f1, "gskip", "Retired Episode", 1_698_000_000, db.now()),
    )

    print("-- auth --")
    for path in ("/", f"/feeds/{f1}", f"/artwork/{f1}", "/settings"):
        r = client.get(path, follow_redirects=False)
        check(f"{path} requires login", r.headers.get("location") == "/login", str(r.status_code))

    client.post("/login", data={"username": "admin", "password": "s3cret"})

    print("\n-- library page --")
    page = client.get("/").text
    check("show title listed", "The Bulwark Daily" in page)
    check("publisher listed", "The Bulwark" in page)
    check("second show listed", "No Art Show" in page)
    check("links into the show page", f'href="/feeds/{f1}"' in page)
    check("artwork referenced for the show that has it", f'src="/artwork/{f1}"' in page)
    check("placeholder used for the show without artwork",
          "placeholder" in page)
    check("ready count shown", "3 ready" in page)
    check("failure count shown", "1 failed" in page)
    check("individual episodes are NOT on the library page",
          "Bulwark Episode 0" not in page)

    print("\n-- artwork --")
    r = client.get(f"/artwork/{f1}")
    check("served with an image content-type",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg")
    check("missing artwork 404s", client.get(f"/artwork/{f2}").status_code == 404)

    print("\n-- show detail page --")
    page = client.get(f"/feeds/{f1}").text
    check("show name in the header", "The Bulwark Daily" in page)
    check("publisher in the header", "The Bulwark" in page)
    check("episodes listed here", "Bulwark Episode 0" in page)
    check("failed episode shows its error", "ffmpeg failed: nope" in page)
    check("retired episode hidden", "Retired Episode" not in page)
    check("per-show retry is scoped to this show",
          f'action="/feeds/{f1}/retry-failed"' in page)
    check("back link to the library", 'href="/"' in page)
    check("unknown show redirects out",
          client.get("/feeds/9999", follow_redirects=False).headers.get("location") == "/")

    print("\n-- poll interval setting --")
    check("defaults to 15 minutes", settings.refresh_minutes == 15,
          str(settings.refresh_minutes))
    page = client.get("/settings").text
    check("current value rendered", 'value="15"' in page)

    r = client.post("/settings", data={"refresh_minutes": "30"}, follow_redirects=False)
    check("save redirects", r.status_code == 303)
    check("new value persisted", prefs.refresh_minutes() == 30, str(prefs.refresh_minutes()))
    check("survives a fresh read from the database",
          db.get_setting("refresh_minutes") == "30")
    check("shown on the settings page", 'value="30"' in client.get("/settings").text)
    check("library reflects it", "every 30 min" in client.get("/").text)

    client.post("/settings", data={"refresh_minutes": "1"})
    check("clamped to the minimum", prefs.refresh_minutes() == prefs.MIN_REFRESH,
          str(prefs.refresh_minutes()))
    client.post("/settings", data={"refresh_minutes": "99999"})
    check("clamped to the maximum", prefs.refresh_minutes() == prefs.MAX_REFRESH,
          str(prefs.refresh_minutes()))

    print("\n-- the scheduler actually picked up the change --")
    from server import scheduler as sched
    client.post("/settings", data={"refresh_minutes": "45"})
    job = sched.scheduler.get_job(sched.POLL_JOB)
    check("running job rescheduled without a restart",
          job is not None and int(job.trigger.interval.total_seconds()) == 45 * 60,
          str(job.trigger) if job else "no job")

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
