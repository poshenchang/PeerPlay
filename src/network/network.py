import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter
from dataclasses import dataclass, field
import js  # 載入 JS 環境

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MSG_TYPE_VAL: str = "val"                   # validation / echo broadcast
MSG_TYPE_COMMIT: str = "commit"             # commitment hash
MSG_TYPE_REVEAL: str = "reveal"             # commitment reveal
MSG_TYPE_SHUFFLE: str = "shuffle"           # used when dealing
MSG_TYPE_TAG: str = "tag"                   # used when dealing
MSG_TYPE_DETAG: str = "detag"               # used when dealing
MSG_TYPE_FINALDEAL: str = "finaldeal"       # used when dealing, for verification
MSG_TYPE_READY: str = "game_ready"          # all players Python loaded, ready to start
MSG_TYPE_CONSENSUS_START: str = "consensus_start"  # second barrier: all players about to enter consensus

DEFAULT_TIMEOUT: float = 10.0               # seconds to wait for peer responses
POLL_INTERVAL: float = 0.05                 # seconds between queue polls
MAX_LIFETIME: float = 300.0                 # 封包最大存活時間，防止 Memory Leak

# ---------------------------------------------------------------------------
# Global State for JS Bridge
# ---------------------------------------------------------------------------
global_network_node = None

def receive_from_network(sender_id: str, json_str: str) -> None:
    if global_network_node is not None:
        global_network_node.push_to_queue(sender_id, json_str)
        js.appendLog(f"[Python] 收到來自 {sender_id} 的封包！內容: {json_str}")
    else:
        js.appendLog("[Python 錯誤] 網路節點尚未初始化，無法接收封包", "error")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RawMessage:
    from_player: str          
    payload: Dict[str, Any]   
    timestamp: float = field(default_factory=time.time)

# ---------------------------------------------------------------------------
# NetworkNode
# ---------------------------------------------------------------------------

class NetworkNode:
    def __init__(
        self,
        player_id: str,
        player_list: List[str],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if player_id not in player_list:
            raise ValueError(f"player_id '{player_id}' must be in player_list")

        self.player_id: str = player_id
        self.player_list: List[str] = sorted(player_list)
        self.timeout: float = timeout
        self.msg_buffers: Dict[str, List[RawMessage]] = {}
        
        global global_network_node
        global_network_node = self

    def push_to_queue(self, from_player: str, json_str: str) -> None:
        try:
            payload = json.loads(json_str)
            msg_type = payload.get("type", "default")
            if msg_type not in self.msg_buffers:
                self.msg_buffers[msg_type] = []
            self.msg_buffers[msg_type].append(
                RawMessage(from_player=from_player, payload=payload)
            )
        except Exception as e:
            js.appendLog(f"[Python 錯誤] 封包解析失敗: {str(e)}", "error")

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """
        Send *payload* to every peer (all players except ourselves).

        Parameters
        ----------
        payload:
            Arbitrary JSON-serialisable dict.  A ``from`` field is
            automatically injected so recipients know the origin.
        """
        payload["from"] = self.player_id
        json_str = json.dumps(payload)
        js.js_send_to_network(json_str)

    async def consume_messages(
        self,
        msg_type: str,
        from_players: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        expected_count: Optional[int] = None,
    ) -> List[RawMessage]:
        """
        Block until *expected_count* messages of *msg_type* arrive from
        *from_players* (or timeout), then return and remove them from that
        tagged buffer.

        Used internally by CommitmentModule / ConsensusModule to collect
        commit and reveal frames without having to replicate the polling loop.

        Parameters
        ----------
        msg_type:
            Value of payload["type"] to match.
        from_players:
            Whitelist of sender IDs to accept.  ``None`` means all peers.
        timeout:
            Seconds to wait.  Defaults to ``self.timeout``.
        expected_count:
            Stop early when this many messages have been collected.
            ``None`` means wait until timeout.
        """
        if timeout is None:
            timeout = self.timeout
        if from_players is None:
            from_players = [p for p in self.player_list if p != self.player_id]

        target_set = set(from_players)
        collected: List[RawMessage] = []
        seen_from: set = set()

        deadline = time.time() + timeout

        while time.time() < deadline:
            if expected_count is not None and len(collected) >= expected_count:
                break

            current_time = time.time()
            
            self.msg_buffers[msg_type] = [
                m for m in self.msg_buffers.get(msg_type, [])
                if current_time - m.timestamp <= MAX_LIFETIME
            ]

            for msg in list(self.msg_buffers.get(msg_type, [])):
                in_whitelist = msg.from_player in target_set
                not_seen = msg.from_player not in seen_from

                if in_whitelist and not_seen:
                    collected.append(msg)
                    seen_from.add(msg.from_player)
                    self.msg_buffers[msg_type].remove(msg)

                    if expected_count is not None and len(collected) >= expected_count:
                        break

            if expected_count is not None and len(collected) >= expected_count:
                break

            await asyncio.sleep(POLL_INTERVAL)

        return collected

    def peers(self) -> List[str]:
        """Return all players except ourselves."""
        return [p for p in self.player_list if p != self.player_id]

js.python_receive_from_network = receive_from_network