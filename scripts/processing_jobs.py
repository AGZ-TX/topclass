import hashlib
import contextlib
import json
import math
import os
import pathlib
import sqlite3
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace('-', '_') in {'key', 'token', 'api_key', 'apikey', 'authorization', 'access_token', 'secret', 'password'}:
                raise ValueError('Credentials belong in the runtime environment')
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


@contextlib.contextmanager
def connect(path):
    path = pathlib.Path(path)
    project_root = pathlib.Path(__file__).resolve().parents[1]
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(project_root)
    except ValueError:
        relative = None
    if relative is not None and (not relative.parts or relative.parts[0] not in {'derived_private', 'private_sources'}):
        raise ValueError('Runtime databases must use a private folder')
    if path.is_symlink():
        raise ValueError('Runtime database cannot be a symbolic link')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
    finally:
        connection.close()


class DurableJobStore:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        with connect(path) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS processing_jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL, version TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, worker TEXT, lease_until REAL, next_attempt_at REAL NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT \'{}\', result TEXT, reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS job_retry_reviews (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, reviewer TEXT NOT NULL, rationale TEXT NOT NULL, reviewed_at REAL NOT NULL, previous_provider_calls TEXT NOT NULL)')

    def enqueue(self, kind, input_fingerprint, version, model, payload=None):
        if not all(isinstance(item, str) and item for item in [kind, input_fingerprint, version, model]):
            raise ValueError('Job identity fields must be nonempty strings')
        reject_secrets(payload)
        job_id = hashlib.sha256(canonical([kind, input_fingerprint, version, model]).encode()).hexdigest()
        now = self.clock()
        with connect(self.path) as connection:
            connection.execute('INSERT OR IGNORE INTO processing_jobs (id,kind,fingerprint,version,model,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?,\'pending\',?,?)', (job_id, kind, input_fingerprint, version, model, canonical(payload or {}), now, now))
        return self.get(job_id)

    def get(self, job_id):
        with connect(self.path) as connection:
            row = connection.execute('SELECT * FROM processing_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        for name in ['payload', 'checkpoint', 'result']:
            value[name] = json.loads(value[name]) if value[name] is not None else None
        return value

    def claim(self, worker, lease_seconds=300, job_id=None):
        if not isinstance(worker, str) or not worker or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('Worker and positive lease are required')
        now = self.clock()
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("UPDATE processing_jobs SET state='review-needed',reason='Expired lease; inspect possible completed provider calls',worker=NULL,lease_until=NULL,updated_at=? WHERE state='running' AND lease_until<=?", (now, now))
            sql = "SELECT id FROM processing_jobs WHERE state IN ('pending','deferred') AND next_attempt_at<=?"
            params = [now]
            if job_id:
                sql += ' AND id=?'
                params.append(job_id)
            row = connection.execute(sql + ' ORDER BY created_at,id LIMIT 1', params).fetchone()
            if row:
                connection.execute("UPDATE processing_jobs SET state='running',worker=?,lease_until=?,updated_at=? WHERE id=?", (worker, now + lease_seconds, now, row['id']))
            connection.commit()
        return self.get(row['id']) if row else None

    def _owned_update(self, job_id, worker, assignments, values):
        with connect(self.path) as connection:
            cursor = connection.execute('UPDATE processing_jobs SET ' + assignments + ',updated_at=? WHERE id=? AND state=\'running\' AND worker=? AND lease_until>?', (*values, self.clock(), job_id, worker, self.clock()))
            if cursor.rowcount != 1:
                raise ValueError('Job ownership is missing, cancelled, or expired')
        return self.get(job_id)

    def checkpoint(self, job_id, worker, value, lease_seconds=300):
        reject_secrets(value)
        if not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('Checkpoint lease must be finite and positive')
        return self._owned_update(job_id, worker, 'checkpoint=?,lease_until=?', (canonical(value), self.clock() + lease_seconds))

    def defer(self, job_id, worker, next_attempt_at, reason):
        if not isinstance(next_attempt_at, (int, float)) or not math.isfinite(next_attempt_at):
            raise ValueError('Deferred time must be finite')
        return self._owned_update(job_id, worker, "state='deferred',next_attempt_at=?,reason=?,worker=NULL,lease_until=NULL", (next_attempt_at, reason))

    def complete(self, job_id, worker, result):
        reject_secrets(result)
        return self._owned_update(job_id, worker, "state='completed',result=?,worker=NULL,lease_until=NULL", (canonical(result),))

    def review(self, job_id, worker, result):
        reject_secrets(result)
        return self._owned_update(job_id, worker, "state='review-needed',result=?,worker=NULL,lease_until=NULL", (canonical(result),))

    def fail(self, job_id, worker, reason):
        return self._owned_update(job_id, worker, "state='failed',reason=?,worker=NULL,lease_until=NULL", (reason,))

    def cancel(self, job_id):
        with connect(self.path) as connection:
            connection.execute("UPDATE processing_jobs SET state='cancelled',worker=NULL,lease_until=NULL,updated_at=? WHERE id=? AND state NOT IN ('completed','cancelled')", (self.clock(), job_id))
        return self.get(job_id)

    def retry_reviewed(self, job_id, retry_provider_calls=False, reviewer=None, rationale=None):
        if not isinstance(retry_provider_calls, bool):
            raise ValueError('retry_provider_calls must be an explicit boolean')
        if retry_provider_calls and any(not isinstance(value, str) or not value.strip() for value in (reviewer, rationale)):
            raise ValueError('Explicit provider retry requires a reviewer and rationale')
        now = self.clock()
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            job = connection.execute('SELECT state FROM processing_jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['state'] not in {'review-needed', 'failed'}:
                raise ValueError('Only reviewed or failed jobs can be explicitly retried')
            calls = []
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_calls'").fetchone():
                calls = [dict(row) for row in connection.execute(
                    'SELECT id,state,attempts,started_at,next_attempt_at,reason FROM provider_calls WHERE job_id=? ORDER BY id', (job_id,))]
            if any(call['state'] in {'running', 'cancelled'} for call in calls):
                raise ValueError('Cannot retry a job with running or cancelled provider calls')
            terminal = [call for call in calls if call['state'] in {'failed', 'review-needed'}]
            if terminal and not retry_provider_calls:
                raise ValueError('Attached provider failures require explicit retry_provider_calls with a reviewer and rationale')
            if retry_provider_calls:
                connection.execute('INSERT INTO job_retry_reviews (job_id,reviewer,rationale,reviewed_at,previous_provider_calls) VALUES (?,?,?,?,?)',
                    (job_id, reviewer.strip(), rationale.strip(), now, canonical(terminal)))
                if terminal:
                    connection.execute("UPDATE provider_calls SET state='pending',reason='Explicit provider retry reviewed',attempts=0,started_at=?,next_attempt_at=0,lease_until=NULL,result=NULL WHERE job_id=? AND state IN ('failed','review-needed')", (now, job_id))
            connection.execute("UPDATE processing_jobs SET state='pending',reason='Explicit reviewed retry',next_attempt_at=0,worker=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, job_id))
            connection.commit()
        return self.get(job_id)
