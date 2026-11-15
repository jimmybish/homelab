#!/usr/bin/env python3
"""Mint (or verify) the fleet honcho api-key JWT. argv-safe helper.

Review r1 finding (t_27b0ca98): the previous implementation interpolated
Jinja values directly into a shell command line. This helper receives ONLY
file paths and validated scalars via argv; the HS256 secret is read from a
0600 file by this process, never passed through shell text or argv.

Usage:
  mint_jwt.py <secret_file> <out_file> <workspace> <jwt_days>
  mint_jwt.py --verify <key_file> <secret_file> <workspace>

Exit codes: 0 ok, 2 invalid arguments, 1 mint/verify failure. Nothing
secret is ever printed to stdout/stderr.
"""
from __future__ import annotations

import os
import re
import sys
import time

import jwt

WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DAYS_MIN, DAYS_MAX = 1, 36500


def _die(code: int, msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def main(argv: list[str]) -> int:
    args = argv[1:]
    verify = False
    if args and args[0] == "--verify":
        verify, args = True, args[1:]

    if verify:
        if len(args) != 3:
            _die(2, "usage: mint_jwt.py --verify <key_file> <secret_file> <workspace>")
        key_file, secret_file, workspace = args
        if not WORKSPACE_RE.match(workspace):
            _die(2, "workspace must match ^[A-Za-z0-9_-]{1,64}$")
        try:
            key = open(key_file).read().strip()
            secret = open(secret_file).read().strip()
            claims = jwt.decode(key, secret, algorithms=["HS256"],
                                options={"require": ["exp", "iat"]})
        except SystemExit:
            raise
        except Exception:
            return 1
        if claims.get("w") != workspace:
            return 1
        return 0

    if len(args) != 4:
        _die(2, "usage: mint_jwt.py <secret_file> <out_file> <workspace> <jwt_days>")
    secret_file, out_file, workspace, jwt_days_raw = args
    if not WORKSPACE_RE.match(workspace):
        _die(2, "workspace must match ^[A-Za-z0-9_-]{1,64}$")
    if not re.match(r"^[0-9]{1,5}$", jwt_days_raw):
        _die(2, "jwt_days must be a plain integer")
    jwt_days = int(jwt_days_raw)
    if not DAYS_MIN <= jwt_days <= DAYS_MAX:
        _die(2, f"jwt_days must be between {DAYS_MIN} and {DAYS_MAX}")

    import time

    try:
        secret = open(secret_file).read().strip()
    except OSError:
        return 1
    if len(secret) < 32:
        _die(2, "secret file too short to be a valid AUTH_JWT_SECRET")

    iat = int(time.time())
    token = jwt.encode(
        {"sub": "hermes-fleet", "w": workspace, "iat": iat, "exp": iat + jwt_days * 86400},
        secret,
        algorithm="HS256",
    )
    # Round-trip verify before publishing: decode what we just built so a
    # corrupt PyJWT result never reaches api_key. Write to a sibling temp
    # file (0600 from creation) and os.replace, so api_key only ever appears
    # complete or not at all (a crash mid-write leaves no half-written key
    # that a later converge would mistake for a good cached one).
    tmp_file = out_file + ".tmp"
    fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"],
                            options={"require": ["exp", "iat", "sub"]})
        if claims.get("w") != workspace:
            raise ValueError("workspace claim mismatch")
    except Exception:
        os.unlink(tmp_file)
        return 1
    os.replace(tmp_file, out_file)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
