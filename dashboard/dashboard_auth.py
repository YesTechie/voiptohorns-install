#!/usr/bin/env python3
"""Dashboard login from .env: DASHBOARD_USER + DASHBOARD_PASS_HASH.

There is no default login. Without both keys the dashboard refuses sign-in.

Make a hash (password read from stdin, never from argv):
    printf '%s' "$PW" | ./venv/bin/python3 dashboard/dashboard_auth.py hash-password
Set the login in .env (user "admin", hashed password; used by the installer):
    printf '%s' "$PW" | ./venv/bin/python3 dashboard/dashboard_auth.py set-login /opt/voiptohorns/.env
"""
import hmac
import os
import sys

from werkzeug.security import check_password_hash, generate_password_hash

HASH_METHOD = "pbkdf2:sha256:600000"
DEFAULT_USER = "admin"


def hash_password(password):
    if not password:
        raise ValueError("empty password")
    return generate_password_hash(password, method=HASH_METHOD)


def configured_user():
    return (os.getenv("DASHBOARD_USER") or "").strip()


def configured_hash():
    return (os.getenv("DASHBOARD_PASS_HASH") or "").strip().strip("'\"")


def is_configured():
    return bool(configured_user() and configured_hash())


def same_user(a, b):
    return hmac.compare_digest((a or "").strip().lower().encode(), (b or "").strip().lower().encode())


def verify(username, password):
    if not is_configured() or not password:
        return False
    if not same_user(username, configured_user()):
        return False
    try:
        return check_password_hash(configured_hash(), password)
    except Exception:
        return False


def _main(argv):
    if len(argv) == 2 and argv[1] == "hash-password":
        target = None
    elif len(argv) == 3 and argv[1] == "set-login":
        target = argv[2]
    else:
        sys.stderr.write("usage: dashboard_auth.py hash-password | set-login <path/.env>  (password on stdin)\n")
        return 2
    pw = sys.stdin.readline().rstrip("\r\n")
    if not pw:
        sys.stderr.write("error: empty password on stdin\n")
        return 1
    if target is None:
        sys.stdout.write(hash_password(pw) + "\n")
        return 0
    import envfile
    envfile.update(target, {"DASHBOARD_USER": DEFAULT_USER, "DASHBOARD_PASS_HASH": hash_password(pw)})
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
