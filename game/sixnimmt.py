"""PeerPlay 6 nimmt! game logic.

What this module does
---------------------
- Keeps public table rows.
- Tracks scores.
- Validates and resolves simultaneous revealed plays.
- Implements the standard 6 nimmt! scoring and row-taking rules.

What this module does NOT do
----------------------------
- No network waits.
- No commit/reveal protocol.
- No dealing / mental poker.
- No room matchmaking.

Those responsibilities belong to other PeerPlay layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence
import random


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def card_horns(card: int) -> int:
    """Return the horn count of a 6 nimmt! card."""
    if card == 55:
        return 7
    if card % 11 == 0:
        return 5
    if card % 10 == 0:
        return 3
    if card % 5 == 0:
        return 2
    return 1


@dataclass
class PlayerState:
    name: str
    score: int = 0
    hand_count: int = 0


@dataclass
class RoundPlay:
    """One revealed play in a round."""

    player: str
    card: int
    chosen_row_on_no_fit: Optional[int] = None

@dataclass
class RoundRecord:
    """Canonical history."""
    round_index: int
    ordered_plays: List[RoundPlay]
    rows_before: List[List[int]]
    rows_after: List[List[int]]
    score_changes: Dict[str, int] = field(default_factory=dict)
    row_actions: Dict[str, Dict[str, object]] = field(default_factory=dict)


class SixNimmtGame:
    """State + rules engine for 6 nimmt!"""

    def __init__(
        self,
        player_list: Sequence[str],
        *,
        rng: Optional[random.Random] = None,
        my_player_id: Optional[str] = None,
    ) -> None:
        if len(player_list) < 2:
            raise ValueError("6 nimmt! requires at least 2 players.")
        if len(set(player_list)) != len(player_list):
            raise ValueError("player_list must contain unique player names.")

        self.player_list: List[str] = list(player_list)
        self.rng = rng or random.Random()
        self.my_player_id = my_player_id

        self.players: Dict[str, PlayerState] = {
            p: PlayerState(name=p) for p in self.player_list
        }
        self.rows: List[List[int]] = []
        self.turn_order: List[str] = list(self.player_list)
        self.round_index: int = 0
        self.history: List[RoundRecord] = []
        self._initialized: bool = False

        # Private/local-only state (for UI/testing on the local peer)
        self.my_hand: List[int] = []

    # ------------------------------------------------------------------ setup
    def reset(
        self,
        *,
        starter_rows: Sequence[int],
        turn_order: Optional[Sequence[str]] = None,
        hand_counts: Optional[Dict[str, int]] = None,
        my_hand: Optional[Sequence[int]] = None,
    ) -> None:
        """Initialize a new 6 nimmt! match from externally prepared cards.

        Parameters
        ----------
        starter_rows:
            The four face-up row starters.
        turn_order:
            Optional explicit turn order. If omitted, ``player_list`` order is
            used.
        hand_counts:
            Optional public counts for each player's remaining cards.
            This is useful in the P2P setting where only card counts, not card
            identities, are globally known.
        my_hand:
            Optional private local hand for the current node. This should only
            be set on the local player's node.
        """
        if len(starter_rows) != 4:
            raise ValueError("6 nimmt! needs exactly 4 starter rows.")
        if len(set(starter_rows)) != 4:
            raise ValueError("Starter rows must contain 4 distinct cards.")

        for p in self.players.values():
            p.score = 0
            p.hand_count = 0

        self.rows = [[card] for card in starter_rows]
        self.turn_order = list(turn_order) if turn_order is not None else list(self.player_list)
        if set(self.turn_order) != set(self.player_list) or len(self.turn_order) != len(self.player_list):
            raise ValueError("turn_order must be a permutation of player_list.")

        self.history.clear()
        self.round_index = 0
        self._initialized = True

        if hand_counts is not None:
            self.set_hand_counts(hand_counts)

        if my_hand is not None:
            self.set_my_hand(my_hand)

    def set_hand_counts(self, hand_counts: Dict[str, int]) -> None:
        """Set public remaining-card counts for each player."""
        missing = set(self.player_list) - set(hand_counts)
        extra = set(hand_counts) - set(self.player_list)
        if missing:
            raise ValueError(f"Missing hand counts for players: {sorted(missing)}")
        if extra:
            raise ValueError(f"Unknown players in hand_counts: {sorted(extra)}")

        for pname, count in hand_counts.items():
            if count < 0:
                raise ValueError(f"hand count for {pname} cannot be negative")
            self.players[pname].hand_count = count
    
    def set_my_hand(self, cards: Sequence[int]) -> None:
        """Store the local player's private hand for UI/testing."""
        if self.my_player_id is None:
            raise ValueError("my_player_id was not set on this game instance.")
        self.my_hand = sorted(cards)
        self.players[self.my_player_id].hand_count = len(self.my_hand)

    # ----------------------------------------------------------------- views
    def is_initialized(self) -> bool:
        return self._initialized

    def get_public_state(self) -> dict:
        return {
            "round_index": self.round_index,
            "rows": [row[:] for row in self.rows],
            "scores": {p.name: p.score for p in self.players.values()},
            "hand_counts": {p.name: p.hand_count for p in self.players.values()},
            "turn_order": self.turn_order[:],
        }

    def get_history(self) -> List[RoundRecord]:
        return list(self.history)

    def get_my_hand(self) -> List[int]:
        if self.my_player_id is None:
            raise ValueError("my_player_id was not set on this game instance.")
        return list(self.my_hand)

    def get_scores(self) -> Dict[str, int]:
        return {p.name: p.score for p in self.players.values()}

    # ----------------------------------------------------------------- helpers
    def _require_player(self, player: str) -> PlayerState:
        if player not in self.players:
            raise KeyError(f"Unknown player: {player}")
        return self.players[player]

    @staticmethod
    def _row_last(row: List[int]) -> int:
        if not row:
            raise ValueError("Invalid empty row state.")
        return row[-1]

    def _validate_card_in_hand(self, player: str, card: int) -> None:
        """Optional local-only validation.

        The actual ownership proof is expected to be enforced by the outer
        dealing / commitment / consensus layers.
        """
        if self.my_player_id is None or player != self.my_player_id:
            return
        if card not in self.my_hand:
            raise ValueError(f"Local player {player} does not have card {card} in hand.")

    def possible_rows_for_card(self, card: int) -> List[int]:
        """Rows that can accept *card* without taking a row."""
        return [i for i, row in enumerate(self.rows) if self._row_last(row) < card]

    def choose_row_to_take(self, card: int) -> int:
        """Choose a row to take when the played card fits none.

        The default heuristic is the row with the fewest horns.
        """
        row_scores = [sum(card_horns(c) for c in row) for row in self.rows]
        return min(range(len(self.rows)), key=lambda i: row_scores[i])

    def validate_play(self, player: str, card: int) -> bool:
        """Validate that a revealed play is locally consistent."""
        self._validate_card_in_hand(player, card)
        return True

    def suggest_play(self, player: str) -> int:
        """Small heuristic for local testing / simple AI."""
        if self.my_player_id is not None and player == self.my_player_id:
            hand = self.my_hand
        else:
            raise ValueError(
                "suggest_play() only has access to the local player's hand in PeerPlay mode."
            )

        if not hand:
            raise ValueError(f"Player {player} has no cards left.")

        best_card: Optional[int] = None
        best_cost: Optional[int] = None

        for card in hand:
            fits = self.possible_rows_for_card(card)
            if fits:
                row = max(fits, key=lambda i: self.rows[i][-1])
                cost = len(self.rows[row])
            else:
                row = self.choose_row_to_take(card)
                cost = 100 + sum(card_horns(c) for c in self.rows[row])

            if best_cost is None or cost < best_cost or (cost == best_cost and (best_card is None or card < best_card)):
                best_card = card
                best_cost = cost

        assert best_card is not None
        return best_card

    def _consume_local_card(self, player: str, card: int) -> None:
        """Remove a card from the local player's private hand if applicable."""
        if self.my_player_id is None or player != self.my_player_id:
            return
        try:
            self.my_hand.remove(card)
        except ValueError as exc:
            raise ValueError(f"Local player {player} does not have card {card} in hand.") from exc
        self.players[self.my_player_id].hand_count = len(self.my_hand)

    # -------------------------------------------------------------- round play
    def apply_verified_round(self, plays: Sequence[RoundPlay]) -> Dict[str, dict]:
        """Apply one fully verified simultaneous round.

        The caller is responsible for ensuring every play in *plays* has been
        verified by the outer protocol (commit/reveal, ownership, timeout rules,
        etc.).
        """
        if not self._initialized:
            raise RuntimeError("Call reset() before apply_verified_round().")

        if len(plays) != len(self.player_list):
            raise ValueError("Each player must submit exactly one play.")
        
        seen_players = set()
        seen_cards = set()
        for play in plays:
            if play.player in seen_players:
                raise ValueError(f"Duplicate play for player {play.player}.")
            seen_players.add(play.player)

            if play.card in seen_cards:
                raise ValueError(f"Duplicate card played in the same round: {play.card}.")
            seen_cards.add(play.card)
        
        # 6 nimmt! resolves in ascending card order.
        ordered = sorted(plays, key=lambda p: (p.card, p.player))
        rows_before = [row[:] for row in self.rows]
        round_result: Dict[str, dict] = {}
        score_changes: Dict[str, int] = {p: 0 for p in self.player_list}
        row_actions: Dict[str, Dict[str, object]] = {}

        for play in ordered:
            result = self._apply_play(play)
            round_result[play.player] = result
            score_changes[play.player] += int(result["score_added"])
            row_actions[play.player] = {
                "action": result["action"],
                "target_row": result["target_row"],
                "taken_cards": result["taken_cards"],
            }
        
        self.round_index += 1
        rows_after = [row[:] for row in self.rows]
        self.history.append(
            RoundRecord(
                round_index=self.round_index,
                ordered_plays=list(ordered),
                rows_before=rows_before,
                rows_after=rows_after,
                score_changes=score_changes,
                row_actions=row_actions,
            )
        )
        return round_result
    
    def resolve_round(self, plays: Sequence[RoundPlay]) -> Dict[str, dict]:
        return self.apply_verified_round(plays)

    def _apply_play(self, play: RoundPlay) -> dict:
        player = self._require_player(play.player)
        card = play.card
        rows_before = [row[:] for row in self.rows]

        self._consume_local_card(player.name, card)

        fitting = self.possible_rows_for_card(card)
        if fitting:
            target = max(fitting, key=lambda i: self.rows[i][-1])
            self.rows[target].append(card)

            taken_cards: List[int] = []
            score_added = 0
            if len(self.rows[target]) >= 6:
                taken_cards = self.rows[target][:5]
                score_added = sum(card_horns(c) for c in taken_cards)
                self.players[player.name].score += score_added
                self.rows[target] = [card]

            return {
                "card": card,
                "action": "placed",
                "target_row": target,
                "taken_cards": taken_cards,
                "score_added": score_added,
                "rows_before": rows_before,
                "rows_after": [row[:] for row in self.rows],
            }

        chosen_row = play.chosen_row_on_no_fit
        if chosen_row is None:
            raise ValueError(
                f"Player {player.name} played {card} with no fitting row; "
                "chosen_row_on_no_fit is required."
            )
        if not (0 <= chosen_row < len(self.rows)):
            raise ValueError(f"chosen_row_on_no_fit out of range: {chosen_row}")

        taken_cards = self.rows[chosen_row][:]
        score_added = sum(card_horns(c) for c in taken_cards)
        self.players[player.name].score += score_added
        self.rows[chosen_row] = [card]

        return {
            "card": card,
            "action": "took_row",
            "target_row": chosen_row,
            "taken_cards": taken_cards,
            "score_added": score_added,
            "rows_before": rows_before,
            "rows_after": [row[:] for row in self.rows],
        }

    # --------------------------------------------------------------- end state
    def is_game_over(self) -> bool:
        if not self._initialized:
            return False
        if not self.track_hands:
            return False
        return all(len(p.hand) == 0 for p in self.players.values())

    def finalize_scores(self) -> Dict[str, int]:
        return self.get_scores()

    def winner(self) -> List[str]:
        scores = self.get_scores()
        best = min(scores.values())
        return [p for p, s in scores.items() if s == best]


# ---------------------------------------------------------------------------
# Example usage for local testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    game = SixNimmtGame(["Alice", "Bob", "Carol", "Dave"], rng=random.Random(42))
    game.reset(
        starter_rows=[12, 25, 47, 88],
        hands={
            "Alice": [3, 14, 26, 39],
            "Bob": [4, 15, 27, 40],
            "Carol": [5, 16, 28, 41],
            "Dave": [6, 17, 29, 42],
        },
    )

    print("Initial public state:")
    print(game.get_public_state())

    plays = []
    for pname in game.turn_order:
        card = game.suggest_play(pname)
        plays.append(
            RoundPlay(
                player=pname,
                card=card,
                chosen_row_on_no_fit=game.choose_row_to_take(card),
            )
        )

    round_result = game.resolve_round(plays)
    print("\nRound result:")
    for p, r in round_result.items():
        print(p, r)

    print("\nState after round:")
    print(game.get_public_state())