"""Porter Stemmer implementation for English suffix reduction.

Extracted from maintenance.py to reduce bloat and share with search.
"""

def _stem_step1(word):
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'i'
    if word.endswith('sses'):
        return word[:-2]
    if word.endswith('ness'):
        return word[:-4]
    if (word.endswith('s') and not word.endswith('ss')
            and not word.endswith('us') and len(word) > 3):
        return word[:-1]
    return word

def _stem_step2(word):
    if word.endswith('ated') and len(word) > 6:
        return word[:-4] + 'ate'
    if word.endswith('ied') and len(word) > 4:
        return word[:-3] + 'i'
    if word.endswith('ed') and not word.endswith('eed') and len(word) > 4:
        return word[:-2]
    if word.endswith('ing') and len(word) > 5:
        return word[:-3]
    if word.endswith('ation') and len(word) > 6:
        return word[:-5]
    if word.endswith('tion') and len(word) > 5:
        return word[:-4] + 't'
    return word

def _stem_step3(word):
    if word.endswith('ously') and len(word) > 6:
        return word[:-5]
    if word.endswith('ably') and len(word) > 5:
        return word[:-4]
    if word.endswith('ibly') and len(word) > 5:
        return word[:-4]
    if word.endswith('ally') and len(word) > 5:
        return word[:-4] + 'al'
    if word.endswith('ly') and len(word) > 4:
        return word[:-2]
    if word.endswith('ful') and len(word) > 5:
        return word[:-3]
    if word.endswith('ment') and len(word) > 5:
        return word[:-4]
    if word.endswith('able') and len(word) > 5:
        return word[:-4]
    if word.endswith('ible') and len(word) > 5:
        return word[:-4]
    return word

def porter_stem(word):
    """Pure-Python Porter stemmer for common suffixes."""
    if len(word) <= 3:
        return word
    word = _stem_step1(word)
    word = _stem_step2(word)
    word = _stem_step3(word)
    return word

# Stem cache — avoids re-stemming the same word
_stem_cache = {}
_STEM_CACHE_MAX = 50_000

def stem_word(word):
    """Cached Porter stem lookup with bounded LRU eviction."""
    s = _stem_cache.get(word)
    if s is not None:
        return s
    s = porter_stem(word)
    if len(_stem_cache) >= _STEM_CACHE_MAX:
        # Evict oldest half to amortize eviction cost
        to_keep = _STEM_CACHE_MAX // 2
        keys = list(_stem_cache.keys())
        for k in keys[:len(keys) - to_keep]:
            del _stem_cache[k]
    _stem_cache[word] = s
    return s
