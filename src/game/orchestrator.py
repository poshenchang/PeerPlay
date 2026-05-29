import asyncio
import copy
from typing import Dict, List, Any, Optional
from .sixnimmt import SixNimmtGame as LogicEngine, RoundPlay
from network import MSG_TYPE_READY, MSG_TYPE_CONSENSUS_START

class Orchestrator:
    """
    P2P Node Orchestrator for 6 nimmt!
    
    This class bridges the UI, Network, Dealing/Consensus crypto, and 
    the pure game logic state machine (game.sixnimmt.SixNimmtGame).
    """
    def __init__(self, network_node, consensus_module, dealing_module):
        self.node = network_node
        self.consensus = consensus_module
        self.dealing = dealing_module
        self.player_id = self.node.player_id
        
        self.players = self.node.player_list
        self.engine = LogicEngine(self.players, my_player_id=self.player_id)
        
        self.ui_notifier = None
        self._reset_round_state()

    def _reset_round_state(self):
        self.phase = "INITIALIZING"
        self.waiting_on_player: Optional[str] = None
        self.waiting_card: Optional[int] = None
        self.played_cards: Dict[str, int] = {}
        
        # State used during the resolution phase simulation
        self._temp_engine = None
        self._pending_items = []
        self._constructed_plays = []
        self._row_choice_future: Optional[asyncio.Future] = None

    def register_ui_notifier(self, callback_func):
        """Register a callback to notify the UI of events."""
        self.ui_notifier = callback_func

    def _notify_all(self, event_type: str, **kwargs):
        if self.ui_notifier:
            self.ui_notifier({"type": event_type, **kwargs})

    async def start_game(self):
        """Starts a new game, dealing cards and initializing the engine."""
        self._reset_round_state()
        self.phase = "DEALING"
        self._notify_all("ROUND_STARTED")

        # ── 同步點：等所有人的 Python 都初始化完畢後再開始 ──────────────────
        # 每 5 秒重新廣播一次 game_ready，讓晚載入的玩家也能收到
        # 一直等到所有 peers 都回應，或最多等 120 秒
        peers = self.node.peers()
        if peers:
            received_ready: set = set()
            deadline = asyncio.get_event_loop().time() + 120.0
            while len(received_ready) < len(peers):
                if asyncio.get_event_loop().time() >= deadline:
                    break
                self.node.broadcast({"type": MSG_TYPE_READY})
                still_waiting = [p for p in peers if p not in received_ready]
                msgs = await self.node.consume_messages(
                    msg_type=MSG_TYPE_READY,
                    from_players=still_waiting,
                    timeout=5.0,
                    expected_count=len(still_waiting),
                )
                for m in msgs:
                    received_ready.add(m.from_player)

        # 第二道同步屏障：所有人確認即將進入 consensus，確保大家同時開始
        # 同時繼續廣播 game_ready，讓還沒過第一道屏障的人能收到
        if peers:
            received_consensus: set = set()
            deadline2 = asyncio.get_event_loop().time() + 120.0
            while len(received_consensus) < len(peers):
                if asyncio.get_event_loop().time() >= deadline2:
                    break
                self.node.broadcast({"type": MSG_TYPE_READY})           # 繼續幫晚到的人
                self.node.broadcast({"type": MSG_TYPE_CONSENSUS_START})
                still_waiting2 = [p for p in peers if p not in received_consensus]
                msgs2 = await self.node.consume_messages(
                    msg_type=MSG_TYPE_CONSENSUS_START,
                    from_players=still_waiting2,
                    timeout=5.0,
                    expected_count=len(still_waiting2),
                )
                for m in msgs2:
                    received_consensus.add(m.from_player)

        deck = list(range(1, 105))
        
        # 1. Consensus on 4 table cards
        shuffled_deck = await self.consensus.global_perm(deck)
        table_cards = shuffled_deck[:4]
        remaining_deck = shuffled_deck[4:]
        
        # 2. Deal private hands — only pass the cards we actually need (40 cards)
        # to keep EC operations to minimum (mental poker encrypts every card)
        hand_size = 10
        n_players = len(self.players)
        needed = remaining_deck[:n_players * hand_size]  # 40 cards instead of 100
        self.pid, hand = await self.dealing.deal(needed, hand_size)
        
        # 3. Reset the engine with these values
        self.engine.reset(
            starter_rows=table_cards,
            hand_counts={p: 10 for p in self.players},
            my_hand=hand
        )
        
        self.phase = "WAITING_FOR_CARDS"
        self._notify_all("NEXT_TURN")

    def get_game_state(self) -> Dict[str, Any]:
        """Returns the current state for the UI to render."""
        initialized = self.engine.is_initialized()
        print(f"[get_game_state] is_initialized={initialized} phase={self.phase}")
        if not initialized:
            return {"phase": self.phase}
            
        state = self.engine.get_public_state()
        print(f"[get_game_state] rows count={len(state.get('rows', []))} rows={state.get('rows')}")
        # Add both key names so any cached JS version works
        state["table_rows"] = state["rows"]
        state["my_hand"] = self.engine.get_my_hand()
        state["phase"] = self.phase
        state["waiting_on"] = self.waiting_on_player
        
        # Track who has played this round
        state["played_statuses"] = {p: (p in self.played_cards) for p in self.players}
        return state

    async def receive_input(self, action_data: Dict[str, Any]):
        """Receives input actions from the UI."""
        action = action_data.get("action")
        
        if self.phase == "WAITING_FOR_CARDS" and action == "PLAY_CARD":
            await self._handle_play_card(action_data.get("card"))
        elif self.phase == "WAITING_FOR_ROW_SELECTION" and action == "CHOOSE_ROW":
            if self.player_id == self.waiting_on_player:
                await self._handle_choose_row(action_data.get("row_index"))
            else:
                self._notify_all("ERROR", message="Not your turn to choose a row")
        else:
            self._notify_all("ERROR", message=f"Invalid action {action} for phase {self.phase}")

    async def _handle_play_card(self, card: int):
        try:
            # Validate locally before hitting the network
            self.engine.validate_play(self.player_id, card)
        except ValueError as e:
            self._notify_all("ERROR", message=str(e))
            return

        self.played_cards[self.player_id] = card
        self._notify_all("WAITING_FOR_OTHERS")

        n_players = len(self.players)

        # Step 1: broadcast our commit (hash only, card hidden)
        commit_id = self.dealing.play_card(card)

        # Step 2: collect every enemy's commit before revealing anything
        for enemy_pid in range(n_players):
            if enemy_pid == self.pid:
                continue
            await self.dealing.get_commit(enemy_pid, 1)

        # Step 3: reveal our card now that everyone has committed
        self.dealing.reveal_card(commit_id)

        # Step 4: collect and verify every enemy's reveal
        for enemy_pid in range(n_players):
            if enemy_pid == self.pid:
                continue
            cards = await self.dealing.get_cards(enemy_pid, 1)
            enemy_player_id = self.dealing._pid_to_player_id(enemy_pid)
            self.played_cards[enemy_player_id] = cards[0]
            
        self.phase = "RESOLVING"
        self._notify_all("ALL_CARDS_REVEALED", cards=self.played_cards)
        
        # 3. Setup resolution simulation
        self._temp_engine = copy.deepcopy(self.engine)
        self._pending_items = sorted(self.played_cards.items(), key=lambda x: x[1])
        self._constructed_plays = []
        
        await self._process_resolving_queue()

    async def _process_resolving_queue(self):
        """Simulates the round to determine if anyone needs to pick a row."""
        print(f"[resolve] START pending={[(p,c) for p,c in self._pending_items]}")
        
        while self._pending_items:
            player_id, card = self._pending_items[0]
            
            fits = self._temp_engine.possible_rows_for_card(card)
            print(f"[resolve] player={player_id} card={card} fits={fits}")
            
            if not fits:
                self.phase = "WAITING_FOR_ROW_SELECTION"
                self.waiting_on_player = player_id
                self.waiting_card = card
                self._notify_all("ROW_SELECTION_REQUIRED", player=player_id, card=card)
                print(f"[resolve] ROW_SELECTION_REQUIRED for {player_id} card={card} (local={player_id == self.player_id})")
                
                if player_id != self.player_id:
                    # Enemy must choose. Await their broadcast via network.
                    print(f"[resolve] waiting for enemy {player_id} CHOOSE_ROW...")
                    msgs = await self.node.consume_messages(
                        msg_type="CHOOSE_ROW",
                        from_players=[player_id],
                        expected_count=1
                    )
                    chosen_row = msgs[0].payload["row_index"]
                    print(f"[resolve] enemy chose row={chosen_row}")
                else:
                    # Local player must choose. Wait for UI to call _handle_choose_row.
                    print(f"[resolve] waiting for LOCAL player to pick row (Future created)")
                    self._row_choice_future = asyncio.get_event_loop().create_future()
                    chosen_row = await self._row_choice_future
                    self._row_choice_future = None
                    print(f"[resolve] local player chose row={chosen_row}")
                await self._apply_row_choice(chosen_row)
                
            else:
                # It fits! No row selection needed.
                play = RoundPlay(player=player_id, card=card)
                self._temp_engine._apply_play(play)
                self._constructed_plays.append(play)
                self._pending_items.pop(0)

        print(f"[resolve] DONE. constructed_plays={[(p.player, p.card) for p in self._constructed_plays]}")
        # Simulation complete! We have all the RoundPlay objects with chosen rows.
        # Apply them to the real engine to officially advance the game state.
        round_result = self.engine.apply_verified_round(self._constructed_plays)
        # Build per-play data for UI animation (sorted ascending by card value)
        plays_data = []
        for play in sorted(self._constructed_plays, key=lambda p: p.card):
            r = round_result.get(play.player, {})
            plays_data.append({
                "player": play.player,
                "card": play.card,
                "target_row": r.get("target_row", 0),
                "action": r.get("action", "placed"),
                "score_added": r.get("score_added", 0),
            })
        self._notify_all("ROUND_RESOLVED", plays=plays_data, scores=self.engine.get_scores())
        
        self.played_cards = {}
        self.waiting_on_player = None
        self.waiting_card = None
        self._temp_engine = None
        self._pending_items = []
        self._constructed_plays = []

        # Wait for the UI's reveal animation (3.5 s) before firing NEXT_TURN
        await asyncio.sleep(4.0)
        
        if self.engine.is_game_over():
            self.phase = "GAME_OVER"
            self._notify_all("GAME_OVER", scores=self.engine.finalize_scores())
        else:
            self.phase = "WAITING_FOR_CARDS"
            self._notify_all("NEXT_TURN")

    async def _handle_choose_row(self, row_index: int):
        print(f"[choose_row] called row_index={row_index} phase={self.phase} future_exists={self._row_choice_future is not None}")
        if not (0 <= row_index < 4):
            self._notify_all("ERROR", message="Invalid row index")
            return

        # Broadcast choice to peers
        self.node.broadcast({
            "type": "CHOOSE_ROW",
            "row_index": row_index
        })
        
        # Resolve the future so _process_resolving_queue continues
        if self._row_choice_future and not self._row_choice_future.done():
            self._row_choice_future.set_result(row_index)
            print(f"[choose_row] Future resolved with row={row_index}")
        else:
            print(f"[choose_row] WARNING: future is None or already done! future={self._row_choice_future}")

    async def _apply_row_choice(self, row_index: int):
        player_id, card = self._pending_items.pop(0)
        
        # Advance simulation
        play = RoundPlay(player=player_id, card=card, chosen_row_on_no_fit=row_index)
        self._temp_engine._apply_play(play)
        self._constructed_plays.append(play)
        
        self.phase = "RESOLVING"
        # Do NOT call _process_resolving_queue here — the outer while loop continues naturally
