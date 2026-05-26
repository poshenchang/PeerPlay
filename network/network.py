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

MSG_TYPE_VAL: str = "val"       # validation / echo broadcast
MSG_TYPE_COMMIT: str = "commit" # commitment hash
MSG_TYPE_REVEAL: str = "reveal" # commitment reveal

DEFAULT_TIMEOUT: float = 10.0   # seconds to wait for peer responses
POLL_INTERVAL: float = 0.05     # seconds between queue polls

# ---------------------------------------------------------------------------
# Global State for JS Bridge
# ---------------------------------------------------------------------------
# 🌟 修正點 2：宣告一個全域變數來儲存目前的網路節點實體，避免動態修改 sys.modules
global_network_node = None

def receive_from_network(sender_id: str, json_str: str) -> None:
    """
    [全域函式] 讓 JS 透過 Pyodide 直接呼叫的入口。
    必須放在模組最外層，window.pyodide.globals 才能抓到。
    """
    if global_network_node is not None:
        global_network_node.push_to_queue(sender_id, json_str)
        js.js_append_log(f"[Python] 收到來自 {sender_id} 的封包！內容: {json_str}")
    else:
        js.js_append_log("[Python 錯誤] 網路節點尚未初始化，無法接收封包", "error")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RawMessage:
    """A raw frame as stored in ``msg_queue``."""
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
        self.msg_queue: List[RawMessage] = []
        
        # 🌟 修正點 3：初始化時將自己綁定到全域變數，供 JS 呼叫口使用
        global global_network_node
        global_network_node = self

    # ------------------------------------------------------------------
    # JS Bridge (內部寫入)
    # ------------------------------------------------------------------

    def push_to_queue(self, from_player: str, json_str: str) -> None:
        try:
            payload = json.loads(json_str)
            self.msg_queue.append(
                RawMessage(from_player=from_player, payload=payload)
            )
        except Exception as e:
            js.js_append_log(f"[Python 錯誤] 封包解析失敗: {str(e)}", "error")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
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
        # 🌟 呼叫綁定在 window 上的 JS 函式
        js.js_send_to_network(json_str)

    async def receive(
        self,
        sender: str,
        rcv_msg: Any,
        timeout: Optional[float] = None,
    ) -> Tuple[str, Any]:
        """
        Verify *rcv_msg* (claimed to be from *sender*) via peer cross-check
        and return the majority-agreed real message.

        Algorithm
        ---------
        1. Broadcast a **validation frame** carrying our copy of the message::

               { type: "val",
                 correlation_id: <uuid>,   # ties votes to this receive() call
                 original_sender: sender,
                 content: rcv_msg }

        2. Poll ``msg_queue`` for validation frames from other peers that
           share the same ``correlation_id``.  Stop when we have a majority
           or timeout expires.

        3. Majority vote over all collected ``content`` values (including
           our own) determines ``real_msg``.

        4. Return ``(sender, real_msg)``.

        Timeout handling
        ----------------
        If fewer than a majority of peers respond before *timeout* seconds,
        we proceed with the votes we have.  Callers may treat this as a
        warning (peer may be offline or Byzantine).

        Parameters
        ----------
        sender:
            Player ID of the node whose broadcast we're validating.
        rcv_msg:
            The message content we received from *sender*.
        timeout:
            Override the node-level default timeout for this call.

        Returns
        -------
        (sender, real_msg):
            *sender* is unchanged; *real_msg* is the majority-agreed content.

        Raises
        ------
        RuntimeError:
            If no votes could be collected at all (should not happen in a
            healthy network).
        """
        if timeout is None:
            timeout = self.timeout

        correlation_id = str(uuid.uuid4())
        val_payload: Dict[str, Any] = {
            "type": MSG_TYPE_VAL,
            "correlation_id": correlation_id,
            "original_sender": sender,
            "content": rcv_msg,
        }
        self.broadcast(val_payload)

        peers = set(self.player_list) - {self.player_id}
        majority_threshold = len(self.player_list) // 2 + 1
        votes: Dict[str, Any] = {self.player_id: rcv_msg}

        deadline = time.time() + timeout

        while time.time() < deadline:
            if len(votes) >= majority_threshold:
                break

            matching: List[RawMessage] = []
            remaining: List[RawMessage] = []
            
            for msg in self.msg_queue:
                p = msg.payload
                is_val = p.get("type") == MSG_TYPE_VAL
                same_corr = p.get("correlation_id") == correlation_id
                same_orig = p.get("original_sender") == sender
                new_voter = msg.from_player not in votes
                known_peer = msg.from_player in peers

                if is_val and same_corr and same_orig and new_voter and known_peer:
                    matching.append(msg)
                else:
                    remaining.append(msg) 

            for msg in matching:
                votes[msg.from_player] = msg.payload.get("content")

            self.msg_queue = remaining 

            if not matching:
                await asyncio.sleep(POLL_INTERVAL) 

        if not votes:
            raise RuntimeError(f"receive({sender!r}): no votes collected")

        def _to_key(v: Any) -> str:
            try:
                return json.dumps(v, sort_keys=True) 
            except Exception:
                return repr(v)

        counter: Counter = Counter(_to_key(v) for v in votes.values())
        winner_key, winner_count = counter.most_common(1)[0]

        if winner_count < majority_threshold:
            js.js_append_log(f"[警告] receive({sender!r}): 只有 {winner_count}/{len(self.player_list)} 同意 — 可能有作弊者", "error")

        real_msg: Any = next(v for v in votes.values() if _to_key(v) == winner_key)
        return (sender, real_msg)

    async def consume_messages(
        self,
        msg_type: str,
        from_players: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        expected_count: Optional[int] = None,
    ) -> List[RawMessage]:
        """
        Block until *expected_count* messages of *msg_type* arrive from
        *from_players* (or timeout), then return and remove them from the queue.

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

            remaining: List[RawMessage] = []
            for msg in self.msg_queue:
                is_type = msg.payload.get("type") == msg_type
                in_whitelist = msg.from_player in target_set
                not_seen = msg.from_player not in seen_from

                if is_type and in_whitelist and not_seen:
                    collected.append(msg)
                    seen_from.add(msg.from_player)
                else:
                    remaining.append(msg)

            self.msg_queue = remaining
            await asyncio.sleep(POLL_INTERVAL)

        return collected

    def peers(self) -> List[str]:
        """Return all players except ourselves."""
        return [p for p in self.player_list if p != self.player_id]
    
# ... 前面的程式碼保持不變 ...


import js
js.python_receive_from_network = receive_from_network