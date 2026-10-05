import hashlib
import json

from core.provider import ProviderDeferred
from core.search import replace_json


def binding(key):
    return hashlib.sha256(key.encode()).hexdigest()


def fallback_keys(home, config):
    from core.app import child

    expected = config.get('fallback_bindings', [])
    if not isinstance(expected, list) or len(expected) != len(set(expected)):
        raise ValueError('Invalid Google fallback configuration')
    if not expected:
        return []
    saved = json.loads(child(home, '.google-keys').read_text())
    if saved.get('format') != 'topclass-google-keys-v1':
        raise ValueError('Invalid private Google key storage')
    found = {}
    for entry in saved['fallbacks']:
        if binding(entry['key']) != entry['binding']:
            raise ValueError('Google fallback credential binding changed')
        if entry['binding'] in expected:
            found[entry['binding']] = entry['key']
    if set(found) != set(expected):
        raise ValueError('A configured Google fallback credential is missing')
    return [(token, found[token]) for token in expected]


def add_fallback(home, key):
    from core.app import child, locked

    token = binding(key)
    with locked(home) as home:
        config_path = child(home, '.google.json')
        if not config_path.is_file():
            raise ValueError('Set up the primary Google key before adding a fallback')
        config = json.loads(config_path.read_text())
        saved = fallback_keys(home, config)
        if token != config['credential_binding'] and token not in dict(saved):
            saved.append((token, key))
            replace_json(child(home, '.google-keys'), {'format': 'topclass-google-keys-v1',
                'fallbacks': [{'binding': item, 'key': value} for item, value in saved]})
            config['fallback_bindings'] = [item for item, _ in saved]
            replace_json(config_path, config)
    return {'configured': True, 'credential_count': 1 + len(saved), 'embedding_model': 'gemini-embedding-2',
        'next': 'Quota waits can try the backup key. Keys in the same Google project share provider quota; Google enforces quota; Topclass does not impose usage or spending caps.'}


class FallbackRuntime:
    def __init__(self, runtimes):
        self.runtimes = runtimes
        for runtime in runtimes:
            runtime.quota_failover = len(runtimes) > 1
        self.last_request_cached = False

    def request(self, *args, **kwargs):
        deferred = []
        self.last_request_cached = False
        cache_only = kwargs.pop('cache_only', False)
        for runtime in self.runtimes:
            result = runtime.request(*args, **kwargs, cache_only=True)
            if result is not None:
                self.last_request_cached = True
                return result
        if cache_only:
            return None
        for runtime in self.runtimes:
            try:
                result = runtime.request(*args, **kwargs)
                self.last_request_cached = runtime.last_request_cached
                return result
            except ProviderDeferred as error:
                if not error.reason.startswith('Provider HTTP 429'):
                    raise
                deferred.append(error)
        raise min(deferred, key=lambda error: error.next_attempt_at)

    def usage(self):
        return self.runtimes[0].usage() | {'credential_count': len(self.runtimes), 'usage_scope': 'Usage accounting across keys; Google enforces quota without Topclass usage caps'}

    def close(self):
        for runtime in self.runtimes:
            runtime.close()
