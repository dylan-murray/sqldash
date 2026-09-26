"""Masking for text shown to a user. Imports nothing heavy, so cli.py can load it eagerly."""

import re

REDACTED = "•••"
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/?#\s'\"]+@")


def mask_url_userinfo(text: str) -> str:
    """Mask the userinfo of every `scheme://user:pass@host` URL in text.

    The whole userinfo goes, not just a password: git hosts take a token as the
    username too (`https://ghp_x@github.com/...`). Works on a bare URL or on a
    message that quotes one, so git's own stderr passes through it as well."""
    return _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
