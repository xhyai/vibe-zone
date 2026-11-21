"""Optional categories. They never take part in the main ranking key."""
import hashlib
import json
import math
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote

DEFAULTS = {"badges": True, "hidden": True, "chaos": False, "prompt_logs": False,
            "voting": False, "bonus": False, "ai_pilot": False,
            "smallest": True, "startup": True, "latency": True, "language": True}
LABELS = {"smallest": "Najmniejszy obraz", "startup": "Najszybszy start", "latency": "Najniższe p99",
          "language": "Nietypowy język", "survivor": "Survivor", "bonus": "Bonus", "pilot": "AI Pilot"}
FEATURE_LABELS = {"badges": "Odznaki", "hidden": "Ukryte testy", "chaos": "Chaos round",
                  "prompt_logs": "Prompt logi", "voting": "Głosowanie publiczności", "bonus": "Bonus",
                  "ai_pilot": "Ścieżka AI Pilot", **{k: LABELS[k] for k in ("smallest", "startup", "latency", "language")}}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS extras_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audience_votes (
            device_hash TEXT PRIMARY KEY, participant_id INTEGER NOT NULL REFERENCES participants(id)
        );
        CREATE INDEX IF NOT EXISTS votes_participant ON audience_votes(participant_id);
        CREATE TABLE IF NOT EXISTS pilot_green (
            participant_id INTEGER PRIMARY KEY REFERENCES participants(id),
            submission_id INTEGER NOT NULL REFERENCES submissions(id),
            elapsed_seconds REAL NOT NULL, recorded_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS results_submission ON results(submission_id, batch_id DESC);
        CREATE INDEX IF NOT EXISTS batches_state ON batches(state, id DESC);
    """)
    db.execute("BEGIN IMMEDIATE")
    for table, columns in {
        "submissions": {"language": "TEXT NOT NULL DEFAULT ''", "language_verified": "INTEGER NOT NULL DEFAULT 0",
                        "prompt_log": "TEXT NOT NULL DEFAULT ''", "ai_tool": "TEXT NOT NULL DEFAULT ''"},
        "invitations": {"issued_at": "REAL", "pilot": "INTEGER NOT NULL DEFAULT 0"},
        "participants": {"registered_at": "REAL", "pilot": "INTEGER NOT NULL DEFAULT 0"},
    }.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for name, value in {**DEFAULTS, "vote_state": "closed", "bonus_public": None}.items():
        db.execute("INSERT OR IGNORE INTO extras_settings VALUES (?,?)", (name, json.dumps(value)))


def settings(db):
    return {**DEFAULTS, **{r["name"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM extras_settings")}}


def save(db, name, value):
    db.execute("UPDATE extras_settings SET value=? WHERE name=?", (json.dumps(value, ensure_ascii=False), name))


def metadata(language="", prompt_log="", ai_tool=""):
    values = {}
    for key, value, limit in [("language", language, 40), ("ai_tool", ai_tool, 80)]:
        value = value.strip()
        if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Nieprawidłowa deklaracja języka lub narzędzia AI.")
        values[key] = value
    link = prompt_log.strip()
    if link:
        try:
            parsed = urlsplit(link)
            valid = (parsed.scheme == "https" and parsed.hostname in {"github.com", "gist.github.com", "gitlab.com"}
                     and not parsed.username and not parsed.password and parsed.port in (None, 443)
                     and len(parsed.path.strip("/").split("/")) >= 2 and len(link) <= 500
                     and not any(c.isspace() or ord(c) < 32 or c in '\\<>"' for c in unquote(link)))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Prompt log musi być adresem HTTPS repozytorium lub gista w GitHub albo GitLab.")
    return {**values, "prompt_log": link}


def public_result(value):
    from bench.extras import project
    return project(value)


def attach(rows, config, green):
    for row in rows:
        row["badges"] = []
        row["extras"] = {k: v for k, v in public_result(row["result"].get("extras")).items()
                         if config.get(k if k != "pilot_smoke" else "ai_pilot")}
        if not config["prompt_logs"]:
            row["prompt_log"] = row["ai_tool"] = ""
    if not config["badges"]:
        return
    eligible = [r for r in rows if r["result"].get("status") == "passed"]
    def winners(name, field, candidates, reverse=False):
        if not config.get(name):
            return
        valid = [r for r in candidates if type(r["result"].get(field)) in (int, float)
                 and math.isfinite(r["result"][field]) and r["result"][field] >= 0]
        if valid:
            best = (max if reverse else min)(r["result"][field] for r in valid)
            for row in valid:
                if row["result"][field] == best:
                    row["badges"].append(name)
    winners("smallest", "image_bytes", [r for r in eligible if type(r["result"].get("image_bytes")) in (int, float) and r["result"]["image_bytes"] < 50000000])
    winners("startup", "cold_start_ms", eligible)
    winners("latency", "p99_ms", [r for r in eligible if r["result"].get("max_vus", 0) > 0])
    if config["language"]:
        from leaderboard.store import ranking_key
        unusual = [r for r in eligible if r.get("language_verified") and r.get("language")]
        if unusual:
            best = min(ranking_key(r) for r in unusual)
            for row in unusual:
                if ranking_key(row) == best:
                    row["badges"].append("language")
    for row in eligible:
        for flag, result, badge in [("chaos", "chaos", "survivor"), ("bonus", "bonus", "bonus")]:
            if config[flag] and row["extras"].get(result, {}).get("status") == "passed":
                row["badges"].append(badge)
    for row in rows:
        if config["ai_pilot"] and row["participant_id"] in green:
            row["badges"].append("pilot")


def record_green(db, submission, result, enabled):
    value = public_result(result.get("extras"))
    smoke_passed = (result.get("status") == "passed" and value.get("hidden", {}).get("status") == "passed") if result.get("profile", {}).get("name") == "smoke" else value.get("pilot_smoke", {}).get("status") == "passed"
    if not enabled or not smoke_passed:
        return
    team = db.execute("SELECT pilot,registered_at FROM participants WHERE id=?", (submission["participant_id"],)).fetchone()
    if team and team["pilot"] and team["registered_at"] is not None:
        elapsed = max(0, submission["created_at"] - team["registered_at"])
        db.execute("""INSERT INTO pilot_green VALUES (?,?,?,?) ON CONFLICT(participant_id) DO UPDATE SET
            submission_id=excluded.submission_id,elapsed_seconds=excluded.elapsed_seconds,recorded_at=excluded.recorded_at
            WHERE excluded.elapsed_seconds < pilot_green.elapsed_seconds
            OR (SELECT hidden FROM submissions WHERE id=pilot_green.submission_id)=1""",
            (submission["participant_id"], submission["id"], elapsed, time.time()))


def public_state(db):
    config = settings(db)
    pilot = []
    if config["ai_pilot"]:
        pilot = [dict(r) for r in db.execute("""SELECT p.id,p.nickname,g.elapsed_seconds,g.submission_id
            FROM pilot_green g JOIN participants p ON p.id=g.participant_id
            JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE participant_id=p.id)
            JOIN submissions winning ON winning.id=g.submission_id
            WHERE s.hidden=0 AND winning.hidden=0 ORDER BY g.elapsed_seconds,p.id""")]
    return {"features": {k: config[k] for k in DEFAULTS}, "pilot": pilot,
            "bonus": config["bonus_public"] if config["bonus"] else None,
            "vote_state": config["vote_state"] if config["voting"] else "disabled"}


def announce(db, path):
    config = settings(db)
    if not config["bonus"]:
        raise ValueError("Najpierw włącz bonus.")
    if config["bonus_public"]:
        return
    raw = Path(path).read_bytes()
    if len(raw) > 16000:
        raise ValueError("Nieprawidłowy plik bonusu.")
    value = json.loads(raw)
    if set(value) != {"pl", "en"} or not all(isinstance(v, str) and 1 <= len(v) <= 7000 for v in value.values()):
        raise ValueError("Nieprawidłowy plik bonusu.")
    save(db, "bonus_public", {**value, "announced_at": time.time()})


def cast_vote(db, device, participant_id):
    db.execute("BEGIN IMMEDIATE")
    config = settings(db)
    if not config["voting"] or config["vote_state"] != "open":
        raise ValueError("Głosowanie jest zamknięte.")
    if not db.execute("""SELECT 1 FROM submissions s WHERE participant_id=? AND hidden=0
        AND id=(SELECT MAX(id) FROM submissions WHERE participant_id=?)""", (participant_id, participant_id)).fetchone():
        raise ValueError("Nie znaleziono zespołu.")
    if db.execute("SELECT COUNT(*) FROM audience_votes").fetchone()[0] >= 10000:
        raise ValueError("Limit głosów został osiągnięty.")
    key = hashlib.sha256(device.encode()).hexdigest()
    if db.execute("SELECT 1 FROM audience_votes WHERE device_hash=?", (key,)).fetchone():
        raise ValueError("To urządzenie już zagłosowało.")
    db.execute("INSERT INTO audience_votes VALUES (?,?)", (key, participant_id))
