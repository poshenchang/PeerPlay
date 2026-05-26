import random
from typing import List
from ecdsa.ellipticcurve import Point
from ..consensus.consensus import ConsensusModule
from ..network.network import (
    RawMessage,
    MSG_TYPE_SHUFFLE, MSG_TYPE_TAG,
    MSG_TYPE_DETAG, MSG_TYPE_FINALDEAL
)
from ..utils.crypto import (
    gen_scalar_keypair,
    ec_multiply, ec_mod_inverse,
    map_to_curve, map_from_curve,
    hash_concat
)

def encrypt_point(point: Point, key: int) -> Point:
    return ec_multiply(point, key)

def decrypt_point(point: Point, key: int) -> Point:
    return ec_multiply(point, ec_mod_inverse(key))

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

    # TODO: return error if invalid?
    def play_card(self, card: int) -> None:
        # TODO: commit: hash(action | nonce | tkey)
        nonce = self.consensus.commitment.commit(card)
        # TODO: listen
        # TODO: reveal: card, nonce, tkey
        self.consensus.commitment.reveal(card, nonce)
        # TODO: verify: card, nonce, tkey[i]
        #   (1) hash(card | nonce | tkey[i]) == commit
        #   (2) encrypt_point(map_to_curve(card), tkey[i]) == _points
        expected = hash_concat(card, nonce)
        self.consensus.commitment.verify(card, nonce, expected)

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
            gen_scalar_keypair()[0] for _ in n_cards
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
            raw_msg: RawMessage = self.consensus.node.consume_messages(
                msg_type=msg_type,
                from_players=[self.prev_player_id],
                expected_count=1
            )
            # TODO: use receive to verify?
            if raw_msg.payload["to"] != self.pid:
                # TODO: should not happen?
                continue
            break
        self._points = raw_msg.payload["points"]

    def _listen_finaldeal(self) -> None:
        raw_msg: RawMessage = self.consensus.node.consume_messages(
            msg_type=MSG_TYPE_FINALDEAL,
            expected_count=1
        )
        # TODO: use receive to verify?
        self._points = raw_msg.payload["points"]

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
