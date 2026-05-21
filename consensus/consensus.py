"""
consensus/consensus.py
----------------------
Distributed consensus for PeerPlay.

Provides two primitives that all players can call simultaneously and end
up with the **same** result without trusting any single peer.

get_global_seed
~~~~~~~~~~~~~~~
Runs a commit-reveal protocol so every player contributes entropy to a
shared random seed.  No single player can bias the result unless they can
predict every other player's secret — computationally infeasible.

Protocol::

    for every player in parallel:
        local_seed  = os.urandom(8)            # secret entropy contribution
        nonce       = commit(local_seed)        # hide it, publish hash

    # wait for every player's commit

    for every player in parallel:
        reveal(local_seed, nonce)               # expose contribution

    # wait for every player's reveal, verify against their commit hash

    global_seed = int(SHA-256(str(sum(all local_seeds))))

global_perm
~~~~~~~~~~~
Uses ``get_global_seed`` to deterministically shuffle an item list in a
way no player chose alone.

::

    seed  = get_global_seed()
    rng   = random.Random(seed)
    rng.shuffle(items)
    return items
"""

from __future__ import annotations

import hashlib
import os
import random
from typing import Any, Dict, List

from ..network.network import NetworkNode, MSG_TYPE_COMMIT, MSG_TYPE_REVEAL
from ..commitment.commitment import CommitmentModule, CommitError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConsensusError(Exception):
    """
    Raised when the consensus protocol cannot complete.

    Common causes:
    - A peer's reveal does not match their commit  → Byzantine fault.
    - Too few peers responded within the timeout   → network partition.
    """


# ---------------------------------------------------------------------------
# ConsensusModule
# ---------------------------------------------------------------------------

class ConsensusModule:
    """
    High-level consensus operations for PeerPlay.

    Parameters
    ----------
    node:
        The local player's ``NetworkNode``.
    commitment:
        A ``CommitmentModule`` bound to the same *node*.
    timeout:
        Per-phase wall-clock timeout (seconds).  Two phases exist (commit
        collection and reveal collection), so the total wall time is at most
        ``2 × timeout``.
    """

    def __init__(
        self,
        node: NetworkNode,
        commitment: CommitmentModule,
        timeout: float = 15.0,
    ) -> None:
        self.node = node
        self.commitment = commitment
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_global_seed(self) -> int:
        """
        Run a commit-reveal protocol with all peers and return a shared
        random integer seed that no single player could have predetermined.

        Steps
        -----
        1. Generate a random ``local_seed`` (64-bit unsigned int).
        2. ``nonce = commit(local_seed)``  — broadcasts our hash commitment.
        3. Collect every peer's commit frame (wait up to ``timeout`` s).
        4. ``reveal(local_seed, nonce)``   — broadcast our secret.
        5. Collect every peer's reveal frame and verify each one against
           the matching commit hash.
        6. Return ``int(SHA-256(str(Σ local_seeds)), 16)``.

        Returns
        -------
        int
            A large positive integer suitable for seeding Python's
            ``random.Random``.

        Raises
        ------
        ConsensusError
            If any peer's reveal fails verification, or if insufficient
            peers responded.
        """
        peers = self.node.peers()
        n_peers = len(peers)

        # ── Step 1: generate local entropy ──────────────────────────────
        local_seed: int = int.from_bytes(os.urandom(8), "big")

        # ── Step 2: commit ───────────────────────────────────────────────
        nonce: bytes = self.commitment.commit(local_seed)

        # ── Step 3: collect all peers' commit frames ─────────────────────
        commit_msgs = self.node.consume_messages(
            msg_type=MSG_TYPE_COMMIT,
            from_players=peers,
            timeout=self.timeout,
            expected_count=n_peers,
        )

        if len(commit_msgs) < n_peers:
            missing = set(peers) - {m.from_player for m in commit_msgs}
            raise ConsensusError(
                f"get_global_seed: commit phase timed out. "
                f"Missing commits from: {missing}"
            )

        # Map player_id → hash string from commit payloads
        peer_commits: Dict[str, str] = {
            msg.from_player: msg.payload["hash"]
            for msg in commit_msgs
        }

        # ── Step 4: reveal ───────────────────────────────────────────────
        self.commitment.reveal(local_seed, nonce)

        # ── Step 5: collect and verify all peers' reveals ────────────────
        reveal_msgs = self.node.consume_messages(
            msg_type=MSG_TYPE_REVEAL,
            from_players=peers,
            timeout=self.timeout,
            expected_count=n_peers,
        )

        if len(reveal_msgs) < n_peers:
            missing = set(peers) - {m.from_player for m in reveal_msgs}
            raise ConsensusError(
                f"get_global_seed: reveal phase timed out. "
                f"Missing reveals from: {missing}"
            )

        all_seeds: List[int] = [local_seed]

        for msg in reveal_msgs:
            pid = msg.from_player
            action = msg.payload["action"]
            recv_nonce = bytes.fromhex(msg.payload["nonce"])
            expected_hash = peer_commits[pid]

            if not self.commitment.verify(action, recv_nonce, expected_hash):
                raise ConsensusError(
                    f"get_global_seed: player '{pid}' revealed an action that "
                    f"does not match their commit hash — possible cheating!"
                )

            all_seeds.append(int(action))

        # ── Step 6: derive global seed ───────────────────────────────────
        seed_sum: int = sum(all_seeds)
        digest: str = hashlib.sha256(str(seed_sum).encode()).hexdigest()
        return int(digest, 16)

    def global_perm(self, items: List[Any]) -> List[Any]:
        """
        Return a shuffled copy of *items* using a seed that all players
        agreed upon — no player could have chosen the permutation alone.

        This is used, for example, to determine turn order at the start of a
        game session.

        Steps
        -----
        1. ``seed = get_global_seed()``
        2. ``rng = random.Random(seed)``
        3. Shuffle a copy of *items* with *rng* and return it.

        Parameters
        ----------
        items:
            Any list.  The original list is **not** modified.

        Returns
        -------
        list
            A deterministically shuffled copy of *items*.

        Raises
        ------
        ConsensusError
            Propagated from :meth:`get_global_seed` if the protocol fails.
        """
        seed: int = self.get_global_seed()
        rng = random.Random(seed)
        result = list(items)
        rng.shuffle(result)
        return result