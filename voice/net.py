"""Shared TLS context.

Homebrew and pyenv Pythons ship without the macOS root certificates, so
websockets fails with CERTIFICATE_VERIFY_FAILED unless it is pointed at
certifi's bundle explicitly.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

import certifi


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())
