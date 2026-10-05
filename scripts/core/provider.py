import datetime
import email.utils
import hashlib
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
import zoneinfo

from processing_jobs import canonical, connect, reject_secrets


class ProviderFailure(ValueError):
    pass


class ProviderDeferred(ProviderFailure):
    def __init__(self, next_attempt_at, reason):
        self.next_attempt_at = next_attempt_at
        self.reason = reason
        super().__init__(reason)


class ProviderHTTPError(Exception):
    def __init__(self, status, retry_after=None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f'Provider HTTP {status}')


def retry_delay(value, now):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = email.utils.parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


def estimate_tokens(payload):
    return max(1, math.ceil(len(canonical(payload).encode()) / 3))


class ProviderRuntime:
    def __init__(self, path, profile, transport=None, clock=time.time, sleeper=time.sleep, jitter=random.random,
                 credential=None, credential_scope=None):
        reject_secrets(profile)
        self.path = path
        self.profile = dict(profile)
        self.clock, self.sleeper, self.jitter = clock, sleeper, jitter
        self.transport = transport or self._transport
        self.custom_transport = transport is not None
        self.credential = credential
        self.credential_scope = credential_scope
        self.last_request_cached = False
        self.provider_managed = profile.get('provider') == 'google' and profile.get('provider_managed') is True
        if self.provider_managed:
            for name in ('rpm', 'tpm', 'rpd', 'daily_token_cap', 'daily_cost_cap'):
                self.profile.pop(name, None)
            profile = self.profile
        self.quota_failover = False
        if profile.get('provider') not in {'google', 'typesafe'} or not isinstance(profile.get('project_id'), str) or not profile['project_id']:
            raise ValueError('Provider and project_id are required')
        if profile.get('account_mode') not in {'free', 'paid'}:
            raise ValueError('account_mode must be free or paid')
        if not isinstance(profile.get('enabled', False), bool):
            raise ValueError('enabled must be a boolean')
        for name in ['daily_token_cap', 'daily_cost_cap']:
            if name in profile:
                value = profile[name]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                    raise ValueError(f'Invalid {name}')
        if not isinstance(profile.get('cost_rates', {}), dict):
            raise ValueError('cost_rates must map model IDs to pricing')
        for model, rates in profile.get('cost_rates', {}).items():
            if not isinstance(model, str) or not isinstance(rates, dict) or not rates:
                raise ValueError('Each cost rate must name a model and nonempty pricing')
            for name, value in rates.items():
                if name not in {'input_per_million', 'output_per_million', 'text_per_million', 'image_per_million', 'audio_per_million', 'video_per_million'} or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                    raise ValueError('Pricing values must be finite nonnegative rates per million tokens')
            if not any(name != 'output_per_million' for name in rates):
                raise ValueError('Configure input pricing as well as output pricing')
        if profile.get('cost_rates') and (not isinstance(profile.get('pricing_date'), str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', profile['pricing_date'])):
            raise ValueError('Configured pricing requires a YYYY-MM-DD pricing_date')
        for name in ([] if self.provider_managed else ['rpm', 'tpm', 'rpd']):
            if not isinstance(profile.get(name), int) or isinstance(profile[name], bool) or profile[name] < 1:
                raise ValueError(f'Configure a positive {name} from your provider project limits')
        for name, default in [('max_attempts', 3), ('max_elapsed_seconds', 120), ('max_inline_wait_seconds', 2)]:
            self.profile.setdefault(name, default)
            value = self.profile[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < (1 if name != 'max_inline_wait_seconds' else 0):
                raise ValueError(f'Invalid {name}')
        if not isinstance(self.profile['max_attempts'], int):
            raise ValueError('max_attempts must be an integer')
        self.scope = canonical([profile['provider'], profile['project_id']])
        self.cooldown_scope = canonical([self.scope, credential_scope]) if credential_scope else self.scope
        with connect(path) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS provider_profiles (scope TEXT PRIMARY KEY, configuration TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS provider_calls (id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,started_at REAL NOT NULL,lease_until REAL,next_attempt_at REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,result TEXT,reason TEXT,job_id TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS provider_attempts (id INTEGER PRIMARY KEY,call_id TEXT NOT NULL,scope TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,operation TEXT NOT NULL,started_at REAL NOT NULL,day TEXT NOT NULL,status TEXT NOT NULL,estimated_input_tokens INTEGER NOT NULL,actual_input_tokens INTEGER,actual_output_tokens INTEGER,estimated_cost REAL,usage_cost_estimate REAL,pricing_date TEXT)')
            connection.execute('CREATE INDEX IF NOT EXISTS provider_attempt_scope_time ON provider_attempts(scope,started_at)')
            connection.execute('CREATE TABLE IF NOT EXISTS provider_cooldowns (scope TEXT, model TEXT, next_attempt_at REAL NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(scope,model))')
            connection.execute('BEGIN IMMEDIATE')
            saved = connection.execute('SELECT configuration FROM provider_profiles WHERE scope=?', (self.scope,)).fetchone()
            if self.provider_managed:
                connection.execute("UPDATE provider_calls SET next_attempt_at=0,reason=NULL,attempts=0,started_at=? WHERE scope=? AND state='deferred' AND reason LIKE 'Configured %'", (self.clock(), self.scope))
                if saved:
                    old = json.loads(saved['configuration'])
                    for name in ('rpm', 'tpm', 'rpd', 'daily_token_cap', 'daily_cost_cap', 'limits_basis'):
                        old.pop(name, None)
                    old['provider_managed'] = True
                    old['limits_basis'] = self.profile.get('limits_basis')
                    saved = {'configuration': canonical(old)}
                    connection.execute('UPDATE provider_profiles SET configuration=? WHERE scope=?', (canonical(old), self.scope))
            coordinated = ['provider_managed', 'account_mode', 'rpm', 'tpm', 'rpd', 'daily_token_cap', 'daily_cost_cap', 'cost_rates', 'pricing_date', 'max_attempts', 'max_elapsed_seconds']
            if saved and any(json.loads(saved['configuration']).get(name) != self.profile.get(name) for name in coordinated):
                raise ValueError('Workers sharing a project must use the same coordinated limits and pricing')
            connection.execute('INSERT OR IGNORE INTO provider_profiles VALUES (?,?)', (self.scope, canonical(self.profile)))
            connection.commit()

    def _day(self, timestamp):
        timezone = zoneinfo.ZoneInfo('America/Los_Angeles') if self.profile['provider'] == 'google' else datetime.timezone.utc
        local = datetime.datetime.fromtimestamp(timestamp, timezone)
        tomorrow = datetime.datetime.combine(local.date() + datetime.timedelta(days=1), datetime.time(), timezone)
        return local.date().isoformat(), tomorrow.timestamp()

    def _cost(self, model, input_tokens, output_tokens=0):
        rates = self.profile.get('cost_rates', {}).get(model)
        if not rates:
            return None
        input_rate = max(value for name, value in rates.items() if name != 'output_per_million')
        return input_tokens * input_rate / 1e6 + output_tokens * rates.get('output_per_million', 0) / 1e6

    def _reserve(self, call_id, provider, model, operation, tokens, job_id, output_tokens=0):
        now = self.clock()
        day, reset = self._day(now)
        with connect(self.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            if job_id and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='processing_jobs'").fetchone():
                job = connection.execute('SELECT state FROM processing_jobs WHERE id=?', (job_id,)).fetchone()
                if job and job['state'] == 'cancelled':
                    raise ProviderFailure('Job cancelled')
            row = connection.execute('SELECT * FROM provider_calls WHERE id=?', (call_id,)).fetchone()
            if row and row['state'] == 'completed':
                return None, json.loads(row['result'])
            cooldown = connection.execute('SELECT next_attempt_at,reason FROM provider_cooldowns WHERE scope=? AND model=?', (self.cooldown_scope, model)).fetchone()
            if cooldown and cooldown['next_attempt_at'] > now:
                raise ProviderDeferred(cooldown['next_attempt_at'], cooldown['reason'])
            if row and row['state'] in {'failed', 'review-needed'}:
                raise ProviderFailure(row['reason'])
            if row and row['state'] == 'running':
                if row['lease_until'] <= now:
                    connection.execute("UPDATE provider_calls SET state='review-needed',reason='Interrupted request; inspect usage before explicitly retrying' WHERE id=?", (call_id,))
                    connection.commit()
                    raise ProviderFailure('Interrupted request; inspect usage before explicitly retrying')
                raise ProviderDeferred(row['lease_until'], 'Another worker owns this provider request')
            if row and row['next_attempt_at'] > now:
                raise ProviderDeferred(row['next_attempt_at'], row['reason'] or 'Request deferred')
            if row and row['state'] == 'deferred' and (row['reason'] or '').startswith('Provider HTTP 429') and (self.provider_managed or now - row['started_at'] >= self.profile['max_elapsed_seconds']):
                connection.execute('UPDATE provider_calls SET attempts=0,started_at=? WHERE id=?', (now, call_id))
                row = connection.execute('SELECT * FROM provider_calls WHERE id=?', (call_id,)).fetchone()
            if row and (row['attempts'] >= self.profile['max_attempts'] or (row['attempts'] and now - row['started_at'] >= self.profile['max_elapsed_seconds'])):
                connection.execute("UPDATE provider_calls SET state='failed',reason='Retry budget exhausted' WHERE id=?", (call_id,))
                connection.commit()
                raise ProviderFailure('Retry budget exhausted')
            deferred = None
            if not self.provider_managed:
                daily = connection.execute('SELECT COUNT(*) AS requests,COALESCE(SUM(MAX(estimated_input_tokens,COALESCE(actual_input_tokens,0))),0) AS tokens,COALESCE(SUM(MAX(COALESCE(estimated_cost,0),COALESCE(usage_cost_estimate,0))),0) AS cost FROM provider_attempts WHERE scope=? AND day=?', (self.scope, day)).fetchone()
                minute = connection.execute('SELECT started_at,MAX(estimated_input_tokens,COALESCE(actual_input_tokens,0)) AS tokens FROM provider_attempts WHERE scope=? AND started_at>? ORDER BY started_at', (self.scope, now - 60)).fetchall()
                if tokens > self.profile['tpm']:
                    raise ProviderFailure('One request exceeds the configured token-per-minute limit; split its input')
                if daily['requests'] >= self.profile['rpd'] or daily['tokens'] + tokens > self.profile.get('daily_token_cap', float('inf')):
                    deferred = (reset, 'Configured daily budget reached')
                elif self.profile.get('daily_cost_cap') is not None:
                    cost = self._cost(model, tokens, output_tokens)
                    if cost is None:
                        raise ProviderFailure('Cost cap requires configured model pricing')
                    if daily['cost'] + cost > self.profile['daily_cost_cap']:
                        deferred = (reset, 'Configured estimated daily cost budget reached')
                if deferred is None and (len(minute) >= self.profile['rpm'] or sum(item['tokens'] for item in minute) + tokens > self.profile['tpm']):
                    deferred = (minute[0]['started_at'] + 60.001, 'Configured request or token pacing limit reached')
            if deferred:
                connection.execute("INSERT INTO provider_calls (id,scope,state,started_at,next_attempt_at,reason,job_id) VALUES (?,?,'deferred',?,?,?,?) ON CONFLICT(id) DO UPDATE SET state='deferred',next_attempt_at=excluded.next_attempt_at,reason=excluded.reason", (call_id, self.scope, now, deferred[0], deferred[1], job_id))
                connection.commit()
                raise ProviderDeferred(*deferred)
            connection.execute("INSERT INTO provider_calls (id,scope,state,started_at,lease_until,attempts,job_id) VALUES (?,?,'running',?,?,1,?) ON CONFLICT(id) DO UPDATE SET state='running',lease_until=excluded.lease_until,started_at=CASE WHEN provider_calls.attempts=0 THEN excluded.started_at ELSE provider_calls.started_at END,attempts=provider_calls.attempts+1", (call_id, self.scope, now, now + 90, job_id))
            cursor = connection.execute("INSERT INTO provider_attempts (call_id,scope,provider,model,operation,started_at,day,status,estimated_input_tokens,estimated_cost,pricing_date) VALUES (?,?,?,?,?,?,?,'running',?,?,?)", (call_id, self.scope, provider, model, operation, now, day, tokens, self._cost(model, tokens, output_tokens), self.profile.get('pricing_date')))
            connection.commit()
            return cursor.lastrowid, None

    def _finish(self, call_id, attempt_id, state, reason=None, result=None, next_attempt_at=0, model=None):
        usage = (result or {}).get('usageMetadata', (result or {}).get('usage', {}))
        input_tokens = usage.get('promptTokenCount', usage.get('input_tokens'))
        output_tokens = usage.get('candidatesTokenCount', usage.get('output_tokens'))
        for value in [input_tokens, output_tokens]:
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ProviderFailure('Malformed provider token usage')
        with connect(self.path) as connection:
            connection.execute('UPDATE provider_attempts SET status=?,actual_input_tokens=?,actual_output_tokens=?,usage_cost_estimate=? WHERE id=?', (state, input_tokens, output_tokens, self._cost(model, input_tokens, output_tokens or 0) if input_tokens is not None else None, attempt_id))
            connection.execute('UPDATE provider_calls SET state=?,reason=?,result=?,next_attempt_at=?,lease_until=NULL WHERE id=?', (state, reason, canonical(result) if result is not None else None, next_attempt_at, call_id))

    def request(self, provider, model, operation, payload, estimated_tokens=0, job_id=None, cache=True, cache_version='v1', cache_only=False):
        self.last_request_cached = False
        if not self.profile.get('enabled', False):
            raise ProviderFailure('Live provider processing is disabled; enable the configured profile')
        if not self.custom_transport and not (self.credential or os.environ.get('GEMINI_API_KEY' if provider == 'google' else 'TYPESAFE_API_KEY')):
            raise ProviderFailure('Provider key is missing from the runtime environment')
        if provider != self.profile['provider'] or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', model):
            raise ProviderFailure('Provider or model does not match the configured profile')
        if operation not in ({'embedContent', 'batchEmbedContents', 'generateContent'} if provider == 'google' else {'systemone'}):
            raise ProviderFailure('Unsupported provider operation')
        reject_secrets(payload)
        if not isinstance(estimated_tokens, int) or isinstance(estimated_tokens, bool) or estimated_tokens < 0:
            raise ValueError('estimated_tokens must be a nonnegative integer')
        call_id = hashlib.sha256(canonical([self.cooldown_scope, model, operation, payload, job_id, cache_version]).encode()).hexdigest()
        if not cache:
            call_id = hashlib.sha256((call_id + str(time.time_ns())).encode()).hexdigest()
        tokens = estimated_tokens or estimate_tokens(payload)
        output_tokens = payload.get('generationConfig', {}).get('maxOutputTokens', 0) if operation == 'generateContent' else 0
        if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
            raise ValueError('maxOutputTokens must be a nonnegative integer')
        if operation == 'generateContent' and self.profile.get('daily_cost_cap') is not None and self.profile.get('cost_rates', {}).get(model, {}).get('output_per_million', 0) > 0 and not output_tokens:
            raise ProviderFailure('A reading cost cap requires explicit maxOutputTokens')
        if cache_only:
            if not cache:
                return None
            with connect(self.path) as connection:
                if job_id and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='processing_jobs'").fetchone():
                    job = connection.execute('SELECT state FROM processing_jobs WHERE id=?', (job_id,)).fetchone()
                    if job and job['state'] == 'cancelled':
                        raise ProviderFailure('Job cancelled')
                row = connection.execute("SELECT result FROM provider_calls WHERE id=? AND state='completed'", (call_id,)).fetchone()
            if row:
                self.last_request_cached = True
                return json.loads(row['result'])
            return None
        while True:
            attempt_id, cached = self._reserve(call_id, provider, model, operation, tokens, job_id, output_tokens)
            if cached is not None:
                self.last_request_cached = True
                return cached
            try:
                result = self.transport(provider, model, operation, payload)
                if not isinstance(result, dict):
                    raise ProviderFailure('Malformed provider response')
                reject_secrets(result)
                self._finish(call_id, attempt_id, 'completed', result=result, model=model)
                return result
            except ProviderHTTPError as error:
                transient = error.status in {408, 429, 500, 502, 503, 504, 529}
                with connect(self.path) as connection:
                    row = connection.execute('SELECT attempts,started_at FROM provider_calls WHERE id=?', (call_id,)).fetchone()
                if error.status == 429:
                    delay = max(retry_delay(error.retry_after, self.clock()) or 0, min(30, 2 ** (row['attempts'] - 1)) * (1 + self.jitter()))
                    next_at = self.clock() + delay
                    if not self.provider_managed and row['attempts'] >= self.profile['max_attempts']:
                        next_at = max(next_at, row['started_at'] + self.profile['max_elapsed_seconds'])
                    reason = 'Provider HTTP 429; quota retry scheduled'
                    self._finish(call_id, attempt_id, 'deferred', reason, next_attempt_at=next_at)
                    with connect(self.path) as cooldown:
                        cooldown.execute('INSERT INTO provider_cooldowns VALUES(?,?,?,?) ON CONFLICT(scope,model) DO UPDATE SET next_attempt_at=MAX(next_attempt_at,excluded.next_attempt_at),reason=excluded.reason', (self.cooldown_scope, model, next_at, reason))
                    if self.quota_failover or next_at - self.clock() > self.profile['max_inline_wait_seconds']:
                        raise ProviderDeferred(next_at, reason) from None
                    self.sleeper(next_at - self.clock())
                    continue
                if not transient or row['attempts'] >= self.profile['max_attempts']:
                    reason = f'Provider HTTP {error.status}; ' + ('retry budget exhausted' if transient else 'correct credentials, request, or billing configuration')
                    self._finish(call_id, attempt_id, 'failed', reason)
                    raise ProviderFailure(reason) from None
                delay = max(retry_delay(error.retry_after, self.clock()) or 0, min(30, 2 ** (row['attempts'] - 1)) * (1 + self.jitter()))
                if self.clock() + delay - row['started_at'] >= self.profile['max_elapsed_seconds']:
                    self._finish(call_id, attempt_id, 'failed', 'Retry elapsed-time budget exhausted')
                    raise ProviderFailure('Retry elapsed-time budget exhausted') from None
                next_at = self.clock() + delay
                self._finish(call_id, attempt_id, 'deferred', f'Provider HTTP {error.status}; retry scheduled', next_attempt_at=next_at)
                if delay > self.profile['max_inline_wait_seconds']:
                    raise ProviderDeferred(next_at, f'Provider HTTP {error.status}; retry scheduled') from None
                self.sleeper(delay)
            except Exception:
                self._finish(call_id, attempt_id, 'review-needed', 'Request outcome unknown; inspect provider usage before explicitly retrying')
                raise ProviderFailure('Request outcome unknown; inspect provider usage before explicitly retrying') from None

    def _transport(self, provider, model, operation, payload):
        key = self.credential or os.environ.get('GEMINI_API_KEY' if provider == 'google' else 'TYPESAFE_API_KEY')
        if not key:
            raise ProviderFailure('Provider key is missing from the runtime environment')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:{operation}' if provider == 'google' else 'https://api.typesafe.ai/v1/systemone'
        headers = {'Content-Type': 'application/json', 'x-goog-api-key': key} if provider == 'google' else {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}
        request = urllib.request.Request(url, canonical(payload).encode(), headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode()
                if key in body:
                    raise ProviderFailure('Provider response contains credentials and was discarded')
                return json.loads(body)
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get('Retry-After') if error.headers else None
            if provider == 'google' and error.code == 429 and not retry_after:
                retry_after = 60
                try:
                    details = json.loads(error.read()).get('error', {}).get('details', [])
                    for detail in details:
                        delay = detail.get('retryDelay', '') if isinstance(detail, dict) else ''
                        if isinstance(delay, str) and re.fullmatch(r'\d+(?:\.\d+)?s', delay):
                            retry_after = max(1, float(delay[:-1]))
                except (ValueError, TypeError, AttributeError):
                    pass
            raise ProviderHTTPError(error.code, retry_after) from None

    def usage(self):
        day, reset = self._day(self.clock())
        with connect(self.path) as connection:
            row = connection.execute('SELECT COUNT(*) AS attempts,COUNT(*)-COUNT(DISTINCT call_id) AS retry_attempts,COUNT(actual_input_tokens) AS attempts_with_input_usage,COUNT(*)-COUNT(actual_input_tokens) AS attempts_without_input_usage,COALESCE(SUM(estimated_input_tokens),0) AS estimated_input_tokens,SUM(actual_input_tokens) AS actual_input_tokens,SUM(actual_output_tokens) AS actual_output_tokens,SUM(estimated_cost) AS estimated_cost,SUM(usage_cost_estimate) AS usage_based_cost_estimate FROM provider_attempts WHERE scope=? AND day=?', (self.scope, day)).fetchone()
            states = connection.execute('SELECT state,COUNT(*) AS count FROM provider_calls WHERE scope=? GROUP BY state', (self.scope,)).fetchall()
        return {**dict(row), 'request_states': {item['state']: item['count'] for item in states}, 'provider': self.profile['provider'], 'project_id': self.profile['project_id'], 'account_mode': self.profile['account_mode'], 'day': day, 'daily_reset_at': reset, 'provider_remaining_quota': None, 'pricing_date': self.profile.get('pricing_date'), 'billing_total': None, 'cost_basis': 'Configured maximum input modality rate; local estimates, not provider billing'}

    def close(self):
        self.credential = None
