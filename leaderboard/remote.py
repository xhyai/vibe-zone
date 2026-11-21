"""Transactional pull queue. The web process never contacts a runner or Docker."""
import hashlib
import hmac
import json
import math
import re
import secrets
import time

from bench import profiles, runner
from leaderboard import extras

LEASE_SECONDS = 90
MAX_ATTEMPTS = 3
ADDRESS = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,127}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')
PHASES = {'preparing', 'building', 'measuring', 'cleanup', 'uploading'}


class Conflict(ValueError):
    pass


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS runners (
            address TEXT PRIMARY KEY, metadata TEXT NOT NULL, seen_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runner_control (
            id INTEGER PRIMARY KEY CHECK(id=1), active TEXT, profile TEXT NOT NULL DEFAULT 'full',
            overrides TEXT NOT NULL DEFAULT '{}', test_banner INTEGER NOT NULL DEFAULT 1
        );
        INSERT OR IGNORE INTO runner_control(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS remote_batches (
            batch_id INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
            address TEXT NOT NULL, config TEXT NOT NULL, profile_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            submission_id INTEGER NOT NULL REFERENCES submissions(id), artifact TEXT NOT NULL,
            seed TEXT NOT NULL, flags TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            attempt INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL, deadline REAL,
            available_at REAL NOT NULL DEFAULT 0, phase TEXT, result_hash TEXT, log TEXT NOT NULL DEFAULT '',
            UNIQUE(batch_id,submission_id)
        );
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,available_at,id);
    ''')
    if 'approved_artifact' not in {r[1] for r in db.execute('PRAGMA table_info(submissions)')}:
        db.execute('ALTER TABLE submissions ADD COLUMN approved_artifact TEXT')


def artifact(value):
    if runner.IMAGE_ID.fullmatch(value):
        return {'kind': 'local', 'reference': value}
    if '@' in value:
        source, revision = value.rsplit('@', 1)
        if runner.REPO.fullmatch(source) and re.fullmatch('[a-f0-9]{40}', revision):
            runner.validate_source(source)
            return {'kind': 'repo', 'reference': source, 'revision': revision}
    runner.validate_source(value)
    if '@sha256:' not in value:
        raise ValueError('Use an image digest or a repository URL with @ followed by a full commit SHA')
    return {'kind': 'image', 'reference': value}


def register(db, data, now):
    address = data.get('address', '')
    metadata = data.get('metadata')
    if not isinstance(address, str) or not ADDRESS.fullmatch(address) or not isinstance(metadata, dict):
        raise ValueError('Invalid runner registration')
    required = {'cpus', 'memory_bytes', 'role', 'kernel', 'runtime', 'engine', 'version', 'architecture'}
    if set(metadata) != required or metadata.get('role') not in {'test', 'official'}:
        raise ValueError('Invalid runner metadata')
    if (type(metadata['cpus']) is not int or not 2 <= metadata['cpus'] <= 512 or
            type(metadata['memory_bytes']) is not int or not 3 * profiles.GIB <= metadata['memory_bytes'] <= 8 * 1024 * profiles.GIB):
        raise ValueError('Invalid runner resources')
    if any(not isinstance(metadata[k], str) or not 1 <= len(metadata[k]) <= 160 for k in required - {'cpus', 'memory_bytes'}):
        raise ValueError('Invalid runner version')
    if metadata['architecture'] not in {'amd64', 'x86_64'} or not metadata['runtime'].startswith('runsc '):
        raise ValueError('Runner requires native amd64 and runsc')
    metadata = {**metadata, 'fingerprint': profiles.host_fingerprint(metadata)}
    profiles.capacity(metadata, 'full')
    old = db.execute('SELECT metadata FROM runners WHERE address=?', (address,)).fetchone()
    if old and json.loads(old['metadata']) != metadata and db.execute("SELECT 1 FROM remote_batches rb JOIN batches b ON b.id=rb.batch_id WHERE b.state='running' AND rb.address=?", (address,)).fetchone():
        raise Conflict('Cannot change a runner while its batch is running')
    db.execute('INSERT INTO runners VALUES (?,?,?) ON CONFLICT(address) DO UPDATE SET metadata=excluded.metadata,seen_at=excluded.seen_at',
               (address, json.dumps(metadata, sort_keys=True), now))
    return {'registered': True, **public_state(db, now)}


def public_state(db, now=None):
    now = time.time() if now is None else now
    control = dict(db.execute('SELECT * FROM runner_control WHERE id=1').fetchone())
    active = db.execute('SELECT * FROM runners WHERE address=?', (control['active'],)).fetchone()
    online = bool(active and active['seen_at'] >= now - LEASE_SECONDS)
    official = bool(active and json.loads(active['metadata'])['role'] == 'official')
    return {'online': online, 'official': official, 'profile': control['profile'],
            'test_banner': bool(control['test_banner'] or not online or not official)}


def configure(db, address, name, overrides, test_banner, now):
    if db.execute("SELECT 1 FROM batches WHERE state='running'").fetchone():
        raise Conflict('Wait for the running batch before changing runner settings')
    row = db.execute('SELECT * FROM runners WHERE address=?', (address,)).fetchone()
    if not row or row['seen_at'] < now - LEASE_SECONDS:
        raise ValueError('Choose an online runner')
    host = json.loads(row['metadata'])
    profiles.make(host, name, overrides)
    if not test_banner and host['role'] != 'official':
        raise ValueError('Only an online official runner can remove the test banner')
    db.execute('UPDATE runner_control SET active=?,profile=?,overrides=?,test_banner=? WHERE id=1',
               (address, name, json.dumps(overrides), int(test_banner)))


def fail_batch(db, batch_id, now, reason):
    db.execute("UPDATE batches SET state='failed',finished_at=?,error=? WHERE id=? AND state='running'", (now, reason, batch_id))
    db.execute("UPDATE jobs SET state='cancelled' WHERE batch_id=? AND state IN ('queued','leased')", (batch_id,))


def expire(db, now):
    for job in db.execute("SELECT * FROM jobs WHERE state='leased' AND (lease_until<? OR deadline<?)", (now, now)).fetchall():
        if job['attempt'] >= MAX_ATTEMPTS or job['deadline'] < now:
            fail_batch(db, job['batch_id'], now, 'Runner lease expired; publication stopped')
        else:
            db.execute("UPDATE jobs SET state='queued',lease=NULL,lease_until=NULL,available_at=?,phase='preparing' WHERE id=?", (now + LEASE_SECONDS, job['id']))


def schedule(db, now):
    from leaderboard.store import latest
    if db.execute("SELECT 1 FROM batches WHERE state='running'").fetchone():
        return
    control = db.execute('SELECT * FROM control WHERE id=1').fetchone()
    if not control['requested'] and control['next_run'] > now:
        return
    selected = db.execute('SELECT * FROM runner_control WHERE id=1').fetchone()
    host = db.execute('SELECT * FROM runners WHERE address=? AND seen_at>=?', (selected['active'], now - LEASE_SECONDS)).fetchone()
    if not host:
        return
    config = profiles.make(json.loads(host['metadata']), selected['profile'], json.loads(selected['overrides']))
    fingerprint = runner.fingerprint(config)
    batch = db.execute("INSERT INTO batches(started_at,state) VALUES (?,'running')", (now,)).lastrowid
    db.execute('INSERT INTO remote_batches VALUES (?,?,?,?)', (batch, selected['active'], json.dumps(config), fingerprint))
    db.execute('UPDATE control SET requested=0,next_run=? WHERE id=1', ((int(now) // 900 + 1) * 900,))
    settings = extras.settings(db)
    flags = [k for k in ('hidden', 'chaos', 'bonus') if settings[k] and (k != 'bonus' or settings['bonus_public'])]
    seed = secrets.token_hex(32)
    for submission in latest(db):
        if submission['approved_artifact']:
            job_flags = flags + (['pilot'] if settings['ai_pilot'] and submission['pilot'] else [])
            db.execute('INSERT INTO jobs(batch_id,submission_id,artifact,seed,flags) VALUES (?,?,?,?,?)',
                       (batch, submission['id'], submission['approved_artifact'], seed, json.dumps(job_flags)))
        else:
            result = {'status': 'failed', 'max_vus': 0, 'profile': config, 'profile_hash': fingerprint,
                      'error': 'Oczekuje na zatwierdzenie obrazu przez organizatora.'}
            db.execute('INSERT INTO results VALUES (?,?,?)', (batch, submission['id'], json.dumps(result)))
    publish(db, batch, now)


def publish(db, batch, now):
    if db.execute("SELECT 1 FROM jobs WHERE batch_id=? AND state!='done'", (batch,)).fetchone():
        return
    if not db.execute("SELECT 1 FROM batches WHERE id=? AND state='running'", (batch,)).fetchone():
        return
    db.execute("UPDATE batches SET state='published',finished_at=? WHERE id=?", (now, batch))
    db.execute('UPDATE control SET next_run=? WHERE id=1', ((int(now) // 900 + 1) * 900,))
    old = "SELECT id FROM batches WHERE state!='running' ORDER BY id DESC LIMIT -1 OFFSET 64"
    db.execute(f'DELETE FROM results WHERE batch_id IN ({old})')
    db.execute(f'DELETE FROM batches WHERE id IN ({old})')


def claim(db, data, now):
    address = data.get('address')
    row = db.execute('SELECT metadata FROM runners WHERE address=?', (address,)).fetchone()
    if not row:
        raise ValueError('Register the runner first')
    db.execute('UPDATE runners SET seen_at=? WHERE address=?', (now, address))
    expire(db, now)
    schedule(db, now)
    batch = db.execute("SELECT rb.* FROM remote_batches rb JOIN batches b ON b.id=rb.batch_id WHERE rb.address=? AND b.state='running'", (address,)).fetchone()
    if not batch:
        return {'job': None}
    config = json.loads(batch['config'])
    if db.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=? AND state='leased'", (batch['batch_id'],)).fetchone()[0] >= config['slots']:
        return {'job': None}
    job = db.execute("SELECT * FROM jobs WHERE batch_id=? AND state='queued' AND available_at<=? ORDER BY id LIMIT 1", (batch['batch_id'], now)).fetchone()
    if not job:
        return {'job': None}
    lease = secrets.token_hex(32)
    deadline = job['deadline'] or now + config['max_run_seconds'] * 3 + 1800
    db.execute("UPDATE jobs SET state='leased',attempt=attempt+1,lease=?,lease_until=?,deadline=?,phase='preparing' WHERE id=?",
               (lease, now + LEASE_SECONDS, deadline, job['id']))
    return {'job': {'id': job['id'], 'lease': lease, 'lease_seconds': LEASE_SECONDS, 'deadline': deadline,
                    'artifact': json.loads(job['artifact']), 'config': config, 'profile_hash': batch['profile_hash'],
                    'seed': job['seed'], 'flags': json.loads(job['flags'])}}


def owned(db, data, now, allow_done=False):
    job = db.execute('SELECT j.*,rb.address,rb.config,rb.profile_hash,b.state AS batch_state FROM jobs j JOIN remote_batches rb ON rb.batch_id=j.batch_id JOIN batches b ON b.id=j.batch_id WHERE j.id=?', (data.get('id'),)).fetchone()
    if (not job or job['address'] != data.get('address') or not isinstance(data.get('lease'), str)
            or not hmac.compare_digest(job['lease'] or '', data['lease'])):
        raise Conflict('Stale or unknown job lease')
    if allow_done and job['state'] == 'done':
        return job
    if job['state'] != 'leased' or job['batch_state'] != 'running' or job['lease_until'] < now or job['deadline'] < now:
        raise Conflict('Job is no longer leased')
    return job


def heartbeat(db, data, now):
    job = owned(db, data, now)
    phase = data.get('phase', 'measuring')
    if phase not in PHASES:
        raise ValueError('Invalid job phase')
    db.execute('UPDATE jobs SET lease_until=?,phase=? WHERE id=?', (now + LEASE_SECONDS, phase, job['id']))
    db.execute('UPDATE runners SET seen_at=? WHERE address=?', (now, job['address']))
    return {'renewed': True}


def validate_result(result, job):
    if not isinstance(result, dict) or result.get('status') not in {'passed', 'failed'}:
        raise ValueError('Invalid result')
    if result.get('profile_hash') != job['profile_hash'] or result.get('profile') != json.loads(job['config']):
        raise ValueError('Result profile does not match the assigned profile')
    if result['status'] == 'passed':
        reps = result.get('repetitions')
        if not isinstance(reps, list) or len(reps) != 3 or not runner.IMAGE_ID.fullmatch(result.get('image_id', '')):
            raise ValueError('Three measurements of an immutable image are required')
        config = json.loads(job['config'])
        scores = [r.get('max_vus') for r in reps]
        if any(type(v) is not int or v not in [0] + config['levels'] for v in scores) or sorted(scores)[1] != result.get('max_vus'):
            raise ValueError('Invalid median score')
        for key in ('image_bytes', 'cold_start_ms'):
            value = result.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Missing measurement')
        if result['image_bytes'] >= config['max_image_bytes'] or result['cold_start_ms'] >= 30000:
            raise ValueError('Admission limit exceeded')
        if result['max_vus'] and (type(result.get('p99_ms')) not in (int, float) or not 0 <= result['p99_ms'] < 500 or result.get('error_percent') != 0):
            raise ValueError('Invalid qualifying latency or errors')
    json.dumps(result, allow_nan=False)


def complete(db, data, now):
    job = owned(db, data, now, allow_done=True)
    result, log = data.get('result'), data.get('log', '')
    validate_result(result, job)
    if not isinstance(log, str) or len(log.encode()) > 65536:
        raise ValueError('Build log exceeds 64 KiB')
    encoded = json.dumps(result, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if job['state'] == 'done':
        if job['result_hash'] != digest:
            raise Conflict('Conflicting result replay')
        return {'accepted': True, 'duplicate': True}
    if result.get('error_kind') == 'infrastructure' or result.get('cleanup_errors'):
        fail_batch(db, job['batch_id'], now, 'Runner infrastructure or cleanup failed; inspect job log')
        db.execute("UPDATE jobs SET state='done',result_hash=?,log=? WHERE id=?", (digest, log, job['id']))
        return {'accepted': True, 'published': False}
    db.execute('INSERT INTO results VALUES (?,?,?)', (job['batch_id'], job['submission_id'], encoded))
    db.execute("UPDATE jobs SET state='done',phase='uploading',result_hash=?,log=? WHERE id=?", (digest, log, job['id']))
    submission = db.execute('SELECT * FROM submissions WHERE id=?', (job['submission_id'],)).fetchone()
    extras.record_green(db, submission, result, extras.settings(db)['ai_pilot'])
    publish(db, job['batch_id'], now)
    return {'accepted': True}
