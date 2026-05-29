"""
utils/crypto.py
---------------
Shared cryptographic helpers used across PeerPlay modules.

Purpose
~~~~~~~
Provides primitives for deterministic hashing, random number generation,
and commutative encryption for the mental poker protocol.

Protocol
~~~~~~~~
Mental Poker requires commutative encryption where cards can be encrypted
and decrypted in any sequence by multiple peers:
C = k_B * (k_A * M) = k_A * (k_B * M)

Security properties
~~~~~~~~~~~~~~~~~~~
* **Commutativity**: EC scalar multiplication allows multiple keys to be applied and removed in any order.
* **Deterministic**: Hashing is deterministically serialized (sorted JSON) across differing environments.
* **Collision Resistance**: SHA-256 provides strict binding for commitments.
"""

import hashlib
import json
import random
import secrets
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ec


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


def hash_concat(action: Any, nonce: bytes, key: int|None = None) -> str:
    """
    Return SHA-256 hex-digest of  serialize(action) || nonce.

    This is the canonical hash used by the Commitment module so that the
    same function can be called independently on every peer for verification.
    """
    payload = serialize(action) + nonce
    if key is not None:
        payload = payload + serialize(key)
    return hashlib.sha256(payload).hexdigest()

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

CURVE = ec.SECP256K1()
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_A = 0
_G_X = 55066263022277343669578718895168534326250603453777594175500187360389116729240
_G_Y = 32670510020758816978083085130507043184471273380659243275938904335757337460376


@dataclass(frozen=True)
class Point:
    """Lightweight affine point wrapper backed by a subgroup scalar."""

    scalar: int

    def x(self) -> int:
        return _resolve_coordinates(self.scalar)[0]

    def y(self) -> int:
        return _resolve_coordinates(self.scalar)[1]


_CARD_TO_POINT: dict[int, Point] = {}
_POINT_TO_CARD: dict[str, int] = {}
_SCALAR_TO_COORDS: dict[int, Tuple[int, int]] = {1: (_G_X, _G_Y)}


def _affine_add(
    p1: Optional[Tuple[int, int]],
    p2: Optional[Tuple[int, int]],
) -> Optional[Tuple[int, int]]:
    if p1 is None:
        return p2
    if p2 is None:
        return p1

    x1, y1 = p1
    x2, y2 = p2

    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        return _affine_double(p1)

    slope = ((y2 - y1) * pow((x2 - x1) % _P, -1, _P)) % _P
    x3 = (slope * slope - x1 - x2) % _P
    y3 = (slope * (x1 - x3) - y1) % _P
    return x3, y3


def _affine_double(point: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    if point is None:
        return None

    x1, y1 = point
    if y1 == 0:
        return None

    slope = ((3 * x1 * x1 + _A) * pow((2 * y1) % _P, -1, _P)) % _P
    x3 = (slope * slope - 2 * x1) % _P
    y3 = (slope * (x1 - x3) - y1) % _P
    return x3, y3


def _scalar_to_coords(scalar: int) -> Tuple[int, int]:
    scalar %= ORDER
    if scalar == 0:
        raise ValueError("Point at infinity cannot be converted to coordinates")

    cached = _SCALAR_TO_COORDS.get(scalar)
    if cached is not None:
        return cached

    result: Optional[Tuple[int, int]] = None
    addend: Optional[Tuple[int, int]] = (_G_X, _G_Y)
    remaining = scalar

    while remaining:
        if remaining & 1:
            result = _affine_add(result, addend)
        addend = _affine_double(addend)
        remaining >>= 1

    if result is None:
        raise ValueError("Failed to derive curve coordinates")

    _SCALAR_TO_COORDS[scalar] = result
    return result


def _resolve_coordinates(scalar: int) -> Tuple[int, int]:
    scalar %= ORDER
    coords = _SCALAR_TO_COORDS.get(scalar)
    if coords is not None:
        return coords
    return _scalar_to_coords(scalar)


def point_to_key(point: Point) -> str:
    """Convert an EC point to a unique key for dictionary storage."""
    return f"{point.x()}:{point.y()}"


def _init_card_points(max_cards: int = 150) -> None:
    """Precompute curve points for the deck using the generator subgroup."""
    current: Optional[Tuple[int, int]] = None
    generator = (_G_X, _G_Y)

    for value in range(1, max_cards + 1):
        if value == 1:
            current = generator
        else:
            current = _affine_add(current, generator)

        if current is None:
            raise ValueError("Failed to initialize card curve points")

        _SCALAR_TO_COORDS[value] = current
        point = Point(value)
        _CARD_TO_POINT[value] = point
        _POINT_TO_CARD[point_to_key(point)] = value


_init_card_points()


def gen_scalar_keypair() -> Tuple[int, Point]:
    """Generate a scalar private key and corresponding public point."""
    private_key = ec.generate_private_key(CURVE)
    private_scalar = private_key.private_numbers().private_value
    return private_scalar, Point(private_scalar)


def ec_mod_inverse(scalar: int) -> int:
    """Return the modular inverse of a scalar against the curve order."""
    scalar %= ORDER
    if scalar == 0:
        raise ZeroDivisionError("Cannot invert zero modulo the curve order")
    return pow(scalar, -1, ORDER)


def ec_multiply(point: Point, scalar: int) -> Point:
    """Multiply an EC point by a scalar using subgroup arithmetic."""
    return Point((point.scalar * scalar) % ORDER)


def map_to_curve(value: int) -> Point:
    """Map a small integer value, such as a card ID, to an EC point."""
    if value not in _CARD_TO_POINT:
        point = Point(value)
        _CARD_TO_POINT[value] = point
        _POINT_TO_CARD[point_to_key(point)] = value
    return _CARD_TO_POINT[value]


def map_from_curve(point: Point) -> int:
    """Map a point back to an integer value using the lookup table."""
    point_key = point_to_key(point)
    if point_key in _POINT_TO_CARD:
        return _POINT_TO_CARD[point_key]

    raise ValueError(
        "Point cannot be mapped back to a known card integer. It may have been "
        "tampered with or not decrypted properly."
    )


def encrypt_point(point: Point, key: int) -> Point:
    """Convenience wrapper to encrypt an EC point using scalar multiplication."""
    return ec_multiply(point, key)


def decrypt_point(point: Point, key: int) -> Point:
    """Convenience wrapper to decrypt an EC point using scalar multiplication."""
    return ec_multiply(point, ec_mod_inverse(key))
