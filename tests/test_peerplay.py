"""
PeerPlay Unit Tests
===================
Run with:  python -m pytest tests/test_peerplay.py -v
       or: python tests/test_peerplay.py
"""

import sys, os, asyncio, json, unittest
from unittest.mock import MagicMock, patch

# ── point Python at src/ ─────────────────────────────────────────────────────
SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

# ── stub out `js` before any src import ──────────────────────────────────────
js_stub = MagicMock()
sys.modules['js'] = js_stub
sys.modules['pyodide'] = MagicMock()
sys.modules['pyodide.ffi'] = MagicMock()


# =============================================================================
# Helpers
# =============================================================================

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_node(player_id, player_list):
    from network import NetworkNode
    return NetworkNode(player_id=player_id, player_list=player_list)


# =============================================================================
# 1. SixNimmtGame – pure logic, no network
# =============================================================================

class TestSixNimmtGame(unittest.TestCase):

    def setUp(self):
        from game.sixnimmt import SixNimmtGame, RoundPlay
        self.Game = SixNimmtGame
        self.RoundPlay = RoundPlay
        self.players = ['Alice', 'Bob', 'Carol', 'Dave']

    def _fresh_game(self):
        g = self.Game(self.players, my_player_id='Alice')
        g.reset(
            starter_rows=[10, 20, 30, 40],
            hand_counts={p: 10 for p in self.players},
            my_hand=[1, 2, 3, 4, 5, 6, 7, 8, 9, 11],
        )
        return g

    def test_initial_state(self):
        """reset() must produce 4 rows, each with exactly one card."""
        g = self._fresh_game()
        self.assertTrue(g.is_initialized())
        self.assertEqual(len(g.rows), 4)
        for row in g.rows:
            self.assertEqual(len(row), 1)

    def test_initial_rows_correct_cards(self):
        """Starter cards must appear on the board."""
        g = self._fresh_game()
        first_cards = [row[0] for row in g.rows]
        self.assertEqual(sorted(first_cards), [10, 20, 30, 40])

    def test_my_hand_set(self):
        """Local hand is stored and accessible."""
        g = self._fresh_game()
        self.assertEqual(len(g.get_my_hand()), 10)

    def test_validate_play_ok(self):
        """Card in hand must validate without error."""
        g = self._fresh_game()
        self.assertTrue(g.validate_play('Alice', 1))

    def test_validate_play_not_in_hand(self):
        """Card NOT in hand must raise ValueError."""
        g = self._fresh_game()
        with self.assertRaises(ValueError):
            g.validate_play('Alice', 99)  # 99 is not in Alice's hand

    def test_possible_rows_for_card(self):
        """Card 15 fits rows starting with 10; not rows 20, 30, 40."""
        g = self._fresh_game()
        fits = g.possible_rows_for_card(15)
        self.assertIn(0, fits)   # row 0 starts with 10 < 15
        self.assertNotIn(1, fits)  # row 1 starts with 20 > 15

    def test_apply_round_advances_rows(self):
        """apply_verified_round() must append cards to correct rows."""
        g = self._fresh_game()
        plays = [
            self.RoundPlay('Alice', 11),  # fits after 10
            self.RoundPlay('Bob',   21),  # fits after 20
            self.RoundPlay('Carol', 31),  # fits after 30
            self.RoundPlay('Dave',  41),  # fits after 40
        ]
        g.apply_verified_round(plays)
        # Each row should now have 2 cards
        for row in g.rows:
            self.assertEqual(len(row), 2)

    def test_take_row_on_no_fit(self):
        """Card smaller than all rows forces player to take a row (score penalty)."""
        g = self._fresh_game()
        # Play card 5 which is smaller than all row tops (10,20,30,40)
        plays = [
            self.RoundPlay('Alice', 5, chosen_row_on_no_fit=0),  # takes row 0
            self.RoundPlay('Bob',   21),
            self.RoundPlay('Carol', 31),
            self.RoundPlay('Dave',  41),
        ]
        g.apply_verified_round(plays)
        scores = g.get_scores()
        # Alice took row [10] → 1 horn (10 is not a multiple of 5/10/11)
        self.assertGreater(scores['Alice'], 0)
        self.assertEqual(scores['Bob'], 0)

    def test_game_over_when_hands_empty(self):
        """is_game_over() is True only when every player has 0 hand_count."""
        g = self._fresh_game()
        self.assertFalse(g.is_game_over())
        # Drain hands via hand_count directly
        for p in g.players.values():
            p.hand_count = 0
        self.assertTrue(g.is_game_over())

    def test_duplicate_card_in_round_raises(self):
        """Two players cannot play the same card in one round."""
        g = self._fresh_game()
        plays = [
            self.RoundPlay('Alice', 11),
            self.RoundPlay('Bob',   11),  # duplicate!
            self.RoundPlay('Carol', 31),
            self.RoundPlay('Dave',  41),
        ]
        with self.assertRaises(ValueError):
            g.apply_verified_round(plays)

    def test_reset_needs_4_starter_rows(self):
        """reset() must reject starters != 4."""
        g = self.Game(self.players)
        with self.assertRaises(ValueError):
            g.reset(starter_rows=[1, 2, 3])


# =============================================================================
# 2. NetworkNode – message queue
# =============================================================================

class TestNetworkNode(unittest.TestCase):

    def setUp(self):
        from network import NetworkNode
        self.NodeClass = NetworkNode
        self.players = ['Alice', 'Bob', 'Carol', 'Dave']

    def _node(self, pid):
        return self.NodeClass(player_id=pid, player_list=self.players)

    def test_player_not_in_list_raises(self):
        from network import NetworkNode
        with self.assertRaises(ValueError):
            NetworkNode(player_id='Eve', player_list=self.players)

    def test_push_and_consume_single_message(self):
        node = self._node('Alice')
        node.push_to_queue('Bob', json.dumps({'type': 'commit', 'hash': 'abc'}))
        msgs = run(node.consume_messages(
            msg_type='commit', from_players=['Bob'],
            expected_count=1, timeout=1.0
        ))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].from_player, 'Bob')
        self.assertEqual(msgs[0].payload['hash'], 'abc')

    def test_consume_ignores_wrong_type(self):
        node = self._node('Alice')
        node.push_to_queue('Bob', json.dumps({'type': 'reveal', 'action': 5}))
        msgs = run(node.consume_messages(
            msg_type='commit', from_players=['Bob'],
            expected_count=1, timeout=0.1
        ))
        self.assertEqual(len(msgs), 0, "Wrong-type message must not be returned")

    def test_consume_ignores_wrong_sender(self):
        node = self._node('Alice')
        node.push_to_queue('Carol', json.dumps({'type': 'commit', 'hash': 'xyz'}))
        msgs = run(node.consume_messages(
            msg_type='commit', from_players=['Bob'],  # expecting Bob, not Carol
            expected_count=1, timeout=0.1
        ))
        self.assertEqual(len(msgs), 0, "Wrong-sender message must not be returned")

    def test_push_malformed_json_does_not_crash(self):
        node = self._node('Alice')
        # Should not raise; bad JSON is silently dropped
        node.push_to_queue('Bob', 'NOT JSON {{{')

    def test_broadcast_calls_js(self):
        node = self._node('Alice')
        node.broadcast({'type': 'game_ready'})
        js_stub.js_send_to_network.assert_called()

    def test_peers_excludes_self(self):
        node = self._node('Alice')
        peers = node.peers()
        self.assertNotIn('Alice', peers)
        self.assertEqual(sorted(peers), ['Bob', 'Carol', 'Dave'])

    def test_consume_multiple_players(self):
        """Messages from Bob and Carol both collected in one consume call."""
        node = self._node('Alice')
        node.push_to_queue('Bob',   json.dumps({'type': 'game_ready'}))
        node.push_to_queue('Carol', json.dumps({'type': 'game_ready'}))
        msgs = run(node.consume_messages(
            msg_type='game_ready',
            from_players=['Bob', 'Carol'],
            expected_count=2, timeout=1.0
        ))
        self.assertEqual(len(msgs), 2)


# =============================================================================
# 3. CommitmentModule – commit-reveal integrity
# =============================================================================

class TestCommitmentModule(unittest.TestCase):

    def setUp(self):
        from network import NetworkNode
        from commitment import CommitmentModule
        self.node = NetworkNode(player_id='Alice', player_list=['Alice', 'Bob'])
        self.cm = CommitmentModule(self.node)

    def test_commit_returns_bytes(self):
        nonce = self.cm.commit(42)
        self.assertIsInstance(nonce, bytes)
        self.assertEqual(len(nonce), 32)

    def test_commit_broadcasts(self):
        js_stub.reset_mock()
        self.cm.commit(42)
        js_stub.js_send_to_network.assert_called_once()

    def test_verify_correct(self):
        nonce = self.cm.commit(42)
        # Reconstruct hash manually
        from utils.crypto import hash_concat
        expected_hash = hash_concat(42, nonce)
        self.assertTrue(self.cm.verify(42, nonce, expected_hash))

    def test_verify_wrong_action_fails(self):
        nonce = self.cm.commit(42)
        from utils.crypto import hash_concat
        expected_hash = hash_concat(42, nonce)
        # Wrong action
        self.assertFalse(self.cm.verify(99, nonce, expected_hash))

    def test_verify_wrong_nonce_fails(self):
        nonce = self.cm.commit(42)
        from utils.crypto import hash_concat
        expected_hash = hash_concat(42, nonce)
        import os
        bad_nonce = os.urandom(32)
        self.assertFalse(self.cm.verify(42, bad_nonce, expected_hash))


# =============================================================================
# 4. ConsensusModule – seed consistency
# =============================================================================

class TestConsensusModule(unittest.TestCase):
    """
    Two-player consensus: Alice and Bob each run get_global_seed() concurrently.
    They must arrive at the same seed.
    """

    def _make_pair(self):
        from network import NetworkNode
        from commitment import CommitmentModule
        from consensus import ConsensusModule

        players = ['Alice', 'Bob']
        alice_node = NetworkNode('Alice', players)
        bob_node   = NetworkNode('Bob',   players)
        alice_cm   = CommitmentModule(alice_node)
        bob_cm     = CommitmentModule(bob_node)
        alice_cs   = ConsensusModule(alice_node, alice_cm, timeout=2.0)
        bob_cs     = ConsensusModule(bob_node,   bob_cm,   timeout=2.0)
        return alice_node, bob_node, alice_cs, bob_cs

    def test_both_arrive_at_same_seed(self):
        """Commit-reveal must produce identical seed on both sides."""
        alice_node, bob_node, alice_cs, bob_cs = self._make_pair()

        # Wire nodes: intercept broadcast and deliver to the other node
        def make_deliver(sender_node, receiver_node):
            original_broadcast = sender_node.broadcast
            def patched(payload):
                payload['from'] = sender_node.player_id
                receiver_node.push_to_queue(sender_node.player_id, json.dumps(payload))
                # Also call original so js_stub doesn't complain
                original_broadcast(payload)
            sender_node.broadcast = patched

        make_deliver(alice_node, bob_node)
        make_deliver(bob_node, alice_node)

        seeds = {}
        async def run_consensus(cs, name):
            seeds[name] = await cs.get_global_seed()

        async def both():
            await asyncio.gather(
                run_consensus(alice_cs, 'Alice'),
                run_consensus(bob_cs,   'Bob'),
            )

        run(both())
        self.assertEqual(seeds['Alice'], seeds['Bob'],
            f"Seeds differ! Alice={seeds.get('Alice')}, Bob={seeds.get('Bob')}")

    def test_global_perm_same_order(self):
        """global_perm must return items in the same order for all players."""
        alice_node, bob_node, alice_cs, bob_cs = self._make_pair()

        def make_deliver(sender_node, receiver_node):
            original_broadcast = sender_node.broadcast
            def patched(payload):
                payload['from'] = sender_node.player_id
                receiver_node.push_to_queue(sender_node.player_id, json.dumps(payload))
                original_broadcast(payload)
            sender_node.broadcast = patched

        make_deliver(alice_node, bob_node)
        make_deliver(bob_node, alice_node)

        perms = {}
        deck  = list(range(1, 21))

        async def run_perm(cs, name):
            perms[name] = await cs.global_perm(list(deck))

        async def both():
            await asyncio.gather(
                run_perm(alice_cs, 'Alice'),
                run_perm(bob_cs,   'Bob'),
            )

        run(both())
        self.assertIsNotNone(perms.get('Alice'))
        self.assertEqual(perms['Alice'], perms['Bob'],
            "global_perm produced different orders for Alice and Bob!")
        # Verify all original cards are present
        self.assertEqual(sorted(perms['Alice']), deck)


# =============================================================================
# 5. Orchestrator – play card flow (mocked dealing)
# =============================================================================

class TestOrchestratorFlow(unittest.TestCase):
    """
    Test the orchestrator with a mocked DealingModule so we don't need
    the full mental poker protocol.
    """

    def _make_orchestrator(self, player_id='Alice',
                           players=None, my_hand=None, table_cards=None):
        if players is None:
            players = ['Alice', 'Bob', 'Carol', 'Dave']
        if my_hand is None:
            my_hand = [11, 21, 31, 41, 51, 61, 71, 81, 91, 101]
        if table_cards is None:
            table_cards = [10, 20, 30, 40]

        from network import NetworkNode
        from commitment import CommitmentModule
        from consensus import ConsensusModule
        from game.orchestrator import Orchestrator

        node = NetworkNode(player_id=player_id, player_list=players)

        # Mock DealingModule entirely
        dealing = MagicMock()
        pid = players.index(player_id)
        dealing.pid = pid
        dealing.deal = MagicMock(return_value=asyncio.coroutine(
            lambda *a, **kw: (pid, my_hand))())

        def make_coro(ret):
            async def _inner(*a, **kw): return ret
            return _inner

        dealing.play_card    = MagicMock(return_value=0)
        dealing.reveal_card  = MagicMock()
        dealing.get_commit   = MagicMock(side_effect=make_coro(None))

        # get_cards returns one card per enemy pid
        enemy_cards = [x * 10 + 2 for x in range(1, len(players))]
        call_count = [0]
        async def mock_get_cards(pid, count):
            idx = call_count[0]
            call_count[0] += 1
            return [enemy_cards[idx % len(enemy_cards)]]
        dealing.get_cards = mock_get_cards

        def pid_to_player_id(pid):
            return players[pid]
        dealing._pid_to_player_id = pid_to_player_id

        # Mock ConsensusModule
        consensus = MagicMock()
        async def mock_global_perm(deck):
            return deck  # no shuffle for determinism
        consensus.global_perm = mock_global_perm

        # Wire consensus into dealing mock (needed for _init_pid inside dealing)
        consensus.node = node
        dealing.consensus = consensus

        orch = Orchestrator(node, consensus, dealing)
        orch.dealing = dealing

        # Pre-initialize the engine directly (bypass async start_game consensus)
        orch.engine.reset(
            starter_rows=table_cards,
            hand_counts={p: 10 for p in players},
            my_hand=my_hand,
        )
        orch.phase = "WAITING_FOR_CARDS"
        orch.pid = pid

        return orch, node

    # ── state access ─────────────────────────────────────────────────────────

    def test_get_game_state_has_my_hand(self):
        """get_game_state() must include my_hand when initialized."""
        orch, _ = self._make_orchestrator()
        state = orch.get_game_state()
        self.assertIn('my_hand', state, "get_game_state() missing 'my_hand'")
        self.assertEqual(len(state['my_hand']), 10)

    def test_get_game_state_has_table_rows(self):
        """get_game_state() must include table_rows."""
        orch, _ = self._make_orchestrator()
        state = orch.get_game_state()
        # sixnimmt returns 'rows', orchestrator maps to 'table_rows'? check key
        self.assertTrue(
            'rows' in state or 'table_rows' in state,
            f"Neither 'rows' nor 'table_rows' in state: {list(state.keys())}"
        )

    def test_game_state_not_initialized_before_start(self):
        """Before engine.reset(), get_game_state() must not crash."""
        from network import NetworkNode
        from game.orchestrator import Orchestrator
        node = NetworkNode('Alice', ['Alice', 'Bob'])
        orch = Orchestrator(node, MagicMock(), MagicMock())
        state = orch.get_game_state()
        self.assertNotIn('my_hand', state)

    # ── notifier ─────────────────────────────────────────────────────────────

    def test_register_ui_notifier_called_on_event(self):
        """_notify_all() must invoke the registered notifier."""
        orch, _ = self._make_orchestrator()
        events = []
        orch.register_ui_notifier(lambda ev: events.append(ev))
        orch._notify_all("TEST_EVENT", foo="bar")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'TEST_EVENT')
        self.assertEqual(events[0]['foo'], 'bar')

    # ── play card ─────────────────────────────────────────────────────────────

    def test_play_card_invalid_card_fires_error(self):
        """Playing a card not in hand must fire ERROR, not crash."""
        orch, _ = self._make_orchestrator()
        events = []
        orch.register_ui_notifier(lambda ev: events.append(ev))
        run(orch.receive_input({'action': 'PLAY_CARD', 'card': 99}))
        error_events = [e for e in events if e['type'] == 'ERROR']
        self.assertTrue(len(error_events) > 0,
            "Expected ERROR event for invalid card, got: " + str(events))

    def test_play_card_valid_broadcasts_commit(self):
        """Playing a valid card must call dealing.play_card (broadcasts commit)."""
        orch, _ = self._make_orchestrator()
        orch.register_ui_notifier(lambda ev: None)
        run(orch.receive_input({'action': 'PLAY_CARD', 'card': 11}))
        orch.dealing.play_card.assert_called_once_with(11)

    def test_play_card_collects_commits_before_reveal(self):
        """
        CRITICAL: get_commit() must be called for every enemy BEFORE
        reveal_card() is called.
        """
        call_order = []
        orch, _ = self._make_orchestrator()
        orch.register_ui_notifier(lambda ev: None)

        original_get_commit   = orch.dealing.get_commit.side_effect
        original_reveal_card  = orch.dealing.reveal_card

        async def tracked_get_commit(pid, count):
            call_order.append(f'get_commit({pid})')

        def tracked_reveal(commit_id):
            call_order.append('reveal_card')

        orch.dealing.get_commit.side_effect = tracked_get_commit
        orch.dealing.reveal_card            = tracked_reveal

        run(orch.receive_input({'action': 'PLAY_CARD', 'card': 11}))

        # All get_commit calls must come before reveal_card
        reveal_idx = call_order.index('reveal_card') if 'reveal_card' in call_order else -1
        self.assertGreater(reveal_idx, 0,
            "reveal_card was not called: " + str(call_order))
        for entry in call_order[:reveal_idx]:
            self.assertTrue(entry.startswith('get_commit'),
                f"Non-commit call before reveal_card: {call_order}")

    def test_all_cards_revealed_event_fired(self):
        """ALL_CARDS_REVEALED event must be fired after collecting all cards."""
        orch, _ = self._make_orchestrator()
        events = []
        orch.register_ui_notifier(lambda ev: events.append(ev))
        run(orch.receive_input({'action': 'PLAY_CARD', 'card': 11}))
        types = [e['type'] for e in events]
        self.assertIn('ALL_CARDS_REVEALED', types,
            "Expected ALL_CARDS_REVEALED, got: " + str(types))

    def test_next_turn_eventually_fired(self):
        """After a complete round, NEXT_TURN must be fired (unless game over)."""
        orch, _ = self._make_orchestrator()
        events = []
        orch.register_ui_notifier(lambda ev: events.append(ev))
        run(orch.receive_input({'action': 'PLAY_CARD', 'card': 11}))
        types = [e['type'] for e in events]
        self.assertTrue(
            'NEXT_TURN' in types or 'GAME_OVER' in types,
            "Expected NEXT_TURN or GAME_OVER after round, got: " + str(types)
        )

    def test_wrong_action_in_wrong_phase_fires_error(self):
        """CHOOSE_ROW in WAITING_FOR_CARDS phase must fire ERROR."""
        orch, _ = self._make_orchestrator()
        events = []
        orch.register_ui_notifier(lambda ev: events.append(ev))
        run(orch.receive_input({'action': 'CHOOSE_ROW', 'row_index': 0}))
        error_events = [e for e in events if e['type'] == 'ERROR']
        self.assertTrue(len(error_events) > 0)


# =============================================================================
# 6. network.py JS bridge: receive_from_network
# =============================================================================

class TestNetworkBridge(unittest.TestCase):

    def test_receive_from_network_pushes_to_queue(self):
        """receive_from_network() must push message to global_network_node queue."""
        from network import NetworkNode, receive_from_network
        import network.network as net_mod

        node = NetworkNode('Alice', ['Alice', 'Bob'])
        # receive_from_network uses global_network_node set in NetworkNode.__init__
        self.assertIs(net_mod.global_network_node, node)

        msg = json.dumps({'type': 'commit', 'hash': 'deadbeef'})
        receive_from_network('Bob', msg)

        msgs = run(node.consume_messages(
            msg_type='commit', from_players=['Bob'],
            expected_count=1, timeout=1.0
        ))
        self.assertEqual(len(msgs), 1,
            "receive_from_network did not push message to queue!")

    def test_receive_from_network_without_node_does_not_crash(self):
        """Calling receive_from_network before any node exists must not crash."""
        import network.network as net_mod
        original = net_mod.global_network_node
        net_mod.global_network_node = None
        try:
            from network import receive_from_network
            receive_from_network('Bob', json.dumps({'type': 'commit'}))
            # Should just log, not raise
        finally:
            net_mod.global_network_node = original


# =============================================================================
# 7. Crypto helpers
# =============================================================================

class TestCrypto(unittest.TestCase):

    def test_map_to_curve_and_back(self):
        """map_to_curve + map_from_curve must be identity for any card 1-104."""
        from utils.crypto import map_to_curve, map_from_curve
        for card in [1, 5, 55, 100, 104]:
            point = map_to_curve(card)
            recovered = map_from_curve(point)
            self.assertEqual(recovered, card, f"Round-trip failed for card {card}")

    def test_encrypt_decrypt_single_key(self):
        """encrypt_point + decrypt_point with same key must recover original."""
        from utils.crypto import (map_to_curve, map_from_curve,
                                   encrypt_point, decrypt_point,
                                   gen_scalar_keypair)
        card = 42
        point = map_to_curve(card)
        k, _ = gen_scalar_keypair()
        enc = encrypt_point(point, k)
        dec = decrypt_point(enc, k)
        self.assertEqual(map_from_curve(dec), card)

    def test_commutative_encryption(self):
        """k_B(k_A(M)) must be decryptable in reverse order k_A(k_B(C))."""
        from utils.crypto import (map_to_curve, map_from_curve,
                                   encrypt_point, decrypt_point,
                                   gen_scalar_keypair)
        card = 77
        m = map_to_curve(card)
        ka, _ = gen_scalar_keypair()
        kb, _ = gen_scalar_keypair()

        enc_ab = encrypt_point(encrypt_point(m, ka), kb)
        # Decrypt in opposite order
        dec = decrypt_point(decrypt_point(enc_ab, kb), ka)
        self.assertEqual(map_from_curve(dec), card)

    def test_hash_concat_deterministic(self):
        """hash_concat must produce the same result for same inputs."""
        from utils.crypto import hash_concat
        import os
        nonce = os.urandom(32)
        h1 = hash_concat(42, nonce)
        h2 = hash_concat(42, nonce)
        self.assertEqual(h1, h2)

    def test_hash_concat_different_actions_differ(self):
        from utils.crypto import hash_concat
        import os
        nonce = os.urandom(32)
        self.assertNotEqual(hash_concat(42, nonce), hash_concat(43, nonce))


# =============================================================================
# Main
# =============================================================================

if __name__ == '__main__':
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestSixNimmtGame,
        TestNetworkNode,
        TestCommitmentModule,
        TestConsensusModule,
        TestOrchestratorFlow,
        TestNetworkBridge,
        TestCrypto,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
