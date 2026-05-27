import sys
import asyncio
import json
from typing import List, Dict, Any

# Setup js environment handling for both Pyodide and local testing
try:
    import js
    from pyodide.ffi import create_proxy
except ImportError:
    # Fallback for local testing outside of Pyodide
    class JS:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None
    js = JS()
    
    def create_proxy(obj):
        return obj

from network import NetworkNode
from commitment import CommitmentModule
from consensus import ConsensusModule
from dealing import DealingModule
from game import Orchestrator

# Global orchestrator instance
game_orchestrator = None

def init_game(player_id: str, player_list_json: str) -> None:
    """
    Called by Trystero/JS when the room is fully connected and ready.
    """
    global game_orchestrator
    
    # Parse player list if it's passed as a JSON string
    try:
        player_list = json.loads(player_list_json)
    except Exception:
        player_list = player_list_json if isinstance(player_list_json, list) else []

    print(f"[Python] Initializing game for player {player_id} with players {player_list}")
    
    # 1. Initialize Network Layer
    node = NetworkNode(player_id=player_id, player_list=list(player_list))
    
    # 2. Initialize Crypto Layers
    commitment = CommitmentModule(node)
    consensus = ConsensusModule(node, commitment)
    dealing = DealingModule(consensus)
    
    # 3. Initialize Game Orchestrator
    game_orchestrator = Orchestrator(node, consensus, dealing)
    
    print("[Python] Game initialized successfully.")

async def receive_input(action_data_json: str) -> None:
    """
    Wrapper for JS to call async Python functions easily.
    """
    global game_orchestrator
    if not game_orchestrator:
        print("[Python Error] Game not initialized yet.")
        return
        
    action_data = json.loads(action_data_json)
    await game_orchestrator.receive_input(action_data)

async def start_game() -> None:
    """
    Wrapper for JS to start the game.
    """
    global game_orchestrator
    if not game_orchestrator:
        print("[Python Error] Game not initialized yet.")
        return
        
    await game_orchestrator.start_game()

def get_game_state() -> str:
    """
    Wrapper for JS to get the game state as a JSON string.
    """
    global game_orchestrator
    if not game_orchestrator:
        return "{}"
        
    state = game_orchestrator.get_game_state()
    return json.dumps(state)

def register_ui_notifier(callback) -> None:
    """
    JS passes a Javascript function here. We wrap it in create_proxy 
    (required by Pyodide) so Python can call it safely, and we pass 
    event data back to JS as a JSON string.
    """
    global game_orchestrator
    if not game_orchestrator:
        print("[Python Error] Call init_game before registering notifier.")
        return
        
    def wrapped_callback(event_data):
        # Convert the python dictionary to a JSON string for JS
        callback(json.dumps(event_data))
        
    proxy = create_proxy(wrapped_callback)
    game_orchestrator.register_ui_notifier(proxy)

# -----------------------------------------------------------------
# Export the Python functions to the global JS scope
# so the UI / Trystero layer can easily invoke them.
# -----------------------------------------------------------------
js.python_init_game = init_game
js.python_receive_input = receive_input
js.python_start_game = start_game
js.python_get_game_state = get_game_state
js.python_register_ui_notifier = register_ui_notifier

print("[Python] main.py loaded. Exposed game functions to JS.")
