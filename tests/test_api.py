"""End-to-end test of the HTTP surface via Starlette's TestClient."""
import os
import sys
import pathlib
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update(
    PODCAST_DATA_DIR=TMP,
    PODCAST_ADMIN_USER="admin",
    PODCAST_ADMIN_PASSWORD="s3cret",
    PODCAST_SECRET_KEY="0" * 32,
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from server import db
from server.auth import get_api_token
from server.config import settings
from server.main import app

fails = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


with TestClient(app) as client:
    token = get_api_token()
    auth = {"Authorization": f"Bearer {token}"}

    # Seed a feed with one ready episode and one still pending.
    fid = db.execute("INSERT INTO feeds (url, title, added_at) VALUES (?,?,?)",
                     ("https://x/f.rss", "Test Show", db.now()))
    audio = settings.audio_dir / "1.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    ready = db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, duration, state, "
        "file_path, file_size, created_at, downloaded_at) "
        "VALUES (?,?,?,?,?,'ready',?,?,?,?)",
        (fid, "g1", "Ready Episode", 1_700_000_000, 1800, str(audio), 17, db.now(), db.now()))
    db.execute(
        "INSERT INTO episodes (feed_id, guid, title, published, state, created_at) "
        "VALUES (?,?,?,?,'pending',?)",
        (fid, "g2", "Pending Episode", 1_700_000_100, db.now()))

    print("\n-- auth enforcement --")
    for path in ("/api/v1/ping", "/api/v1/feeds", "/api/v1/episodes", f"/api/v1/media/{ready}"):
        check(f"{path} rejects no token", client.get(path).status_code == 401)
    check("bad token rejected",
          client.get("/api/v1/ping", headers={"Authorization": "Bearer wrong"}).status_code == 401)
    check("valid token accepted", client.get("/api/v1/ping", headers=auth).status_code == 200)

    print("\n-- watch API shape --")
    ping = client.get("/api/v1/ping", headers=auth).json()
    check("ping reports version + ready count", ping["v"] == 1 and ping["eps"] == 1)

    feeds_body = client.get("/api/v1/feeds", headers=auth).json()
    check("feed listed with compact keys",
          feeds_body["feeds"] == [{"i": fid, "t": "Test Show", "n": 1}])

    eps = client.get(f"/api/v1/feeds/{fid}/episodes", headers=auth).json()["eps"]
    check("only ready episodes returned", len(eps) == 1)
    check("episode keys are i/f/t/n/d/s/p", set(eps[0]) == {"i", "f", "t", "n", "d", "s", "p"})
    check("show name included for the watch", eps[0]["n"] == "Test Show")
    check("ref_id matches media path", eps[0]["i"] == ready)
    check("duration carried through", eps[0]["d"] == 1800)

    latest = client.get("/api/v1/episodes", headers=auth).json()["eps"]
    check("latest endpoint returns the ready episode", len(latest) == 1)
    check("limit is clamped", client.get("/api/v1/episodes?limit=999", headers=auth).status_code == 422)
    check("unknown feed 404s", client.get("/api/v1/feeds/9999/episodes", headers=auth).status_code == 404)

    print("\n-- media delivery (Garmin content-type contract) --")
    r = client.get(f"/api/v1/media/{ready}", headers=auth)
    check("200 for ready episode", r.status_code == 200)
    check("Content-Type is exactly audio/mpeg", r.headers["content-type"] == "audio/mpeg")
    check("body is the file", r.content == b"ID3fake-mp3-bytes")
    check("pending episode not downloadable",
          client.get("/api/v1/media/2", headers=auth).status_code == 404)

    audio.unlink()
    check("missing file on disk 404s",
          client.get(f"/api/v1/media/{ready}", headers=auth).status_code == 404)

    print("\n-- web UI session flow --")
    check("dashboard redirects anonymous to /login",
          client.get("/", follow_redirects=False).headers.get("location") == "/login")
    check("wrong password rejected",
          client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401)
    r = client.post("/login", data={"username": "admin", "password": "s3cret"},
                    follow_redirects=False)
    check("correct password sets session", r.status_code == 303)
    check("dashboard renders when logged in", "Test Show" in client.get("/").text)
    check("token page shows the watch token", token in client.get("/token").text)
    client.get("/logout")
    check("logout clears session",
          client.get("/", follow_redirects=False).headers.get("location") == "/login")

    print("\n-- login throttle --")
    for _ in range(6):
        client.post("/login", data={"username": "admin", "password": "nope"})
    check("throttled after repeated failures",
          client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 429)

    check("healthz is open", client.get("/healthz").status_code == 200)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
sys.exit(1 if fails else 0)
