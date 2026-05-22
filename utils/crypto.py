"""
utils/crypto.py
---------------
Shared cryptographic helpers used across PeerPlay modules.

Purpose
~~~~~~~
Provides primitives for deterministic hashing, random number generation,
and Commutative Encryption (Mental Poker) using Elliptic Curve Cryptography.

Protocol
~~~~~~~~
Mental Poker requires Commutative Encryption where cards can be encrypted
and decrypted in any sequence by multiple peers:
C = k_B * (k_A * M) = k_A * (k_B * M)

::

    --- encryption phase ---
    Alice                               Bob
      |  k_A, _ = gen_scalar_keypair()    |
      |  point = map_to_curve(card)       |
      |  enc_A = ec_multiply(point, k_A)  |
      |  broadcast(enc_A)               → |  k_B, _ = gen_scalar_keypair()
      |                                   |  enc_AB = ec_multiply(enc_A, k_B)
      |                                   |  broadcast(enc_AB)
      
    --- decryption phase ---
      |  inv_A = ec_mod_inverse(k_A)      |
      |  dec_A = ec_multiply(enc_AB,inv_A)|
      |  broadcast(dec_A)               → |  inv_B = ec_mod_inverse(k_B)
      |                                   |  dec_AB = ec_multiply(dec_A,inv_B)
      |                                   |  card = map_from_curve(dec_AB)

Security properties
~~~~~~~~~~~~~~~~~~~
* **Commutativity**: EC scalar multiplication allows multiple keys to be applied and removed in any order.
* **Deterministic**: Hashing is deterministically serialized (sorted JSON) across differing environments.
* **Collision Resistance**: SHA-256 provides strict binding for commitments.

Usage example
~~~~~~~~~~~~~
::

    from utils.crypto import gen_scalar_keypair, map_to_curve, ec_multiply, map_from_curve, ec_mod_inverse

    k, P = gen_scalar_keypair()
    card = 12

    # ── Encrypt ──
    encoded = map_to_curve(card)
    encrypted = ec_multiply(encoded, k)

    # ── Decrypt ──
    decrypted = ec_multiply(encrypted, ec_mod_inverse(k))
    recovered_card = map_from_curve(decrypted)
"""

import hashlib
import json
import secrets
import random
from typing import Any, Tuple

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import Point
from ecdsa.util import randrange


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

# ---------------------------------------------------------------------------
# Random & Nonces
# ---------------------------------------------------------------------------

def gen_nonce(length: int = 32) -> bytes:
    """Generate cryptographically secure random bytes for use as a nonce."""
    return secrets.token_bytes(length)

def get_random_with_seed(seed: int) -> random.Random:
    """Return a deterministic PRNG based on a seed."""
    prng = random.Random()
    prng.seed(seed)
    return prng

# ---------------------------------------------------------------------------
# Mental Poker ECC Primitives
# ---------------------------------------------------------------------------

CURVE = SECP256k1.curve
GENERATOR = SECP256k1.generator
ORDER = SECP256k1.order

_CARD_TO_POINT = {}
_POINT_TO_CARD = {}

def point_to_key(point: Point) -> str:
    """Convert an EC Point to a unique uncompressed string key for dictionary storage."""
    return f"{point.x()}:{point.y()}"

def _init_card_points(max_cards: int = 150):
    """Precompute curve points for the deck to avoid Discrete Logarithm Problem brute forcing."""
    for i in range(1, max_cards + 1):
        pt = i * GENERATOR
        _CARD_TO_POINT[i] = pt
        _POINT_TO_CARD[point_to_key(pt)] = i

_init_card_points()

def gen_scalar_keypair() -> Tuple[int, Point]:
    """
    Generate a scalar private key and corresponding public point 
    for commutative encryption (Mental Poker).

    Returns
    -------
    private_scalar : int
        The secret key used for multiplier encryptions.
    public_point : Point
        The mapped public point.
    """
    private_scalar = randrange(ORDER)
    public_point = private_scalar * GENERATOR
    return private_scalar, public_point

def ec_mod_inverse(scalar: int) -> int:
    """
    Return the modular inverse of a scalar against the curve order.
    Used for decryption: (k * (k^-1)) % ORDER == 1.
    """
    return pow(scalar, -1, ORDER)

def ec_multiply(point: Point, scalar: int) -> Point:
    """
    Multiply an EC point by a scalar.
    This serves as both encrypt (point * k) and decrypt (point * k^-1).
    """
    return scalar * point

def map_to_curve(value: int) -> Point:
    """
    Map a small integer value (like a card ID) to an EC point using the precomputed table.
    """
    if value not in _CARD_TO_POINT:
        # Fallback for unexpected sizes, dynamically cache it
        pt = value * GENERATOR
        _CARD_TO_POINT[value] = pt
        _POINT_TO_CARD[point_to_key(pt)] = value
    return _CARD_TO_POINT[value]

def map_from_curve(point: Point) -> int:
    """
    Map a point back to an integer value using the precomputed O(1) loopup table.
    """
    pkey = point_to_key(point)
    if pkey in _POINT_TO_CARD:
        return _POINT_TO_CARD[pkey]
        
    raise ValueError("Point cannot be mapped back to a known card integer. It may have been tampered with or not decrypted properly.")