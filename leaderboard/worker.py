"""Single sequential scheduler. Run separately from the web app, with Docker access."""
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from bench import runner
from leaderboard import store, extras

DATABASE = os.environ.get("DATABASE", "/data/leaderboard.sqlite3")
RESULTS = os.environ.get("RESULTS_DIR", "/results")
INTERVAL = 900


def claim(path, now=None):
    if os.environ.get("VZ_REMOTE_RUNNER") == "1":
        return None
    now = now or time.time()
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        control = db.execute("SELECT * FROM control WHERE id=1").fetchone()
        if db.execute("SELECT 1 FROM batches WHERE state='running'").fetchone():
            return None
        if not control["requested"] and control["next_run"] > now:
            return None
        batch_id = db.execute("INSERT INTO batches(started_at,state) VALUES (?,'running')", (now,)).lastrowid
        db.execute("UPDATE control SET requested=0,next_run=? WHERE id=1", ((int(now) // INTERVAL + 1) * INTERVAL,))
        return batch_id, [dict(row) for row in store.latest(db)]


def prune_reports(directory, keep=1000):
    pattern = re.compile(r"\d{8}T\d{6}-[a-f0-9]{8}(?:-median|-k6-\d+)?\.json\Z")
    files = sorted((p for p in Path(directory).glob("*.json") if pattern.fullmatch(p.name)), key=lambda p: p.name)
    for path in files[:-keep]:
        if path.is_file() and not path.is_symlink():
            path.unlink()


def run_batch(path, batch_id, submissions, benchmark=None, resolve=None, smoke=False):
    config = runner.profile(smoke)
    profile_hash = runner.fingerprint(config)
    resolve = resolve or (lambda source: runner.resolve_source(source, "never"))
    batch_seed = secrets.token_hex(32)
    with store.connect(path) as db:
        extra_config = extras.settings(db)
    flags = [name for name in ("hidden", "chaos", "bonus") if extra_config[name]
             and (name != "bonus" or extra_config["bonus_public"])]

    def execute(resolved, smoke_run=smoke, extra_flags=None):
        args = [sys.executable, str(runner.ROOT / "runner.py"), resolved["image_id"], "--pull", "never", "--results", RESULTS]
        if smoke_run:
            args.append("--smoke")
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "BENCH_SEED": batch_seed, "VZ_EXTRAS_FLAGS": " ".join(flags if extra_flags is None else extra_flags)})
        try:
            stdout, stderr = process.communicate(timeout=config["max_run_seconds"] * 3 + 180)
        except BaseException:
            process.terminate()  # runner's SIGTERM handler removes its containers and writes a failed report
            try:
                process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        lines = stdout.strip().splitlines()
        if not lines:
            raise RuntimeError("Runner did not produce a report: " + stderr[-1000:])
        report = Path(lines[-1]).resolve()
        if report.parent != Path(RESULTS).resolve() or report.stat().st_size > 1024 * 1024:
            raise RuntimeError("Invalid runner report path or size")
        document = json.loads(report.read_text())
        if process.returncode and document.get("status") == "passed":
            raise RuntimeError("Runner exit status disagrees with report")
        prune_reports(RESULTS)
        return document

    default_benchmark = benchmark is None
    benchmark = benchmark or execute
    pilot_cache = {}
    try:
        # Only admin-approved local IDs cross the Docker authority boundary.
        snapshots = []
        for submission in submissions:
            try:
                if not submission.get("approved_image"):
                    snapshots.append((submission, None, "Oczekuje na zatwierdzenie obrazu przez organizatora."))
                    continue
                if not runner.IMAGE_ID.fullmatch(submission["approved_image"]):
                    raise ValueError("Invalid approved immutable image ID")
                allowed = os.environ.get("BENCH_ALLOWED_IMAGE_IDS", "").split()
                if allowed and submission["approved_image"] not in allowed:
                    raise ValueError("Ten serwer testowy uruchamia tylko zatwierdzony starter organizatora.")
                resolved = resolve(submission["approved_image"])
                resolved["source"] = submission["source"]
                snapshots.append((submission, resolved, None))
            except Exception as error:
                snapshots.append((submission, None, str(error)))
        documents = []
        batch_cache = {}
        for submission, resolved, error in snapshots:
            try:
                if error:
                    raise RuntimeError(error)
                result = batch_cache.get(resolved["image_id"])
                if result:
                    result = {**result, "reused": True, "reused_from": result.get("id")}
                else:
                    result = benchmark(resolved)
                    if result.get("cleanup_errors") or result.get("error_kind") == "infrastructure":
                        raise InterruptedError("Infrastructure invalid; publication stopped: " + result.get("error", "cleanup incomplete"))
                    if result.get("status") == "passed":
                        batch_cache[resolved["image_id"]] = dict(result)
                result = dict(result)
                if extra_config["ai_pilot"] and submission.get("pilot") and not smoke and default_benchmark:
                    if resolved["image_id"] not in pilot_cache:
                        pilot_result = execute(resolved, smoke_run=True, extra_flags=["hidden"])
                        if pilot_result.get("error_kind") == "infrastructure" or pilot_result.get("cleanup_errors"):
                            raise InterruptedError("Pilot smoke infrastructure invalid")
                        pilot_cache[resolved["image_id"]] = (pilot_result.get("status") == "passed" and
                            extras.public_result(pilot_result.get("extras")).get("hidden", {}).get("status") == "passed")
                    result["extras"] = {**extras.public_result(result.get("extras")),
                        "pilot_smoke": {"status": "passed" if pilot_cache[resolved["image_id"]] else "failed"}}
                result.update(resolved)
                result["profile_hash"] = profile_hash
            except InterruptedError:
                raise
            except Exception as failure:
                result = {"status": "failed", "error": str(failure)[-2000:], "max_vus": 0, "profile_hash": profile_hash}
            documents.append((batch_id, submission["id"], json.dumps(result, allow_nan=False)))
        with store.connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            for submission, (_, _, document) in zip(submissions, documents):
                extras.record_green(db, submission, json.loads(document), extra_config["ai_pilot"])
            db.executemany("INSERT INTO results(batch_id,submission_id,document) VALUES (?,?,?)", documents)
            db.execute("UPDATE batches SET state='published',finished_at=? WHERE id=?", (time.time(), batch_id))
            # A long batch skips missed quarter-hours instead of creating an endless queue.
            db.execute("UPDATE control SET next_run=? WHERE id=1", ((int(time.time()) // INTERVAL + 1) * INTERVAL,))
            old = "SELECT id FROM batches WHERE state!='running' ORDER BY id DESC LIMIT -1 OFFSET 64"
            db.execute(f"DELETE FROM results WHERE batch_id IN ({old})")
            db.execute(f"DELETE FROM batches WHERE id IN ({old})")
    except BaseException as error:
        with store.connect(path) as db:
            db.execute("UPDATE batches SET state='failed',finished_at=?,error=? WHERE id=?", (time.time(), str(error)[-2000:], batch_id))
        raise


def main():
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt("Scheduler stopping")
    signal.signal(signal.SIGTERM, interrupt)
    store.initialize(DATABASE)
    # OS releases this lock after crashes; multiple workers cannot publish overlapping batches.
    with open(DATABASE + ".worker.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.connect(DATABASE) as db:
            db.execute("UPDATE batches SET state='failed',finished_at=?,error='Scheduler restarted during batch' WHERE state='running'", (time.time(),))
        while True:
            work = claim(DATABASE)
            if work:
                print(f"Starting batch {work[0]} ({len(work[1])} submissions)", flush=True)
                run_batch(DATABASE, *work, smoke=os.environ.get("BENCH_SMOKE", "0") == "1")
            time.sleep(2)


if __name__ == "__main__":
    main()
