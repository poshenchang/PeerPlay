import random
from typing import List, Dict, Tuple
from ecdsa.ellipticcurve import Point
from ..consensus.consensus import ConsensusModule
from ..network.network import (
    RawMessage,
    MSG_TYPE_SHUFFLE, MSG_TYPE_TAG,
    MSG_TYPE_DETAG, MSG_TYPE_FINALDEAL,
    MSG_TYPE_COMMIT, MSG_TYPE_REVEAL
)
from ..utils.crypto import (
    gen_scalar_keypair,
    ec_multiply, ec_mod_inverse,
    map_to_curve, map_from_curve
)

def encrypt_point(point: Point, key: int) -> Point:
    return ec_multiply(point, key)

def decrypt_point(point: Point, key: int) -> Point:
    return ec_multiply(point, ec_mod_inverse(key))

class DealingError(Exception):
    """
    Raised when dealing cannot complete
    """

class PlayCardError(Exception):
    """
    Raised when play card cannot complete
    """

class DealingModule:
    def __init__(self, consensus: ConsensusModule) -> None:
        self.consensus = consensus
        self.pid: int = None                # The order for deal
        self.prev_player_id: str = None     # Used for receive()
        self.hand: List[int] = []           # The hand dealt
        self._points: List[Point] = []      # encrypted
        self._skey: int = None              # for shuffle, per deck
        self._tkeys: List[int] = []         # for tag, per card

    def deal(self, deck: List[int], hand_size: int) -> List[int]:
        self._init_pid()
        self._generate_keys(len(deck))
        self._encrypt_shuffle(deck)
        self._tag()
        self._detag(hand_size)
        self._decrypt_hand(hand_size)
        return self.hand

    def play_card(self, card: int) -> None:
        nonce, commit_msgs = self._commit_played_card(card)
        reveal_msgs = self._reveal_played_card(card, nonce)
        self._verify_played_cards(commit_msgs, reveal_msgs)

    def _init_pid(self) -> None:
        n_player = len(self.consensus.node.player_list)
        order = self.consensus.global_perm(list(range(n_player)))
        player_idx = self.consensus.node.player_list.index(
            self.consensus.node.player_id)
        self.pid = order[player_idx]

        prev_pid = (self.pid - 1) if (self.pid > 0) else (n_player - 1)
        prev_player_idx = order.index(prev_pid)
        self.prev_player_id = self.consensus.node.player_list[prev_player_idx]
        return

    def _generate_keys(self, n_cards: int) -> None:
        self._skey = gen_scalar_keypair()[0]
        self._tkeys = [
            gen_scalar_keypair()[0] for _ in range(n_cards)
        ]
        return

    def _broadcast_points(self, msg_type: str) -> None:
        self.consensus.node.broadcast({
            "type": msg_type,
            "points": self._points
        })
        return

    def _pass_points(self, msg_type: str) -> None:
        n_player = len(self.consensus.node.player_list)
        next_pid = (self.pid + 1) % n_player
        self.consensus.node.broadcast({
            "type": msg_type,
            "points": self._points,
            "to": next_pid
        })
        return

    def _listen_pass(self, msg_type: str) -> None:
        # TODO: from prev player or all players?
        while True:
            raw_msgs: List[RawMessage] = self.consensus.node.consume_messages(
                msg_type=msg_type,
                from_players=[self.prev_player_id],
                expected_count=1
            )
            if len(raw_msgs) != 1:
                raise DealingError(
                    f"_listen_pass: len(raw_msgs) = {len(raw_msgs)}, expected: 1"
                )
            # TODO: use receive to verify?
            if raw_msgs[0].payload["to"] != self.pid:
                # TODO: should not happen?
                continue
            break
        self._points = raw_msgs[0].payload["points"]

    def _listen_finaldeal(self) -> None:
        peers = self.consensus.node.peers()
        raw_msgs: List[RawMessage] = self.consensus.node.consume_messages(
            msg_type=MSG_TYPE_FINALDEAL,
            from_players=peers,
            expected_count=1
        )
        if (len(raw_msgs) != len(peers)):
            missing = set(peers) - {m.from_player for m in raw_msgs}
            raise DealingError(
                f"_listen_finaldeal: Missing finaldeal from: {missing}"
            )
        # TODO: use receive to verify?
        self._points = raw_msgs[0].payload["points"]

    def _encrypt_shuffle(self, deck: List[int]) -> None:
        if self.pid == 0:
            self._points = [map_to_curve(card) for card in deck]
        else:
            self._listen_pass(MSG_TYPE_SHUFFLE)

        for i in range(len(self._points)):
            self._points[i] = encrypt_point(self._points[i], self._skey)
        random.shuffle(self._points)

        self._pass_points(MSG_TYPE_SHUFFLE)
        return

    def _tag(self):
        if self.pid == 0:
            self._listen_pass(MSG_TYPE_SHUFFLE)
        else:
            self._listen_pass(MSG_TYPE_TAG)

        for i in range(len(self._points)):
            self._points[i] = decrypt_point(self._points[i], self._skey)
            self._points[i] = encrypt_point(self._points[i], self._tkeys[i])

        self._pass_points(MSG_TYPE_TAG)
        return

    def _detag(self, hand_size: int):
        if self.pid == 0:
            self._listen_pass(MSG_TYPE_TAG)
        else:
            self._listen_pass(MSG_TYPE_DETAG)

        n_player = len(self.consensus.node.player_list)
        for i in range(len(self._points) - n_player * hand_size):
            if i in range(self.pid * hand_size, (self.pid + 1) * hand_size):
                continue
            self._points[i] = decrypt_point(self._points[i], self._tkeys[i])

        if self.pid == len(self.consensus.node.player_list) - 1:
            self._broadcast_points(MSG_TYPE_FINALDEAL)
        else:
            self._pass_points(MSG_TYPE_DETAG)
        return

    def _decrypt_hand(self, hand_size: int):
        # TODO: last player also attend in consensus?
        if self.pid != len(self.consensus.node.player_list) - 1:
            self._listen_finaldeal()
        for i in range(self.pid * hand_size, (self.pid + 1) * hand_size):
            point = decrypt_point(self._points[i], self._tkeys[i])
            self.hand.append(map_from_curve(point))
        return

    def _commit_played_card(self, card: int) -> Tuple[bytes, List[RawMessage]]:
        node = self.consensus.node
        peers = node.peers()
        # TODO: commit: hash(action | nonce | tkey)
        nonce = self.consensus.commitment.commit(card)
        commit_msgs: List[RawMessage] = node.consume_messages(
            msg_type=MSG_TYPE_COMMIT,
            from_players=peers,
            expected_count=len(peers)
        )
        if len(commit_msgs) < len(peers):
            missing = set(peers) - {m.from_player for m in commit_msgs}
            raise PlayCardError(
                f"play_card: Missing commits from: {missing}"
            )
        return (nonce, commit_msgs)

    def _reveal_played_card(self, card: int, nonce: bytes) -> List[RawMessage]:
        node = self.consensus.node
        peers = node.peers()
        # TODO: reveal: card, nonce, tkey
        self.consensus.commitment.reveal(card, nonce)
        reveal_msgs: List[RawMessage] = node.consume_messages(
            msg_type=MSG_TYPE_REVEAL,
            from_players=peers,
            expected_count=len(peers)
        )
        if len(reveal_msgs) < len(peers):
            missing = set(peers) - {m.from_player for m in reveal_msgs}
            raise PlayCardError(
                f"play_card: Missing reveals from: {missing}"
            )
        return reveal_msgs

    def _verify_played_cards(self, commit_msgs: List[RawMessage],
                                   reveal_msgs: List[RawMessage]):
        # TODO: verify: card, nonce, tkey[i]
        #   (1) hash(card | nonce | tkey[i]) == commit
        #   (2) encrypt_point(map_to_curve(card), tkey[i]) == _points

        # Map player_id -> hash from commit
        peer_commits: Dict[str, str] = {
            msg.from_player: msg.payload["hash"] for msg in commit_msgs
        }
        for msg in reveal_msgs:
            player_id = msg.from_player
            recv_action = msg.payload["action"]
            recv_nonce = bytes.fromhex(msg.payload["nonce"])
            expected_hash = peer_commits[player_id]
            if not self.consensus.commitment.verify(recv_action,
                                                    recv_nonce,
                                                    expected_hash):
                raise PlayCardError(
                    f"_verify_played_cards: player '{player_id}' revealed "
                    f"an action that does not match its commit"
                )
