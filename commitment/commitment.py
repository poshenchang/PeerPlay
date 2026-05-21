"""
commitment/commitment.py
------------------------
Commit-reveal scheme for PeerPlay.

Purpose
~~~~~~~
Guarantees that players cannot change their mind after seeing others'
actions (e.g. Rock-Paper-Scissors, Battleship placement, seed generation).

Protocol
~~~~~~~~
::

    --- commit phase ---
    Alice                               Bob / all peers
      |  commit(action)                   |
      |  1. nonce = os.urandom(32)        |
      |  2. h = SHA-256(action || nonce)  |
      |  3. broadcast {type:commit, hash:h}→|
      |                                   |
    --- reveal phase ---
      |  reveal(action, nonce)            |
      |  4. broadcast {type:reveal,       |
      |       action, nonce}            → |
      |                                   | verify(action, nonce, h) → True/False

Security properties
~~~~~~~~~~~~~~~~~~~
* **Hiding**: the hash reveals nothing about *action* before reveal (random 32-byte nonce).
* **Binding**: a player cannot produce a different (action', nonce') that hashes to the
  same *h* — SHA-256 collision resistance.

Usage example
~~~~~~~~~~~~~
::

    cm = CommitmentModule(node)

    # ── commit phase (all players do this simultaneously) ──
    nonce = cm.commit(my_action)

    # ── wait for everyone's commit to arrive (managed by ConsensusModule) ──

    # ── reveal phase ──
    cm.reveal(my_action, nonce)

    # ── verify a peer's reveal ──
    ok = cm.verify(peer_action, peer_nonce_bytes, peer_hash)
"""

from __future__ import annotations

import os
from typing import Any, Dict

from ..network.network import NetworkNode, MSG_TYPE_COMMIT, MSG_TYPE_REVEAL
from ..utils.crypto import hash_concat


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CommitError(Exception):
    """Raised when a peer's reveal does not match their earlier commit."""


# ---------------------------------------------------------------------------
# CommitmentModule
# ---------------------------------------------------------------------------

class CommitmentModule:
    """
    Commit-reveal helper bound to a single ``NetworkNode``.

    Parameters
    ----------
    node:
        The local player's ``NetworkNode``.  Used to broadcast commit and
        reveal frames.
    """

    def __init__(self, node: NetworkNode) -> None:
        self.node = node

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def commit(self, action: Any) -> bytes:
        """
        Commit to *action* without revealing it.

        Steps
        -----
        1. Generate a cryptographically-random 32-byte *nonce*.
        2. Compute ``hash_val = SHA-256(serialize(action) || nonce)``.
        3. Broadcast ``{type: "commit", player: self_id, hash: hash_val}``
           so every peer records our commitment.
        4. Return *nonce* — the caller must pass it to :meth:`reveal` later.

        Parameters
        ----------
        action:
            Any JSON-serialisable value (int, str, dict, list …).

        Returns
        -------
        nonce : bytes
            32 random bytes.  **Keep secret until reveal phase.**
        """
        nonce: bytes = os.urandom(32)
        hash_val: str = hash_concat(action, nonce)

        payload: Dict[str, Any] = {
            "type": MSG_TYPE_COMMIT,
            "player": self.node.player_id,
            "hash": hash_val,
        }
        self.node.broadcast(payload)

        return nonce

    def reveal(self, action: Any, nonce: bytes) -> None:
        """
        Reveal *action* and *nonce* so peers can verify the earlier commitment.

        Steps
        -----
        1. Broadcast ``{type: "reveal", player: self_id, action: action,
           nonce: <hex-encoded nonce>}``.

        Parameters
        ----------
        action:
            The same value passed to :meth:`commit`.
        nonce:
            The bytes returned by :meth:`commit`.
        """
        payload: Dict[str, Any] = {
            "type": MSG_TYPE_REVEAL,
            "player": self.node.player_id,
            "action": action,
            "nonce": nonce.hex(),   # bytes are not JSON-native; hex-encode
        }
        self.node.broadcast(payload)

    def verify(self, action: Any, nonce: bytes, hash_val: str) -> bool:
        """
        Check that a peer's revealed ``(action, nonce)`` matches their
        previously broadcast *hash_val*.

        Parameters
        ----------
        action:
            Value the peer revealed.
        nonce:
            Bytes the peer revealed (already decoded from hex by the caller).
        hash_val:
            Hex-digest string the peer broadcast during the commit phase.

        Returns
        -------
        bool
            ``True`` iff ``SHA-256(serialize(action) || nonce) == hash_val``.

        Raises
        ------
        CommitError
            If verification fails and the caller wants to surface that as an
            exception rather than check a boolean.  (``verify`` itself only
            returns ``False``; callers may raise ``CommitError`` explicitly.)
        """
        return hash_concat(action, nonce) == hash_val