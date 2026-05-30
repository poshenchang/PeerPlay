"""
4-Player Integration Test
=========================
Tests that all 4 players, running start_game() concurrently with real network
wiring, end up with:
  1. Identical table rows (same 4 starter cards, same order)
  2. Disjoint private hands (no card appears in 2+ hands)
  3. No overlap between hands and table rows
  4. Correct total card count (4 table + 4×10 hands = 44 from deck 1-104)

Run with:  python3.10 tests/test_4player_integration.py
"""

import sys, os, asyncio, json, unittest
from unittest.mock import MagicMock

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

# Stub `js` module before any src import
js_stub = MagicMock()
sys.modules['js']          = js_stub
sys.modules['pyodide']     = MagicMock()
sys.modules['pyodide.ffi'] = MagicMock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def wire_all(nodes):
    """
    Patch every node's broadcast() so that messages are delivered
    to all OTHER nodes' queues — simulating a real P2P network.
    """
    original_broadcasts = {n.player_id: n.broadcast for n in nodes}
    node_map = {n.player_id: n for n in nodes}

    for sender in nodes:
        def make_patched(sender_node):
            def patched_broadcast(payload):
                payload = dict(payload)  # shallow copy so we can mutate
                payload['from'] = sender_node.player_id
                json_str = json.dumps(payload)
                for receiver in nodes:
                    if receiver.player_id != sender_node.player_id:
                        receiver.push_to_queue(sender_node.player_id, json_str)
                # Still call JS stub so js_stub.js_send_to_network is satisfied
                original_broadcasts[sender_node.player_id](payload)
            return patched_broadcast
        sender.broadcast = make_patched(sender)


def make_4player_setup():
    """
    Create 4 fully wired Orchestrators with real Consensus and Dealing.
    Returns list of (orchestrator, node) tuples.
    """
    from network     import NetworkNode
    from commitment  import CommitmentModule
    from consensus   import ConsensusModule
    from dealing     import DealingModule
    from game.orchestrator import Orchestrator

    players = ['Alice', 'Bob', 'Carol', 'Dave']

    nodes      = [NetworkNode(p, players) for p in players]
    wire_all(nodes)

    result = []
    for node in nodes:
        cm   = CommitmentModule(node)
        cs   = ConsensusModule(node, cm, timeout=10.0)
        dm   = DealingModule(cs)
        orch = Orchestrator(node, cs, dm)
        result.append(orch)

    return result


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class Test4PlayerStartGame(unittest.TestCase):

    def setUp(self):
        """Run start_game() for all 4 players concurrently and collect results."""
        self.orchestrators = make_4player_setup()
        self.table_rows_per_player = {}
        self.hands_per_player      = {}

        async def run_all():
            async def start_and_collect(orch):
                await orch.start_game()
                state = orch.get_game_state()
                pid = orch.player_id
                self.table_rows_per_player[pid] = state['rows']
                self.hands_per_player[pid]      = state['my_hand']

            await asyncio.gather(*[start_and_collect(o) for o in self.orchestrators])

        run(run_all())

    # ── table rows ───────────────────────────────────────────────────────────

    def test_all_players_see_same_table_rows(self):
        """All 4 players must see identical table rows after start_game()."""
        rows_list = list(self.table_rows_per_player.values())
        first = rows_list[0]
        for pid, rows in self.table_rows_per_player.items():
            self.assertEqual(
                rows, first,
                f"{pid} has different table rows!\n"
                f"  Expected: {first}\n"
                f"  Got:      {rows}"
            )

    def test_table_has_exactly_4_rows(self):
        """There must be exactly 4 rows on the table."""
        for pid, rows in self.table_rows_per_player.items():
            self.assertEqual(len(rows), 4,
                f"{pid}: expected 4 rows, got {len(rows)}: {rows}")

    def test_each_row_has_exactly_one_starter_card(self):
        """Each row starts with exactly 1 card."""
        for pid, rows in self.table_rows_per_player.items():
            for i, row in enumerate(rows):
                self.assertEqual(len(row), 1,
                    f"{pid} row {i} should have 1 card, has: {row}")

    def test_starter_cards_are_valid(self):
        """All 4 starter cards are in range 1-104."""
        rows = list(self.table_rows_per_player.values())[0]
        starter_cards = [row[0] for row in rows]
        for c in starter_cards:
            self.assertIn(c, range(1, 105),
                f"Starter card {c} is outside 1-104")

    def test_all_starter_cards_distinct(self):
        """The 4 starter cards must all be different."""
        rows = list(self.table_rows_per_player.values())[0]
        starter_cards = [row[0] for row in rows]
        self.assertEqual(len(set(starter_cards)), 4,
            f"Duplicate starter cards: {starter_cards}")

    # ── hands ────────────────────────────────────────────────────────────────

    def test_each_player_has_10_cards(self):
        """Every player must receive exactly 10 cards."""
        for pid, hand in self.hands_per_player.items():
            self.assertEqual(len(hand), 10,
                f"{pid} has {len(hand)} cards, expected 10: {hand}")

    def test_hands_are_disjoint(self):
        """No card may appear in more than one player's hand."""
        all_cards = []
        for pid, hand in self.hands_per_player.items():
            all_cards.extend(hand)
        duplicates = [c for c in set(all_cards) if all_cards.count(c) > 1]
        self.assertEqual(duplicates, [],
            f"Cards appear in multiple hands: {duplicates}\n"
            f"Hands: {self.hands_per_player}")

    def test_hands_contain_valid_cards(self):
        """All dealt cards are in range 1-104."""
        for pid, hand in self.hands_per_player.items():
            for c in hand:
                self.assertIn(c, range(1, 105),
                    f"{pid} received invalid card {c}")

    def test_no_overlap_between_hands_and_table(self):
        """Cards on the table must not appear in any player's hand."""
        rows = list(self.table_rows_per_player.values())[0]
        table_cards = set(row[0] for row in rows)
        for pid, hand in self.hands_per_player.items():
            overlap = table_cards & set(hand)
            self.assertEqual(overlap, set(),
                f"{pid}'s hand overlaps with table: {overlap}")

    def test_total_cards_accounted_for(self):
        """4 table cards + 4×10 hand cards = 44 unique cards from 1-104."""
        rows = list(self.table_rows_per_player.values())[0]
        table_cards = [row[0] for row in rows]
        all_hand_cards = []
        for hand in self.hands_per_player.values():
            all_hand_cards.extend(hand)

        all_dealt = table_cards + all_hand_cards
        self.assertEqual(len(all_dealt), 44,
            f"Expected 44 cards total, got {len(all_dealt)}")
        self.assertEqual(len(set(all_dealt)), 44,
            "Duplicate cards found across table + all hands!")

    # ── game state ───────────────────────────────────────────────────────────

    def test_all_players_in_waiting_for_cards_phase(self):
        """After start_game(), every player must be in WAITING_FOR_CARDS phase."""
        for orch in self.orchestrators:
            state = orch.get_game_state()
            self.assertEqual(state.get('phase'), 'WAITING_FOR_CARDS',
                f"{orch.player_id} phase is '{state.get('phase')}', "
                f"expected 'WAITING_FOR_CARDS'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromTestCase(Test4PlayerStartGame)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
