import asyncio
import copy
from typing import Dict, List, Any, Optional
from .sixnimmt import SixNimmtGame as LogicEngine, RoundPlay

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
        
        deck = list(range(1, 105))
        
        # 1. Consensus on 4 table cards
        shuffled_deck = await self.consensus.global_perm(deck)
        table_cards = shuffled_deck[:4]
        remaining_deck = shuffled_deck[4:]
        
        # 2. Deal private hands
        self.pid, hand = await self.dealing.deal(remaining_deck, 10)
        
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
        if not self.engine.is_initialized():
            return {"phase": self.phase}
            
        state = self.engine.get_public_state()
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
        
        # 1. Commit and reveal immediately
        self.dealing.play_card(card)
        self.dealing.reveal_card(card)
        
        # 2. Collect enemy cards via dealing module
        n_players = len(self.players)
        
        for enemy_pid in range(n_players):
            if enemy_pid == self.pid:
                continue
            
            # get_cards blocks until commit and reveal are received and verified
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
        
        while self._pending_items:
            player_id, card = self._pending_items[0]
            
            fits = self._temp_engine.possible_rows_for_card(card)
            
            if not fits:
                self.phase = "WAITING_FOR_ROW_SELECTION"
                self.waiting_on_player = player_id
                self.waiting_card = card
                self._notify_all("ROW_SELECTION_REQUIRED", player=player_id, card=card)
                
                if player_id != self.player_id:
                    # Enemy must choose. Await their broadcast via network.
                    msgs = await self.node.consume_messages(
                        msg_type="CHOOSE_ROW",
                        from_players=[player_id],
                        expected_count=1
                    )
                    chosen_row = msgs[0].payload["row_index"]
                    await self._apply_row_choice(chosen_row)
                return # Yield to wait for UI or network
                
            else:
                # It fits! No row selection needed.
                play = RoundPlay(player=player_id, card=card)
                self._temp_engine._apply_play(play)
                self._constructed_plays.append(play)
                self._pending_items.pop(0)

        # Simulation complete! We have all the RoundPlay objects with chosen rows.
        # Apply them to the real engine to officially advance the game state.
        self.engine.apply_verified_round(self._constructed_plays)
        
        self.played_cards = {}
        self.waiting_on_player = None
        self.waiting_card = None
        self._temp_engine = None
        self._pending_items = []
        self._constructed_plays = []
        
        if self.engine.is_game_over():
            self.phase = "GAME_OVER"
            self._notify_all("GAME_OVER", scores=self.engine.finalize_scores())
        else:
            self.phase = "WAITING_FOR_CARDS"
            self._notify_all("NEXT_TURN")

    async def _handle_choose_row(self, row_index: int):
        if not (0 <= row_index < 4):
            self._notify_all("ERROR", message="Invalid row index")
            return

        # Broadcast choice to peers
        self.node.broadcast({
            "type": "CHOOSE_ROW",
            "row_index": row_index
        })
        
        await self._apply_row_choice(row_index)

    async def _apply_row_choice(self, row_index: int):
        player_id, card = self._pending_items.pop(0)
        
        # Advance simulation
        play = RoundPlay(player=player_id, card=card, chosen_row_on_no_fit=row_index)
        self._temp_engine._apply_play(play)
        self._constructed_plays.append(play)
        
        self.phase = "RESOLVING"
        await self._process_resolving_queue()
