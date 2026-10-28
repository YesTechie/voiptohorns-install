"""Atomic updates of the app's .env (mode 600). Values are written single-quoted
(\\ and ' escaped), which python-dotenv reads back verbatim."""
import os
import re
import tempfile

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def quote(value):
    v = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return "'" + v + "'"


def update(path, values):
    """Set KEY=value for each item, keep every other line as is, replace the file atomically."""
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    todo = dict(values)
    out = []
    for line in lines:
        m = _KEY.match(line)
        if m and m.group(1) in todo:
            k = m.group(1)
            out.append("%s=%s" % (k, quote(todo.pop(k))))
        else:
            out.append(line)
    for k, v in todo.items():
        out.append("%s=%s" % (k, quote(v)))
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
