"""Stopwords, Synonyms, and token processing utilities.

Extracted from maintenance.py to centralize text processing.
"""
import json
import os
import re
from .stem import porter_stem

# Pre-compiled regexes (Unicode-aware)
_RE_CAMEL = re.compile(r'([a-z\u00e0-\u00ff])([A-Z\u00c0-\u00df])')
_RE_SEPS = re.compile(r'[_\-.\s]+')
_RE_HEX_NOISE = re.compile(r'^[0-9a-f]{8,}$')

STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "need", "must", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "about", "like", "through", "after", "over",
    "between", "out", "against", "during", "without",
    "before", "under", "around", "among", "it", "its",
    "this", "that", "these", "those", "he", "she", "they",
    "we", "you", "i", "me", "him", "her", "us", "them",
    "my", "your", "his", "our", "their", "what", "which",
    "who", "whom", "how", "when", "where", "why", "all",
    "each", "every", "both", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because",
    "but", "and", "or", "if", "then", "else", "also",
})

_SYNONYM_GROUPS = (
    ('api', 'endpoint', 'route'),
    ('async', 'asynchronous', 'concurrent'),
    ('auth', 'authentication', 'authorize', 'authorization'),
    ('auto', 'automobile', 'car', 'vehicle'),
    ('cache', 'caching', 'memoize', 'memoization'),
    ('cli', 'commandline'),
    ('config', 'configuration', 'configure', 'settings', 'preferences'),
    ('cred', 'credential', 'secret', 'apikey'),
    ('db', 'database'),
    ('del', 'delete', 'remove', 'drop'),
    ('dep', 'dependency', 'package', 'library'),
    ('deploy', 'deployment', 'release'),
    ('doc', 'documentation', 'readme'),
    ('env', 'environment'),
    ('err', 'error', 'exception', 'fault', 'failure'),
    ('fn', 'func', 'function', 'method', 'procedure'),
    ('init', 'initialize', 'initialise', 'bootstrap', 'setup'),
    ('log', 'logging', 'logger'),
    ('migrate', 'migration'),
    ('model', 'schema', 'table'),
    ('msg', 'message', 'notification'),
    ('perf', 'performance', 'latency', 'throughput'),
    ('queue', 'broker', 'pubsub'),
    ('repo', 'repository'),
    ('req', 'request'),
    ('resp', 'response'),
    ('retry', 'backoff', 'resilience'),
    ('svc', 'service', 'microservice'),
    ('sync', 'synchronize', 'synchronise', 'replicate'),
    ('test', 'testing', 'spec', 'unittest'),
    ('ui', 'frontend', 'interface'),
    ('val', 'validate', 'validation', 'verify', 'verification'),
    ('ws', 'websocket', 'realtime'),
)

SYNONYM_MAP = {}
for _group in _SYNONYM_GROUPS:
    canonical = _group[0]
    for _word in _group:
        SYNONYM_MAP[_word] = canonical
        s = porter_stem(_word)
        if s != _word:
            SYNONYM_MAP[s] = canonical
del _group, _word, canonical

def normalize_name(name):
    """Normalize entity name for fuzzy matching."""
    name = _RE_CAMEL.sub(r'\1 \2', name)
    return _RE_SEPS.sub(' ', name.lower().strip())

def normalize_type(etype):
    """Normalize entityType for consistent comparison/storage."""
    if not isinstance(etype, str):
        etype = str(etype)
    return etype.lower().strip()

def make_bigrams(tokens):
    """Generate underscore-joined bigrams from token list."""
    return [tokens[i] + '_' + tokens[i + 1] for i in range(len(tokens) - 1)]

def filter_token(w):
    """Return True if token should be kept in TF-IDF index."""
    if len(w) < 2 or len(w) > 50:
        return False
    if w in STOPWORDS:
        return False
    if _RE_HEX_NOISE.match(w):
        return False
    return True

def expand_synonyms(stemmed_word):
    """Return canonical synonym if mapped, else original."""
    return SYNONYM_MAP.get(stemmed_word, stemmed_word)

def load_aliases(memory_dir):
    """Load project-specific aliases from .memory/aliases.json."""
    aliases_path = os.path.join(memory_dir, "aliases.json")
    try:
        with open(aliases_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return dict(SYNONYM_MAP)
    if not isinstance(data, dict):
        return dict(SYNONYM_MAP)
    groups = data.get("groups", [])
    if not isinstance(groups, list) or not groups:
        return dict(SYNONYM_MAP)
    merged = dict(SYNONYM_MAP)
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        canonical = str(group[0]).lower().strip()
        if not canonical:
            continue
        for word in group:
            w = str(word).lower().strip()
            if w:
                merged[w] = canonical
                s = porter_stem(w)
                if s != w:
                    merged[s] = canonical
    return merged
