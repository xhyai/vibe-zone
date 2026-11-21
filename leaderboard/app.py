import hmac
import os
import secrets
import time
from functools import wraps
from pathlib import Path
from itsdangerous import URLSafeTimedSerializer, BadSignature
import hashlib
import re

from flask import Flask, g, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.datastructures import LanguageAccept

from bench.runner import IMAGE_ID, validate_source
from leaderboard import store, extras, charts, remote, runner_api
from datetime import datetime
from zoneinfo import ZoneInfo
from leaderboard.i18n import ENGLISH, translate, public_error


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY"), ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD"),
                      DATABASE=os.environ.get("DATABASE", "/data/leaderboard.sqlite3"),
                      REMOTE_RUNNER=os.environ.get("VZ_REMOTE_RUNNER", "0") == "1",
                      RUNNER_TOKEN=os.environ.get("RUNNER_TOKEN", ""),
                      BENCH_SMOKE=os.environ.get("BENCH_SMOKE", "0") == "1",
                      ALLOWED_IMAGE_IDS=os.environ.get("BENCH_ALLOWED_IMAGE_IDS", "").split(),
                      BONUS_SPEC_FILE=os.environ.get("BONUS_SPEC_FILE", "/run/vibezone/bonus.json"),
                      MAX_CONTENT_LENGTH=16 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1")
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"] or not app.config["ADMIN_PASSWORD"]:
        raise RuntimeError("Set SECRET_KEY and ADMIN_PASSWORD before starting the leaderboard")
    if not app.testing and (len(app.config["SECRET_KEY"]) < 32 or len(app.config["ADMIN_PASSWORD"]) < 20
            or any(str(app.config[key]).startswith("replace-with-") for key in ("SECRET_KEY", "ADMIN_PASSWORD"))):
        raise RuntimeError("Use a random SECRET_KEY (32+ chars) and ADMIN_PASSWORD (20+ chars), not example values")
    if app.config["REMOTE_RUNNER"] and len(app.config["RUNNER_TOKEN"]) < 32:
        raise RuntimeError("Remote runner requires a random RUNNER_TOKEN of at least 32 characters")
    store.initialize(app.config["DATABASE"])

    asset_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(app.static_folder).glob("*")) if p.is_file())).hexdigest()[:12]
    javascript = Path(app.static_folder, "board.js").read_text()
    live_keys = set(re.findall(r"t\(['\"]([^'\"]+)['\"]\)", javascript)) | set(extras.LABELS.values())
    devices = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="audience-device")

    @app.template_filter("warsaw_time")
    def warsaw_time(value):
        return datetime.fromtimestamp(value, ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y %H:%M:%S") if value else translate("jeszcze brak")

    @app.context_processor
    def csrf_context():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        banner = app.config["BENCH_SMOKE"]
        if app.config["REMOTE_RUNNER"]:
            with store.connect(app.config["DATABASE"]) as db:
                banner = remote.public_state(db)["test_banner"]
        return {"csrf_token": session["csrf"], "lang": g.lang, "t": translate,
                "js_messages": {k: ENGLISH[k] for k in live_keys if k in ENGLISH} if g.lang == "en" else {},
                "badge_labels": extras.LABELS, "asset_hash": asset_hash, "smoke": banner, "remote_enabled": app.config["REMOTE_RUNNER"],
                "language_path": request.path if request.method == "GET" else "/"}

    @app.before_request
    def choose_language():
        if request.endpoint == "static":
            g.lang = "en"
            return
        selected = request.args.get("lang")
        if selected in ("pl", "en"):
            session["lang"] = selected
        preferences = LanguageAccept([(tag.split("-")[0], quality) for tag, quality in request.accept_languages])
        g.lang = session.get("lang") or preferences.best_match(["en", "pl"], default="en")

    def localized_board():
        selected = request.args.get("profile")
        if selected and not remote.DIGEST.fullmatch(selected):
            abort(400)
        board = store.board(app.config["DATABASE"], selected)
        if app.config["REMOTE_RUNNER"]:
            with store.connect(app.config["DATABASE"]) as db:
                board["runner"] = remote.public_state(db)
        for row in board["ranking"] + board["rejected"]:
            original = row["result"]
            row["result"] = {key: original[key] for key in ("id", "status", "max_vus", "image_bytes", "cold_start_ms",
                "p95_ms", "p99_ms", "throughput_rps", "error_percent", "profile", "profile_hash", "finished_at", "error",
                "reused", "reused_from") if key in original}
            row["result"]["repetitions"] = [{key: r.get(key) for key in ("id", "max_vus")} for r in original.get("repetitions", [])]
        for row in board["rejected"]:
            row["result"]["error"] = public_error(row["result"].get("error", ""))
        return board

    @app.before_request
    def csrf_check():
        if request.endpoint == "runner_api.operation":
            request.max_content_length = 1024 * 1024
            return
        if request.method == "POST":
            expected, supplied = session.get("csrf"), request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                abort(400, "Sesja formularza wygasła. Odśwież stronę i spróbuj ponownie.")
        if request.endpoint == "submit" and not store.allow_request(app.config["DATABASE"], "submissions", 30):
            abort(429, "Limit 30 zgłoszeń na minutę. Spróbuj ponownie za minutę.")

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        cacheable_asset = request.endpoint == "static" and response.status_code in (200, 304) and request.args.get("v") == asset_hash
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if cacheable_asset else "no-store, no-transform"
        response.headers["Content-Language"] = g.lang
        response.vary.add("Accept-Language")
        if not cacheable_asset:
            response.vary.add("Cookie")
        return response

    def admin_required(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if session.get("admin_until", 0) < time.time() and not store.allow_request(app.config["DATABASE"], "admin-attempts", 20):
                abort(429, "Zbyt wiele prób logowania. Spróbuj ponownie za minutę.")
            auth = request.authorization
            valid = auth and auth.username == "admin" and hmac.compare_digest((auth.password or "").encode(), app.config["ADMIN_PASSWORD"].encode())
            if not valid:
                return translate("Wymagane hasło administratora."), 401, {"WWW-Authenticate": 'Basic realm="Vibe Zone admin", charset="UTF-8"'}
            session["admin_until"] = time.time() + 300
            return fn(*args, **kwargs)
        return decorated

    runner_api.install(app, admin_required)

    @app.get("/health")
    def health():
        with store.connect(app.config["DATABASE"]) as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.get("/")
    def index():
        from leaderboard.content import GLOSSARY
        return render_template("home.html", board=localized_board(), compact=True, limit=5, chart=charts.EXAMPLE, glossary=GLOSSARY)

    @app.get("/leaderboard")
    def leaderboard():
        return render_template("index.html", board=localized_board(), kiosk=False)

    @app.get("/submit")
    def submit_form():
        return render_template("submit.html", board=localized_board())

    @app.get("/submissions")
    def legacy_submissions():
        return redirect(url_for("submit_form", lang=g.lang), code=308)

    @app.get("/start")
    def start():
        from leaderboard.content import GUIDE
        with store.connect(app.config["DATABASE"]) as db:
            state = extras.public_state(db)
        return render_template("start.html", guide=GUIDE, extras_state=state)

    @app.get("/team/<int:participant_id>")
    def team(participant_id):
        detail = store.team_history(app.config["DATABASE"], participant_id)
        if detail is None:
            abort(404)
        board = localized_board()
        current = next((r for r in board["ranking"] + board["rejected"] if r["participant_id"] == participant_id), None)
        return render_template("team.html", team=detail, current=current, extras_state=board["extras"])

    @app.get("/screen")
    def screen():
        return render_template("index.html", board=localized_board(), kiosk=True)

    @app.get("/api/leaderboard")
    def api_board():
        return jsonify(localized_board())

    @app.post("/submissions")
    def submit():
        source = request.form.get("source", "").strip()
        try:
            validate_source(source)
            submission_id, token = store.submit(app.config["DATABASE"], request.form.get("nickname", ""), source,
                                                 request.form.get("team_token", ""), request.form.get("invite_code", ""),
                                                 metadata={key: request.form.get(key, "") for key in ("language", "prompt_log", "ai_tool")})
        except ValueError as error:
            return render_template("submit.html", board=localized_board(),
                                   error=translate(str(error)), values=request.form), 400
        return render_template("submitted.html", submission_id=submission_id, token=token), 201

    @app.get("/admin")
    @admin_required
    def admin():
        with store.connect(app.config["DATABASE"]) as db:
            submissions = db.execute("SELECT s.*,p.nickname FROM submissions s JOIN participants p ON p.id=s.participant_id WHERE s.id=(SELECT MAX(id) FROM submissions WHERE participant_id=s.participant_id) ORDER BY p.id LIMIT 200").fetchall()
            batches = db.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 10").fetchall()
            config = extras.settings(db)
            runners = [dict(row) for row in db.execute('SELECT * FROM runners ORDER BY address')]
            selection = dict(db.execute('SELECT * FROM runner_control WHERE id=1').fetchone())
            jobs = db.execute('SELECT id,batch_id,submission_id,state,attempt,phase,lease_until FROM jobs ORDER BY id DESC LIMIT 100').fetchall()
        return render_template("admin.html", submissions=submissions, batches=batches, extra_config=config,
                               feature_labels=extras.FEATURE_LABELS, runners=runners, selection=selection, jobs=jobs)

    @app.post("/admin/invitations")
    @admin_required
    def invitation():
        try:
            token = store.issue_invitation(app.config["DATABASE"], pilot=request.form.get("pilot") == "1")
        except ValueError as error:
            abort(400, str(error))
        return render_template("invitation.html", token=token), 201

    @app.post("/admin/run")
    @admin_required
    def run_batch():
        with store.connect(app.config["DATABASE"]) as db:
            db.execute("UPDATE control SET requested=1 WHERE id=1")
        return redirect(url_for("admin"), code=303)

    @app.post("/admin/submissions/<int:submission_id>/hide")
    @admin_required
    def hide(submission_id):
        with store.connect(app.config["DATABASE"]) as db:
            if not db.execute("UPDATE submissions SET hidden=1 WHERE id=?", (submission_id,)).rowcount:
                abort(404)
        return redirect(url_for("admin"), code=303)

    @app.post("/admin/submissions/<int:submission_id>/approve")
    @admin_required
    def approve(submission_id):
        image_id = request.form.get("image_id", "").strip()
        if not IMAGE_ID.fullmatch(image_id):
            abort(400, "Podaj pełne lokalne ID obrazu: sha256 i 64 znaki hex.")
        if app.config["ALLOWED_IMAGE_IDS"] and image_id not in app.config["ALLOWED_IMAGE_IDS"]:
            abort(400, "Ten serwer testowy uruchamia tylko zatwierdzony starter organizatora.")
        with store.connect(app.config["DATABASE"]) as db:
            if not db.execute("UPDATE submissions SET approved_image=? WHERE id=? AND hidden=0", (image_id, submission_id)).rowcount:
                abort(404)
        return redirect(url_for("admin"), code=303)

    @app.get("/robots.txt")
    def robots():
        return "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\nDisallow: /vote\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.post("/admin/extras")
    @admin_required
    def configure_extras():
        if request.form.get("ai_pilot") == "1" and request.form.get("hidden") != "1":
            abort(400, "AI Pilot wymaga włączenia ukrytych testów.")
        with store.connect(app.config["DATABASE"]) as db:
            db.execute("BEGIN IMMEDIATE")
            for name in extras.DEFAULTS:
                extras.save(db, name, request.form.get(name) == "1")
            if request.form.get("voting") != "1":
                extras.save(db, "vote_state", "closed")
        return redirect(url_for("admin"), code=303)

    @app.post("/admin/bonus/announce")
    @admin_required
    def announce_bonus():
        try:
            with store.connect(app.config["DATABASE"]) as db:
                db.execute("BEGIN IMMEDIATE")
                extras.announce(db, app.config["BONUS_SPEC_FILE"])
        except (ValueError, OSError):
            abort(400, "Nie można ogłosić bonusu. Sprawdź ustawienia i prywatny plik.")
        return redirect(url_for("admin"), code=303)

    @app.post("/admin/submissions/<int:submission_id>/language")
    @admin_required
    def verify_language(submission_id):
        with store.connect(app.config["DATABASE"]) as db:
            if not db.execute("UPDATE submissions SET language_verified=? WHERE id=? AND hidden=0 AND language!=''",
                              (int(request.form.get("verified") == "1"), submission_id)).rowcount:
                abort(404)
        return redirect(url_for("admin"), code=303)

    @app.post("/admin/voting")
    @admin_required
    def voting_state():
        state = request.form.get("state")
        if state not in {"open", "closed"}:
            abort(400)
        with store.connect(app.config["DATABASE"]) as db:
            db.execute("BEGIN IMMEDIATE")
            if not extras.settings(db)["voting"]:
                abort(400)
            extras.save(db, "vote_state", state)
        return redirect(url_for("admin"), code=303)

    def device_id():
        try:
            value = devices.loads(request.cookies.get("vz_device", ""), max_age=365 * 86400)
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else None
        except BadSignature:
            return None

    @app.route("/vote", methods=["GET", "POST"])
    def vote():
        device = device_id()
        if request.method == "POST":
            if not device:
                abort(400, "Włącz cookies i otwórz formularz ponownie.")
            if not store.allow_request(app.config["DATABASE"], "votes", 120):
                abort(429, "Zbyt wiele głosów. Spróbuj za minutę.")
            try:
                participant_id = int(request.form.get("participant_id", ""))
                with store.connect(app.config["DATABASE"]) as db:
                    extras.cast_vote(db, device, participant_id)
            except (ValueError, OverflowError) as error:
                abort(400, str(error))
            return redirect(url_for("vote", lang=g.lang), code=303)
        with store.connect(app.config["DATABASE"]) as db:
            state = extras.public_state(db)
            teams = [dict(row) for row in db.execute("""SELECT p.id,p.nickname FROM participants p
                JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE participant_id=p.id)
                WHERE s.hidden=0 ORDER BY p.id""")]
            counts = dict(db.execute("SELECT participant_id,COUNT(*) FROM audience_votes GROUP BY participant_id")) if state["vote_state"] == "closed" else {}
            voted = bool(device and db.execute("SELECT 1 FROM audience_votes WHERE device_hash=?", (hashlib.sha256(device.encode()).hexdigest(),)).fetchone())
        teams.sort(key=lambda r: (-counts.get(r["id"], 0), r["id"]))
        response = app.make_response(render_template("vote.html", teams=teams, counts=counts, voted=voted, extras_state=state))
        if device is None:
            response.set_cookie("vz_device", devices.dumps(secrets.token_urlsafe(32)), max_age=365 * 86400,
                                secure=app.config["SESSION_COOKIE_SECURE"], httponly=True, samesite="Strict")
        return response

    @app.errorhandler(HTTPException)
    def error_page(error):
        messages = {404: "Nie znaleziono strony.", 405: "Niedozwolona metoda.",
                    413: "Formularz jest zbyt duży.", 400: "Nieprawidłowe żądanie."}
        message = error.description if error.description in ENGLISH else messages.get(error.code, "Usługa jest chwilowo niedostępna.")
        return render_template("error.html", message=translate(message)), error.code

    return app
