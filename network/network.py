"""
network/network.py
------------------
P2P full-mesh (complete-graph) network layer for PeerPlay.

Responsibilities
~~~~~~~~~~~~~~~~
* Maintain a sorted, globally-consistent ``player_list``.
* Provide ``broadcast`` / ``receive`` as the **only** public send/receive API.
* ``receive`` performs a majority-vote cross-check to detect Byzantine peers.
* ``_connect`` / ``_send`` / ``_listen`` are **stubs** — transport-layer
  implementation is delegated to the transport team.

Concurrency model
~~~~~~~~~~~~~~~~~
``_listen`` runs in a **daemon thread** and appends every incoming frame into
``msg_queue`` (protected by ``_queue_lock``).  ``receive`` polls that queue
in a tight loop with a configurable timeout so neither side blocks the other
and there is no risk of deadlock.

Message types (stored in payload["type"])
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
MSG_TYPE_VAL    — validation broadcast emitted by ``receive``
MSG_TYPE_COMMIT — commitment hash broadcast emitted by CommitmentModule
MSG_TYPE_REVEAL — reveal broadcast emitted by CommitmentModule
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..utils.crypto import serialize

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MSG_TYPE_VAL: str = "val"       # validation / echo broadcast
MSG_TYPE_COMMIT: str = "commit" # commitment hash
MSG_TYPE_REVEAL: str = "reveal" # commitment reveal

DEFAULT_TIMEOUT: float = 10.0   # seconds to wait for peer responses
POLL_INTERVAL: float = 0.05     # seconds between queue polls


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RawMessage:
    """A raw frame as stored in ``msg_queue``."""
    from_player: str          # peer who sent this frame
    payload: Dict[str, Any]   # decoded message body
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# NetworkNode
# ---------------------------------------------------------------------------

class NetworkNode:
    """
    Represents *this* player's network endpoint in the PeerPlay P2P mesh.

    Parameters
    ----------
    player_id:
        Unique identifier for this node (e.g. a public key fingerprint or
        human-readable name).  Must appear in ``player_list``.
    player_list:
        Complete list of all players in the session.  Will be sorted
        in-place so every node has the same canonical order.
    timeout:
        Default wall-clock seconds ``receive`` will wait for peer echoes
        before falling back to whatever votes have arrived.
    """

    def __init__(
        self,
        player_id: str,
        player_list: List[str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if player_id not in player_list:
            raise ValueError(f"player_id '{player_id}' must be in player_list")

        self.player_id: str = player_id
        # Sorted once; every node uses the same sort so the order is identical
        self.player_list: List[str] = sorted(player_list)
        self.timeout: float = timeout

        # Thread-safe inbox — _listen appends, receive pops
        self.msg_queue: List[RawMessage] = []
        self._queue_lock: threading.Lock = threading.Lock()

        self._listen_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Transport stubs (to be implemented by the transport team)
    # ------------------------------------------------------------------

    def _connect(self, player: str) -> None:
        """
        [STUB] Open a persistent connection to *player*.

        Expected behaviour:
        - Perform handshake / authentication.
        - Store the connection handle so ``_send`` can look it up by player_id.
        - Called once per peer during session setup.
        """
        raise NotImplementedError(
            "_connect must be implemented by the transport layer"
        )

    def _send(self, player: str, payload: Dict[str, Any]) -> None:
        """
        [STUB] Deliver *payload* to a single *player* over the established
        connection.

        Expected behaviour:
        - Look up the connection for *player*.
        - Serialise *payload* (e.g. JSON over TCP / WebSocket).
        - Raise ``ConnectionError`` on send failure so callers can decide
          whether to retry or evict the peer.
        """
        raise NotImplementedError(
            "_send must be implemented by the transport layer"
        )

    def _listen(self) -> None:
        """
        [STUB] Background daemon thread — read frames from the transport and
        push them into ``msg_queue``.

        Expected skeleton::

            while True:
                # Block until a frame arrives from the transport
                from_player, raw_bytes = <transport>.recv()

                payload = json.loads(raw_bytes)

                # Append atomically; _queue_lock protects all queue access
                with self._queue_lock:
                    self.msg_queue.append(
                        RawMessage(from_player=from_player, payload=payload)
                    )

        Notes
        -----
        * Never call ``broadcast`` or ``receive`` from inside this thread;
          that would deadlock because both methods acquire ``_queue_lock``.
        * Val-tagged frames (``MSG_TYPE_VAL``) are stored in the queue just
          like any other frame — ``receive`` filters them out by type and
          ``correlation_id``.
        """
        raise NotImplementedError(
            "_listen must be implemented by the transport layer"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Spawn the background listener thread and connect to all peers.

        Call this once after constructing the node and before any
        ``broadcast`` / ``receive`` calls.
        """
        # Connect to every other player
        for player in self.player_list:
            if player != self.player_id:
                self._connect(player)

        # Start the listener daemon
        self._listen_thread = threading.Thread(
            target=self._listen,
            name=f"listen-{self.player_id}",
            daemon=True,
        )
        self._listen_thread.start()

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """
        Send *payload* to every peer (all players except ourselves).

        This is the **only** way external modules should send messages.
        Do **not** call ``_send`` directly from outside this class.

        Parameters
        ----------
        payload:
            Arbitrary JSON-serialisable dict.  A ``from`` field is
            automatically injected so recipients know the origin.
        """
        payload = {**payload, "from": self.player_id}
        peers = [p for p in self.player_list if p != self.player_id]
        for peer in peers:
            self._send(peer, payload)

    def receive(
        self,
        sender: str,
        rcv_msg: Any,
        timeout: Optional[float] = None,
    ) -> Tuple[str, Any]:
        """
        Verify *rcv_msg* (claimed to be from *sender*) via peer cross-check
        and return the majority-agreed real message.

        Algorithm
        ---------
        1. Broadcast a **validation frame** carrying our copy of the message::

               { type: "val",
                 correlation_id: <uuid>,   # ties votes to this receive() call
                 original_sender: sender,
                 content: rcv_msg }

        2. Poll ``msg_queue`` for validation frames from other peers that
           share the same ``correlation_id``.  Stop when we have a majority
           or timeout expires.

        3. Majority vote over all collected ``content`` values (including
           our own) determines ``real_msg``.

        4. Return ``(sender, real_msg)``.

        Deadlock avoidance
        ------------------
        ``_listen`` **only** appends frames — it never calls ``receive`` or
        ``broadcast``.  ``receive`` polls the queue with a sleep loop, so
        there is no nested lock acquisition.

        Timeout handling
        ----------------
        If fewer than a majority of peers respond before *timeout* seconds,
        we proceed with the votes we have.  Callers may treat this as a
        warning (peer may be offline or Byzantine).

        Parameters
        ----------
        sender:
            Player ID of the node whose broadcast we're validating.
        rcv_msg:
            The message content we received from *sender*.
        timeout:
            Override the node-level default timeout for this call.

        Returns
        -------
        (sender, real_msg):
            *sender* is unchanged; *real_msg* is the majority-agreed content.

        Raises
        ------
        RuntimeError:
            If no votes could be collected at all (should not happen in a
            healthy network).
        """
        if timeout is None:
            timeout = self.timeout

        # --- Step 1: announce what we received ---
        # correlation_id scopes the vote to this specific receive() call
        # so concurrent receive() calls on different messages don't mix.
        correlation_id = str(uuid.uuid4())

        val_payload: Dict[str, Any] = {
            "type": MSG_TYPE_VAL,
            "correlation_id": correlation_id,
            "original_sender": sender,
            "content": rcv_msg,
        }
        self.broadcast(val_payload)

        # --- Step 2: collect validation echoes from peers ---
        peers = set(self.player_list) - {self.player_id}
        majority_threshold = len(self.player_list) // 2 + 1

        # Seed with our own vote
        votes: Dict[str, Any] = {self.player_id: rcv_msg}

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            # We need at least majority_threshold votes total to stop early
            if len(votes) >= majority_threshold:
                break

            matching: List[RawMessage] = []

            with self._queue_lock:
                remaining: List[RawMessage] = []
                for msg in self.msg_queue:
                    p = msg.payload
                    is_val = p.get("type") == MSG_TYPE_VAL
                    same_corr = p.get("correlation_id") == correlation_id
                    same_orig = p.get("original_sender") == sender
                    new_voter = msg.from_player not in votes
                    known_peer = msg.from_player in peers

                    if is_val and same_corr and same_orig and new_voter and known_peer:
                        matching.append(msg)
                    else:
                        remaining.append(msg)  # keep unrelated messages

                for msg in matching:
                    votes[msg.from_player] = msg.payload.get("content")

                self.msg_queue = remaining  # prune consumed frames

            if not matching:
                time.sleep(POLL_INTERVAL)

        # --- Step 3: majority vote ---
        if not votes:
            raise RuntimeError(
                f"receive({sender!r}): no votes collected — "
                "network may be partitioned"
            )

        def _to_key(v: Any) -> str:
            """Deterministic, hashable representation for Counter."""
            try:
                return serialize(v).decode("utf-8")
            except Exception:
                return repr(v)

        counter: Counter = Counter(_to_key(v) for v in votes.values())
        winner_key, winner_count = counter.most_common(1)[0]

        if winner_count < majority_threshold:
            # Warn but still return best guess — caller can escalate
            import warnings
            warnings.warn(
                f"receive({sender!r}): only {winner_count}/{len(self.player_list)} "
                "votes agree — possible Byzantine peer",
                stacklevel=2,
            )

        # Recover the original Python object matching the winner key
        real_msg: Any = next(
            v for v in votes.values() if _to_key(v) == winner_key
        )

        return (sender, real_msg)

    # ------------------------------------------------------------------
    # Internal helpers (available to sibling modules)
    # ------------------------------------------------------------------

    def consume_messages(
        self,
        msg_type: str,
        from_players: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        expected_count: Optional[int] = None,
    ) -> List[RawMessage]:
        """
        Block until *expected_count* messages of *msg_type* arrive from
        *from_players* (or timeout), then return and remove them from the queue.

        Used internally by CommitmentModule / ConsensusModule to collect
        commit and reveal frames without having to replicate the polling loop.

        Parameters
        ----------
        msg_type:
            Value of payload["type"] to match.
        from_players:
            Whitelist of sender IDs to accept.  ``None`` means all peers.
        timeout:
            Seconds to wait.  Defaults to ``self.timeout``.
        expected_count:
            Stop early when this many messages have been collected.
            ``None`` means wait until timeout.
        """
        if timeout is None:
            timeout = self.timeout
        if from_players is None:
            from_players = [p for p in self.player_list if p != self.player_id]

        target_set = set(from_players)
        collected: List[RawMessage] = []
        seen_from: set = set()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if expected_count is not None and len(collected) >= expected_count:
                break

            with self._queue_lock:
                remaining: List[RawMessage] = []
                for msg in self.msg_queue:
                    is_type = msg.payload.get("type") == msg_type
                    in_whitelist = msg.from_player in target_set
                    not_seen = msg.from_player not in seen_from

                    if is_type and in_whitelist and not_seen:
                        collected.append(msg)
                        seen_from.add(msg.from_player)
                    else:
                        remaining.append(msg)

                self.msg_queue = remaining

            time.sleep(POLL_INTERVAL)

        return collected

    def peers(self) -> List[str]:
        """Return all players except ourselves."""
        return [p for p in self.player_list if p != self.player_id]