import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Optional
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
        # 為了避免 Log 洗頻，可以考慮將底下的 Log 註解掉，或只在 Debug 時開啟
        # js.appendLog(f"[Python] 收到來自 {sender_id} 的封包！")
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
        
        # 存放已經通過共識、準備給業務層 Consume 的合法封包
        self.msg_buffers: Dict[str, List[RawMessage]] = {}
        
        # [防作弊共識機制] 存放正在等待多數決的封包
        # 格式: { msg_id: { "reporters": { reporter_id: payload_json_str }, "timestamp": float } }
        self.forward_pool: Dict[str, Dict[str, Any]] = {}
        
        # [防作弊共識機制] 記錄已經完成共識的 msg_id，避免重複處理
        # 格式: { msg_id: timestamp }
        self.processed_msgs: Dict[str, float] = {}
        
        global global_network_node
        global_network_node = self

    def _handle_resolved_payload(self, original_sender: str, payload: dict) -> None:
        """當封包達成共識後，正式將其推入 Buffers 供 consume_messages 取用"""
        msg_type = payload.get("type", "default")
        if msg_type not in self.msg_buffers:
            self.msg_buffers[msg_type] = []
        
        self.msg_buffers[msg_type].append(
            RawMessage(from_player=original_sender, payload=payload)
        )

    def _record_forward(self, msg_id: str, original_sender: str, reporter: str, payload: dict) -> None:
        """記錄轉發資訊並檢查是否達成多數決共識"""
        # 發送者不需要對「自己發送的封包」參與共識
        if original_sender == self.player_id:
            return

        current_time = time.time()

        if msg_id in self.processed_msgs:
            return  # 該封包已經決議過，忽略

        if msg_id not in self.forward_pool:
            self.forward_pool[msg_id] = {
                "reporters": {},
                "timestamp": current_time
            }

        # 將 Payload 轉為字串以進行一致性比對 (Sort keys 確保順序不影響結果)
        payload_str = json.dumps(payload, sort_keys=True)
        self.forward_pool[msg_id]["reporters"][reporter] = payload_str

        # 檢查多數決 (Majority Consensus)
        counts = Counter(self.forward_pool[msg_id]["reporters"].values())
        
        # 系統總人數 - 1 (不包含發送者自己) 就是我們期待收到回報的 Peer 數量
        num_peers = len(self.player_list) - 1
        majority_threshold = (num_peers // 2) + 1  # 例如: 4人遊戲 -> 3 Peers -> 門檻為 2

        for p_str, count in counts.items():
            if count >= majority_threshold:
                # 達成共識！
                self.processed_msgs[msg_id] = current_time
                resolved_payload = json.loads(p_str)
                self._handle_resolved_payload(original_sender, resolved_payload)
                
                # 清理記憶體
                del self.forward_pool[msg_id]
                break

    def push_to_queue(self, from_player: str, json_str: str) -> None:
        try:
            data = json.loads(json_str)

            # 兼容舊版或不具備 tag 的系統訊息
            tag = data.get("tag")
            if not tag:
                self._handle_resolved_payload(from_player, data)
                return

            sender = data.get("sender")

            if tag == "broadcast":
                # msg = {sender: A, tag: "broadcast", message: payload}
                payload = data.get("message", {})
                msg_id = payload.get("msg_id")

                if not msg_id:
                    return

                # 1. 轉發給其他人 (包含 {self, forward, msg})
                forward_msg = {
                    "sender": self.player_id,
                    "tag": "forward",
                    "message": data
                }
                js.js_send_to_network(json.dumps(forward_msg))

                # 2. 將原始發送者的廣播也視為一票，加入我們自己的 Forward Pool
                self._record_forward(msg_id, original_sender=sender, reporter=sender, payload=payload)

            elif tag == "forward":
                # data = {sender: B, tag: "forward", message: {sender: A, tag: "broadcast", message: payload}}
                reporter = sender
                inner_msg = data.get("message", {})
                original_sender = inner_msg.get("sender")
                payload = inner_msg.get("message", {})
                msg_id = payload.get("msg_id")

                if not msg_id:
                    return

                # 將轉發者的票加入 Forward Pool
                self._record_forward(msg_id, original_sender=original_sender, reporter=reporter, payload=payload)

        except Exception as e:
            js.appendLog(f"[Python 錯誤] 封包解析失敗: {str(e)}", "error")

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """
        Send *payload* to every peer.
        底層自動封裝 tag 與防作弊機制，對上層完全透明。
        """
        payload["from"] = self.player_id
        
        # 注入唯一識別碼，確保轉發追蹤
        if "msg_id" not in payload:
            payload["msg_id"] = str(uuid.uuid4())

        # 封裝成廣播格式: {sender, tag, message}
        wrapper = {
            "sender": self.player_id,
            "tag": "broadcast",
            "message": payload
        }
        
        json_str = json.dumps(wrapper)
        js.js_send_to_network(json_str)

    async def consume_messages(
        self,
        msg_type: str,
        from_players: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        expected_count: Optional[int] = None,
    ) -> List[RawMessage]:
        
        if timeout is None:
            timeout = self.timeout
        if from_players is None:
            from_players = [p for p in self.player_list if p != self.player_id]

        target_set = set(from_players)
        collected: List[RawMessage] = []
        seen_from: set = set()

        deadline = time.time() + timeout

        while time.time() < deadline:
            current_time = time.time()
            
            # --- 垃圾回收機制 (GC) ---
            # 1. 歷遍清理所有合法的 Buffers (防止惡意塞入未知 type 的記憶體攻擊)
            for m_type in list(self.msg_buffers.keys()):
                self.msg_buffers[m_type] = [
                    m for m in self.msg_buffers[m_type]
                    if current_time - m.timestamp <= MAX_LIFETIME
                ]
                # 順手清掉空的 key，節省空間
                if not self.msg_buffers[m_type]:
                    del self.msg_buffers[m_type]
            
            # 2. 清理過期的 Forward Pool (防 Memory Leak)
            expired_forwards = [
                m_id for m_id, pool_data in self.forward_pool.items() 
                if current_time - pool_data["timestamp"] > MAX_LIFETIME
            ]
            for m_id in expired_forwards:
                del self.forward_pool[m_id]

            # 3. 清理過期的 Processed Cache
            expired_processed = [
                m_id for m_id, ts in self.processed_msgs.items()
                if current_time - ts > MAX_LIFETIME
            ]
            for m_id in expired_processed:
                del self.processed_msgs[m_id]
            # -------------------------

            # 提取已經達成共識的合法訊息
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
        return [p for p in self.player_list if p != self.player_id]

js.python_receive_from_network = receive_from_network