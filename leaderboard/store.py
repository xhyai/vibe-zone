import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from leaderboard import extras, charts, remote

MAX_TEAMS = 200
MAX_SUBMISSIONS = 5000
MAX_TEAM_SUBMISSIONS = 100


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY, nickname TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL, token_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, participant_id INTEGER NOT NULL REFERENCES participants(id),
                source TEXT NOT NULL, created_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS submission_participant ON submissions(participant_id, id DESC);
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS results (
                batch_id INTEGER NOT NULL REFERENCES batches(id), submission_id INTEGER NOT NULL REFERENCES submissions(id),
                document TEXT NOT NULL, PRIMARY KEY(batch_id, submission_id)
            );
            CREATE TABLE IF NOT EXISTS control (
                id INTEGER PRIMARY KEY CHECK(id=1), next_run REAL NOT NULL, requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                token_hash TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0
            );
        """)
        remote.initialize(db)
        db.execute("BEGIN IMMEDIATE")
        if "approved_image" not in {row[1] for row in db.execute("PRAGMA table_info(submissions)")}:
            db.execute("ALTER TABLE submissions ADD COLUMN approved_image TEXT")
        next_run = (int(time.time()) // 900 + 1) * 900
        db.execute("INSERT OR IGNORE INTO control(id,next_run) VALUES (1,?)", (next_run,))
        extras.initialize(db)


def allow_request(path, bucket, limit, window_seconds=60, now=None):
    now = time.time() if now is None else now
    window = int(now) // window_seconds
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT window,count FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()
        count = row["count"] if row and row["window"] == window else 0
        if count >= limit:
            return False
        # Fixed buckets only: no attacker-controlled high-cardinality keys.
        db.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,?,?)", (bucket, window, count + 1))
        return True


def issue_invitation(path, pilot=False):
    token = secrets.token_urlsafe(24)
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] >= MAX_TEAMS:
            raise ValueError("Wydano już 200 zaproszeń. Kolejne wymagają przeglądu przez operatora.")
        pilot = bool(pilot and extras.settings(db)["ai_pilot"])
        db.execute("INSERT INTO invitations(token_hash,issued_at,pilot) VALUES (?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), time.time(), int(pilot)))
    return token


def submit(path, nickname, source, token="", invitation="", metadata=None):
    metadata = extras.metadata(**(metadata or {}))
    nickname = " ".join(nickname.split())
    if not 1 <= len(nickname) <= 48 or any(ord(char) < 32 for char in nickname):
        raise ValueError("Podaj nazwę zespołu (1-48 znaków).")
    new_token = None
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] >= MAX_SUBMISSIONS:
            raise ValueError("Limit zgłoszeń wydarzenia został osiągnięty. Skontaktuj się z organizatorem.")
        participant = db.execute("SELECT * FROM participants WHERE name_key=?", (nickname.casefold(),)).fetchone()
        if participant:
            actual = hashlib.sha256(token.encode()).hexdigest()
            if not hmac.compare_digest(actual, participant["token_hash"]):
                raise ValueError("Ta nazwa jest zajęta. Aby zaktualizować zgłoszenie, podaj kod zespołu.")
            participant_id = participant["id"]
            if db.execute("SELECT COUNT(*) FROM submissions WHERE participant_id=?", (participant_id,)).fetchone()[0] >= MAX_TEAM_SUBMISSIONS:
                raise ValueError("Zespół osiągnął limit 100 wersji zgłoszenia.")
        else:
            invite_hash = hashlib.sha256(invitation.encode()).hexdigest()
            registration = db.execute("SELECT issued_at,pilot FROM invitations WHERE token_hash=?", (invite_hash,)).fetchone()
            if not db.execute("UPDATE invitations SET used=1 WHERE token_hash=? AND used=0", (invite_hash,)).rowcount:
                raise ValueError("Pierwsze zgłoszenie wymaga jednorazowego zaproszenia od organizatora.")
            if db.execute("SELECT COUNT(*) FROM participants").fetchone()[0] >= MAX_TEAMS:
                raise ValueError("Limit 200 zespołów został osiągnięty. Skontaktuj się z organizatorem.")
            new_token = secrets.token_urlsafe(24)
            participant_id = db.execute("INSERT INTO participants(nickname,name_key,token_hash) VALUES (?,?,?)",
                (nickname, nickname.casefold(), hashlib.sha256(new_token.encode()).hexdigest())).lastrowid
            db.execute("UPDATE participants SET registered_at=?,pilot=? WHERE id=?",
                       (registration["issued_at"], registration["pilot"], participant_id))
        submission_id = db.execute("INSERT INTO submissions(participant_id,source,created_at) VALUES (?,?,?)",
                                   (participant_id, source, time.time())).lastrowid
        db.execute("UPDATE submissions SET language=?,prompt_log=?,ai_tool=? WHERE id=?",
                   (metadata["language"], metadata["prompt_log"], metadata["ai_tool"], submission_id))
    return submission_id, new_token


def latest(db):
    # Filter hidden AFTER choosing the newest, so hiding never revives an old submission.
    return db.execute("""SELECT s.*, p.nickname, p.pilot FROM submissions s JOIN participants p ON p.id=s.participant_id
        WHERE s.id=(SELECT MAX(s2.id) FROM submissions s2 WHERE s2.participant_id=s.participant_id)
        AND s.hidden=0 ORDER BY p.id""").fetchall()


def ranking_key(row):
    result = row["result"]
    return (-result.get("max_vus", 0), result.get("image_bytes", float("inf")),
            result.get("cold_start_ms", float("inf")), result.get("p99_ms") if result.get("p99_ms") is not None else float("inf"))


def measurement_stats(result):
    """Describe recorded samples only; never substitute a zero for missing data."""
    repetitions = result.get("repetitions") or []
    vus = [r["max_vus"] for r in repetitions if r.get("max_vus") is not None]
    starts = result.get("cold_start_samples_ms") or []
    return {"measurements": len(repetitions), "vus_min": min(vus) if vus else None,
            "vus_max": max(vus) if vus else None, "startup_samples": len(starts),
            "startup_min_ms": min(starts) if starts else None,
            "startup_max_ms": max(starts) if starts else None}


def team_history(path, participant_id):
    with connect(path) as db:
        # The same hide rule as the public ranking: never revive a hidden team.
        team = db.execute("""SELECT p.id,p.nickname,s.language,s.language_verified,s.prompt_log,s.ai_tool FROM participants p JOIN submissions s
            ON s.participant_id=p.id WHERE p.id=? AND s.hidden=0
            AND s.id=(SELECT MAX(id) FROM submissions WHERE participant_id=p.id)""", (participant_id,)).fetchone()
        if not team:
            return None
        config = extras.settings(db)
        if not config["prompt_logs"]:
            team = {**dict(team), "prompt_log": "", "ai_tool": ""}
        history = []
        for row in db.execute("""SELECT b.id,b.finished_at,s.id AS submission_id,s.source,r.document
            FROM results r JOIN batches b ON b.id=r.batch_id
            JOIN submissions s ON s.id=r.submission_id
            WHERE s.participant_id=? AND s.hidden=0 AND b.state='published'
            ORDER BY b.id DESC LIMIT 64""", (participant_id,)):
            result = json.loads(row["document"])
            # Explicit public projection: no diagnostics, invitations or ownership hashes.
            public = {key: result.get(key) for key in ("status", "max_vus", "image_bytes", "cold_start_ms",
                "p95_ms", "p99_ms", "throughput_rps", "error_percent", "profile_hash", "finished_at")}
            public["extras"] = extras.public_result(result.get("extras"))
            public["profile_name"] = (result.get("profile") or {}).get("name")
            history.append({"batch_id": row["id"], "published_at": row["finished_at"],
                            "submission_id": row["submission_id"], "source": row["source"],
                            "result": public, "chart": charts.result_chart(result), "stats": measurement_stats(result)})
    # Plot only the uninterrupted suffix of successful, comparable measurements.
    trend = []
    for row in history:
        result = row["result"]
        if (result["status"] != "passed" or not result["profile_hash"] or result["max_vus"] is None
                or (trend and result["profile_hash"] != trend[0]["result"]["profile_hash"])):
            break
        trend.append(row)
    values = [r["result"]["max_vus"] for r in reversed(trend)]
    maximum = max(values, default=1) or 1
    points = " ".join(f"{8 + i * 304 / max(len(values) - 1, 1):.1f},{72 - value * 64 / maximum:.1f}"
                      for i, value in enumerate(values))
    return {**dict(team), "history": history, "trend_points": points,
            "trend_values": values, "trend_count": len(values)}


def board(path, profile_hash=None):
    with connect(path) as db:
        available_profiles = [dict(row) for row in db.execute("""SELECT rb.profile_hash,rb.config,MAX(b.id) AS batch_id
            FROM remote_batches rb JOIN batches b ON b.id=rb.batch_id WHERE b.state='published'
            GROUP BY rb.profile_hash ORDER BY batch_id DESC LIMIT 64""")]
        for item in available_profiles:
            config = json.loads(item.pop('config'))
            item['name'] = config['name'] + ' / ' + config['runner_role']
        if profile_hash:
            published = db.execute("""SELECT b.* FROM batches b JOIN remote_batches rb ON rb.batch_id=b.id
                WHERE b.state='published' AND rb.profile_hash=? ORDER BY b.id DESC LIMIT 1""", (profile_hash,)).fetchone()
        else:
            published = db.execute("SELECT * FROM batches WHERE state='published' ORDER BY id DESC LIMIT 1").fetchone()
        running = db.execute("SELECT id,started_at FROM batches WHERE state='running' ORDER BY id DESC LIMIT 1").fetchone()
        control = dict(db.execute("SELECT * FROM control WHERE id=1").fetchone())
        submissions = [dict(row) for row in db.execute("""SELECT s.id,s.participant_id,s.source,s.created_at,s.approved_image,p.nickname,
            s.id=(SELECT MAX(id) FROM submissions WHERE participant_id=s.participant_id) AS current
            FROM submissions s JOIN participants p ON p.id=s.participant_id
            WHERE s.hidden=0 ORDER BY s.id DESC LIMIT 100""")]
        rows = []
        extra_state = extras.public_state(db)
        config = extras.settings(db)
        green = {r["id"] for r in extra_state["pilot"]}
        summary = dict(db.execute("""SELECT COUNT(DISTINCT participant_id) AS teams, COUNT(*) AS submissions
            FROM submissions WHERE hidden=0 AND participant_id NOT IN (
                SELECT participant_id FROM submissions s WHERE hidden=1
                AND id=(SELECT MAX(id) FROM submissions WHERE participant_id=s.participant_id))""").fetchone())
        summary["batches"] = db.execute("SELECT COUNT(*) FROM batches WHERE state='published'").fetchone()[0]
        if published:
            for row in db.execute("""SELECT r.document,s.id,s.participant_id,s.source,s.language,s.language_verified,s.prompt_log,s.ai_tool,p.nickname FROM results r
                JOIN submissions s ON s.id=r.submission_id JOIN participants p ON p.id=s.participant_id
                WHERE r.batch_id=? AND s.hidden=0 AND NOT EXISTS (
                  SELECT 1 FROM submissions newer WHERE newer.participant_id=s.participant_id AND newer.hidden=1
                    AND newer.id=(SELECT MAX(id) FROM submissions WHERE participant_id=s.participant_id))""", (published["id"],)):
                result = json.loads(row["document"])
                rows.append({"submission_id": row["id"], "participant_id": row["participant_id"],
                             "source": row["source"], "nickname": row["nickname"],
                             **{key: row[key] for key in ("language", "language_verified", "prompt_log", "ai_tool")},
                             "result": result, "stats": measurement_stats(result)})
    # A legacy or malformed batch must never rank incomparable measurements together.
    selected_profile = profile_hash or next((r['result'].get('profile_hash') for r in rows), None)
    rows = [r for r in rows if r['result'].get('profile_hash') == selected_profile]
    extras.attach(rows, config, green)
    qualified = sorted([row for row in rows if row["result"].get("status") == "passed"],
                       key=lambda row: (ranking_key(row), row["submission_id"]))
    for index, row in enumerate(qualified):
        row["rank"] = qualified[index - 1]["rank"] if index and ranking_key(row) == ranking_key(qualified[index - 1]) else index + 1
    rejected = [row for row in rows if row["result"].get("status") != "passed"]
    summary["best_vus"] = qualified[0]["result"].get("max_vus") if qualified else None
    return {"profiles": available_profiles, "selected_profile": selected_profile, "extras": extra_state, "ranking": qualified, "rejected": rejected, "submissions": submissions, "summary": summary,
            "last_update": published["finished_at"] if published else None,
            "next_update": control["next_run"], "requested": bool(control["requested"]),
            "running": dict(running) if running else None}
