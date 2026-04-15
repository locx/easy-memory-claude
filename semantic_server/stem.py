"""Porter Stemmer implementation for English suffix reduction.

Extracted from maintenance.py to reduce bloat and share with search.
"""
from collections import OrderedDict

# Table-driven suffix rules for each step: (suffix, min_len, replacement)
_STEP1_RULES = [
    ('ies', 4, 'i'),
    ('sses', 4, 'ss'),
    ('ness', 4, ''),
]
_STEP2_RULES = [
    ('ated', 6, 'ate'),
    ('ied', 4, 'i'),
    ('ed', 4, ''),
    ('ing', 5, ''),
    ('ation', 6, ''),
    ('tion', 5, 't'),
]
_STEP3_RULES = [
    ('ously', 6, ''),
    ('ably', 5, ''),
    ('ibly', 5, ''),
    ('ally', 5, 'al'),
    ('ly', 4, ''),
    ('ful', 5, ''),
    ('ment', 5, ''),
    ('able', 5, ''),
    ('ible', 5, ''),
]


def _apply_rules(word, rules):
    for suffix, min_len, repl in rules:
        if word.endswith(suffix) and len(word) > min_len:
            return word[:-len(suffix)] + repl
    return word


def _stem_step1(word):
    if word.endswith('sses'):
        return word[:-2]
    if word.endswith('ness'):
        return word[:-4]
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'i'
    if (word.endswith('s') and not word.endswith('ss')
            and not word.endswith('us') and len(word) > 3):
        return word[:-1]
    return word


def _stem_step2(word):
    return _apply_rules(word, _STEP2_RULES)


def _stem_step3(word):
    return _apply_rules(word, _STEP3_RULES)


def porter_stem(word):
    """Pure-Python Porter stemmer for common suffixes."""
    if len(word) <= 3:
        return word
    word = _stem_step1(word)
    word = _stem_step2(word)
    word = _stem_step3(word)
    return word


# Stem cache — true LRU via OrderedDict
_stem_cache = OrderedDict()
_STEM_CACHE_MAX = 50_000


def stem_word(word):
    """Cached Porter stem lookup with true LRU eviction."""
    if word in _stem_cache:
        _stem_cache.move_to_end(word)
        return _stem_cache[word]
    s = porter_stem(word)
    _stem_cache[word] = s
    _stem_cache.move_to_end(word)
    if len(_stem_cache) > _STEM_CACHE_MAX:
        _stem_cache.popitem(last=False)
    return s
