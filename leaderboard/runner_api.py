"""Token-authenticated machine endpoints and CSRF-protected organizer controls."""
import hmac
import json
import time
from flask import Blueprint, abort, current_app, jsonify, redirect, request, url_for
from leaderboard import remote, store


def install(app, admin_required):
    api = Blueprint('runner_api', __name__)

    @api.before_request
    def authenticate():
        token = current_app.config['RUNNER_TOKEN']
        supplied = request.headers.get('X-Runner-Token', '')
        if (not current_app.config['REMOTE_RUNNER'] or not token or len(supplied) > 256
                or not hmac.compare_digest(token.encode(), supplied.encode())):
            return jsonify(error='Runner authentication required'), 401
        if not request.is_json:
            return jsonify(error='JSON required'), 415
        if not store.allow_request(app.config['DATABASE'], 'runner-api', 600):
            return jsonify(error='Rate limit'), 429

    @api.post('/api/runner/<operation>')
    def operation(operation):
        methods = {name: getattr(remote, name) for name in ('register', 'claim', 'heartbeat', 'complete')}
        if operation not in methods:
            return jsonify(error='Unknown operation'), 404
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify(error='JSON object required'), 400
        try:
            with store.connect(app.config['DATABASE']) as db:
                db.execute('BEGIN IMMEDIATE')
                result = methods[operation](db, data, time.time())
            return jsonify(result)
        except remote.Conflict as error:
            return jsonify(error=str(error)), 409
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            return jsonify(error=str(error)[:200]), 400

    app.register_blueprint(api)

    @app.post('/admin/runner')
    @admin_required
    def configure_runner():
        try:
            overrides = {key: json.loads(request.form[key]) for key in
                         ('slots', 'cpu', 'memory_bytes', 'levels', 'ramp_seconds', 'hold_seconds') if request.form.get(key)}
            with store.connect(app.config['DATABASE']) as db:
                db.execute('BEGIN IMMEDIATE')
                remote.configure(db, request.form.get('address', ''), request.form.get('profile', ''), overrides,
                                 request.form.get('test_banner') == '1', time.time())
        except (ValueError, TypeError, KeyError):
            abort(400, 'Nieprawidłowy profil lub runner jest niedostępny. Zaczekaj na koniec batcha.')
        return redirect(url_for('admin'), code=303)

    @app.post('/admin/runner/cancel')
    @admin_required
    def cancel_remote_batch():
        with store.connect(app.config['DATABASE']) as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT rb.batch_id FROM remote_batches rb JOIN batches b ON b.id=rb.batch_id WHERE b.state='running'").fetchall():
                remote.fail_batch(db, row['batch_id'], time.time(), 'Cancelled by organizer')
        return redirect(url_for('admin'), code=303)

    @app.post('/admin/submissions/<int:submission_id>/artifact')
    @admin_required
    def approve_artifact(submission_id):
        if not app.config['REMOTE_RUNNER']:
            abort(400)
        try:
            value = remote.artifact(request.form.get('artifact', '').strip())
        except ValueError:
            abort(400, 'Podaj obraz z digestem lub adres repozytorium z pełnym SHA commita.')
        with store.connect(app.config['DATABASE']) as db:
            if not db.execute('UPDATE submissions SET approved_artifact=? WHERE id=? AND hidden=0', (json.dumps(value), submission_id)).rowcount:
                abort(404)
        return redirect(url_for('admin'), code=303)

    @app.get('/admin/runner/jobs/<int:job_id>/log')
    @admin_required
    def job_log(job_id):
        with store.connect(app.config['DATABASE']) as db:
            row = db.execute('SELECT log FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            abort(404)
        return row['log'], 200, {'Content-Type': 'text/plain; charset=utf-8',
                               'Content-Disposition': f'attachment; filename="job-{job_id}.txt"'}
