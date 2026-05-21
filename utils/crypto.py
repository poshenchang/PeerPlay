"""
utils/crypto.py
---------------
Shared cryptographic helpers used across PeerPlay modules.
All hashing is SHA-256.  Serialisation is deterministic JSON so that
hash(action) is the same on every node regardless of Python version or
dict insertion order.
"""

import hashlib
import json
from typing import Any


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def serialize(value: Any) -> bytes:
    """
    Deterministically serialise *value* to bytes.

    - Dicts are sorted by key.
    - Non-JSON-native types (e.g. bytes) fall back to their repr string.
    """
    return json.dumps(value, sort_keys=True, default=repr).encode("utf-8")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def hash_value(data: Any) -> str:
    """Return SHA-256 hex-digest of *data* after deterministic serialisation."""
    return hashlib.sha256(serialize(data)).hexdigest()


def hash_concat(action: Any, nonce: bytes) -> str:
    """
    Return SHA-256 hex-digest of  serialize(action) || nonce.

    This is the canonical hash used by the Commitment module so that the
    same function can be called independently on every peer for verification.
    """
    action_bytes = serialize(action)
    return hashlib.sha256(action_bytes + nonce).hexdigest()