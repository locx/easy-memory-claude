"""Optional fast JSON backend: orjson -> stdlib json.

Normalizes parse errors to ValueError so callers can use
a single except clause (orjson.JSONDecodeError is NOT a
subclass of json.JSONDecodeError).
"""

try:
    import orjson as _orjson

    def loads(s):
        try:
            return _orjson.loads(s)
        except _orjson.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc

    def dumps(obj, separators=None):
        return _orjson.dumps(obj).decode("utf-8")

    def dump(obj, f, separators=None):
        f.write(_orjson.dumps(obj).decode("utf-8"))

    def load(f):
        try:
            return _orjson.loads(f.read())
        except _orjson.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc

    _backend = "orjson"

except ImportError:
    import json as _json

    def loads(s):
        return _json.loads(s)

    def dumps(obj, separators=None):
        if separators is None:
            separators = (",", ":")
        return _json.dumps(
            obj, separators=separators,
        )

    def dump(obj, f, separators=None):
        if separators is None:
            separators = (",", ":")
        _json.dump(
            obj, f, separators=separators,
        )

    def load(f):
        return _json.load(f)

    _backend = "json"
