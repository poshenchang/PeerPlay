"""
PeerPlay — Decentralized P2P Game Platform
===========================================
Package structure:
    network/     — P2P full-mesh networking (broadcast / receive with majority-vote)
    commitment/  — Commit-reveal scheme (anti-regret, simultaneous action)
    consensus/   — Global seed & permutation agreement
    utils/       — Shared crypto helpers
"""

from .network.network import NetworkNode, RawMessage
from .commitment.commitment import CommitmentModule
from .consensus.consensus import ConsensusModule

__all__ = ["NetworkNode", "RawMessage", "CommitmentModule", "ConsensusModule"]