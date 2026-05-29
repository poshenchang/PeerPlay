import random
from typing import List, Dict, Tuple, Optional
from consensus import ConsensusModule
from network import (
    RawMessage,
    MSG_TYPE_SHUFFLE, MSG_TYPE_TAG,
    MSG_TYPE_DETAG, MSG_TYPE_FINALDEAL,
    MSG_TYPE_COMMIT, MSG_TYPE_REVEAL
)
from utils.crypto import (
    Point,
    gen_scalar_keypair,
    map_to_curve, map_from_curve,
    encrypt_point, decrypt_point
)


def _point_to_json(pt: Point) -> dict:
    """Serialize an EC Point to a JSON-safe dict."""
    return {"scalar": pt.scalar}


def _json_to_point(d: dict) -> Point:
    """Deserialize an EC Point from a JSON dict."""
    return Point(d["scalar"])

class DealingError(Exception):
    """
    Raised when dealing cannot complete
    """

class PlayCardError(Exception):
    """
    Raised when play card cannot complete
    """

class DealingModule:
    """
    Example usage: 
        1. Initalize `dm = DealingModule(consensus)`
        2. Every player calls `pid, hand = dm.deal()`
        3. To play a card (commit) `commit_id = dm.play_card(card)`
        4. To listen commits `dm.get_commit(enemy_pid, expect_count)`
        5. To reveal a card `dm.reveal_card(commit_id)`
        6. To get cards `dm.get_cards(enemy_pid, expect_count)`
    """
    def __init__(self, consensus: ConsensusModule) -> None:
        self.consensus = consensus
        self.pid: Optional[int] = None                # The order pid in order
        self.prev_player_id: Optional[str] = None     # Used for receive()
        self.hand: List[int] = []           # The hand dealt
        self._order: List[int] = []         # The order for this deal
        self._points: List[Point] = []      # encrypted
        self._skey: Optional[int] = None              # for shuffle, per deck
        self._tkeys: List[int] = []         # for tag, per card
        self._commit_count: int = 0         # the commit_id for receive side
        # A list of played but not revealed (commit_id, (card, nonce))
        self._played_queue: Dict[int, Tuple[int, bytes]] = {}
        # A nested dict for received but not verified commits
        #     (player_id, (commit_id, hash))
        self._commit_queue: Dict[str, Dict[int, str]] = {}

    async def deal(self, deck: List[int], hand_size: int) -> Tuple[int, List[int]]:
        """
        Decide an universal agreed order between all players in the same network node.
        The position in the order is returned as the first argument `pid`.
        Deal `hand_size` cards from `deck` following the above order.
            1st pass: encyprt with per deck key and shuffle
            2nd pass: decrypt 1st pass and encrypt with per card key
            3rd pass: decrypt 2nd pass for others
        Broadcast the final one layer encrypted deck in the end of 3rd pass.
        Each player decrypts the last one layer and return its `pid` and `hand`.
        """
        await self._init_pid()
        self._generate_keys(len(deck))
        await self._encrypt_shuffle(deck)
        await self._tag()
        await self._detag(hand_size)
        await self._decrypt_hand(hand_size)
        if self.pid is None:
            raise DealingError("PID not initialized during dealing")
        return (self.pid, self.hand)

    def play_card(self, card: int) -> int:
        """
        Commit `card`, broadcast the hash of this action to other players.
        Returns an `commit_id` for this commit, used when reveal.
        Note that `commit_id` is only unique per player,
        i.e. two players may have the same `commit_id`.
        """
        commit_id = self._commit_count
        nonce = self._commit_played_card(card)
        self._played_queue[commit_id] = (card, nonce)
        return commit_id

    async def get_commit(self, pid: int, expect_count: int) -> None:
        """
        Listen to other player's commit (specified by `pid`).
        Block until `expect_count` commits received,
        or raise error if timeout.
        """
        commit_msgs = await self._listen_commit(pid, expect_count)
        for msg in commit_msgs:
            player_id = msg.from_player
            commit_id = msg.payload["commit_id"]
            self._commit_queue.setdefault(player_id, {})[commit_id] = msg.payload["hash"]
        return

    def reveal_card(self, commit_id: int) -> None:
        """
        Reveal the earlier commit specified by `commit_id` returned by `play_card()`.
        Broadcast the actual card to other players.
        """
        card, nonce = self._played_queue.pop(commit_id)
        if nonce is None:
            raise PlayCardError(f"Cannot reveal commit {commit_id}: no commitment found.")
        self._reveal_played_card(card, nonce, commit_id)
        return

    async def get_cards(self, pid: int, expect_count: int) -> List[int]:
        """
        Returns the verified cards played by `pid`.
        Listens to other player's reveal (specified by `pid`),
        then verify the player's reveal against its commit.
        Should call `get_commit()` for that player first in order to verify.
        Raise error if failed to get reveal or verification fails.
        """
        reveal_msgs = await self._listen_reveal(pid, expect_count)
        cards = self._verify_played_cards(reveal_msgs)
        return cards

    async def _init_pid(self) -> None:
        n_player = len(self.consensus.node.player_list)
        self._order = await self.consensus.global_perm(list(range(n_player)))
        self.pid = self._player_id_to_pid(self.consensus.node.player_id)
        prev_pid = (self.pid - 1) if (self.pid > 0) else (n_player - 1)
        self.prev_player_id = self._pid_to_player_id(prev_pid)
        return

    def _pid_to_player_id(self, pid: int) -> str:
        idx = self._order.index(pid)
        return self.consensus.node.player_list[idx]

    def _player_id_to_pid(self, player_id: str) -> int:
        idx = self.consensus.node.player_list.index(player_id)
        return self._order[idx]

    def _generate_keys(self, n_cards: int) -> None:
        self._skey = gen_scalar_keypair()[0]
        self._tkeys = [
            gen_scalar_keypair()[0] for _ in range(n_cards)
        ]
        return

    def _broadcast_points(self, msg_type: str) -> None:
        self.consensus.node.broadcast({
            "type": msg_type,
            "points": [_point_to_json(p) for p in self._points]
        })
        return

    def _pass_points(self, msg_type: str) -> None:
        if self.pid is None:
            raise DealingError("PID not initialized")
        n_player = len(self.consensus.node.player_list)
        next_pid = (self.pid + 1) % n_player
        self.consensus.node.broadcast({
            "type": msg_type,
            "points": [_point_to_json(p) for p in self._points],
            "to": next_pid
        })
        return

    async def _listen_pass(self, msg_type: str) -> None:
        if self.pid is None or self.prev_player_id is None:
            raise DealingError("PID or prev_player_id not initialized")
        # TODO: from prev player or all players?
        while True:
            raw_msgs: List[RawMessage] = await self.consensus.node.consume_messages(
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
        self._points = [_json_to_point(p) for p in raw_msgs[0].payload["points"]]

    async def _listen_finaldeal(self) -> None:
        peers = self.consensus.node.peers()
        raw_msgs: List[RawMessage] = await self.consensus.node.consume_messages(
            msg_type=MSG_TYPE_FINALDEAL,
            from_players=peers,
            expected_count=1
        )
        if (len(raw_msgs) != 1):
            missing = set(peers) - {m.from_player for m in raw_msgs}
            raise DealingError(
                f"_listen_finaldeal: Missing finaldeal from: {missing}"
            )
        # TODO: use receive to verify?
        self._points = [_json_to_point(p) for p in raw_msgs[0].payload["points"]]

    async def _encrypt_shuffle(self, deck: List[int]) -> None:
        if self._skey is None:
            raise DealingError("skey not initialized")
        if self.pid == 0:
            self._points = [map_to_curve(card) for card in deck]
        else:
            await self._listen_pass(MSG_TYPE_SHUFFLE)

        for i in range(len(self._points)):
            self._points[i] = encrypt_point(self._points[i], self._skey)
        random.shuffle(self._points)

        self._pass_points(MSG_TYPE_SHUFFLE)
        return

    async def _tag(self):
        if self._skey is None:
            raise DealingError("skey not initialized")
        if self.pid == 0:
            await self._listen_pass(MSG_TYPE_SHUFFLE)
        else:
            await self._listen_pass(MSG_TYPE_TAG)

        for i in range(len(self._points)):
            self._points[i] = decrypt_point(self._points[i], self._skey)
            self._points[i] = encrypt_point(self._points[i], self._tkeys[i])

        self._pass_points(MSG_TYPE_TAG)
        return

    async def _detag(self, hand_size: int):
        if self.pid is None:
            raise DealingError("PID not initialized")
        if self.pid == 0:
            await self._listen_pass(MSG_TYPE_TAG)
        else:
            await self._listen_pass(MSG_TYPE_DETAG)

        n_player = len(self.consensus.node.player_list)
        for i in range(n_player * hand_size):
            if i in range(self.pid * hand_size, (self.pid + 1) * hand_size):
                continue
            self._points[i] = decrypt_point(self._points[i], self._tkeys[i])

        if self.pid == len(self.consensus.node.player_list) - 1:
            self._broadcast_points(MSG_TYPE_FINALDEAL)
        else:
            self._pass_points(MSG_TYPE_DETAG)
        return

    async def _decrypt_hand(self, hand_size: int):
        if self.pid is None:
            raise DealingError("PID not initialized")
        # TODO: last player also attend in consensus?
        if self.pid != len(self.consensus.node.player_list) - 1:
            await self._listen_finaldeal()
        for i in range(self.pid * hand_size, (self.pid + 1) * hand_size):
            point = decrypt_point(self._points[i], self._tkeys[i])
            self.hand.append(map_from_curve(point))
        return

    def _commit_played_card(self, card: int) -> bytes:
        # TODO: commit: hash(action | nonce | tkey)
        nonce = self.consensus.commitment.commit(
            action=card, commit_id=self._commit_count
        )
        self._commit_count += 1
        return nonce

    def _reveal_played_card(self, card: int, nonce: bytes,
                            commit_id: int) -> None:
        # TODO: reveal: card, nonce, tkey
        self.consensus.commitment.reveal(
            action=card, nonce=nonce, commit_id=commit_id
        )

    async def _listen_commit(self, pid: int, expect_count: int) -> List[RawMessage]:
        node = self.consensus.node
        from_player_id = self._pid_to_player_id(pid)
        commit_msgs: List[RawMessage] = await node.consume_messages(
            msg_type=MSG_TYPE_COMMIT,
            from_players=[from_player_id],
            expected_count=expect_count
        )
        if len(commit_msgs) != expect_count:
            raise PlayCardError(
                f"_listen_commit: Got {len(commit_msgs)} commits, "
                f"expect: {expect_count}"
            )
        return commit_msgs

    async def _listen_reveal(self, pid: int, expect_count: int) -> List[RawMessage]:
        node = self.consensus.node
        from_player_id = self._pid_to_player_id(pid)
        reveal_msgs: List[RawMessage] = await node.consume_messages(
            msg_type=MSG_TYPE_REVEAL,
            from_players=[from_player_id],
            expected_count=expect_count
        )
        if len(reveal_msgs) != expect_count:
            raise PlayCardError(
                f"_listen_reveal: Got {len(reveal_msgs)} reveal, "
                f"expect: {expect_count}"
            )
        return reveal_msgs

    def _verify_played_cards(self, reveal_msgs: List[RawMessage]) -> List[int]:
        # TODO: verify: card, nonce, tkey[i]
        #   (1) hash(card | nonce | tkey[i]) == commit
        #   (2) encrypt_point(map_to_curve(card), tkey[i]) == _points

        verified_actions: List[int] = []
        for msg in reveal_msgs:
            player_id = msg.from_player
            recv_action = msg.payload["action"]
            recv_nonce = bytes.fromhex(msg.payload["nonce"])
            commit_id = msg.payload["commit_id"]
            player_commit = self._commit_queue.get(player_id, {})
            expected_hash = player_commit.get(commit_id)
            if expected_hash is None:
                raise PlayCardError(
                    f"_verify_played_cards: no commit from "
                    f"player: '{player_id}', commit id: '{commit_id}'"
                )
            if not self.consensus.commitment.verify(
                recv_action, recv_nonce, expected_hash
            ): raise PlayCardError(
                    f"_verify_played_cards: player '{player_id}' revealed "
                    f"an action that does not match its commit"
                )
            verified_actions.append(recv_action)
        return verified_actions
