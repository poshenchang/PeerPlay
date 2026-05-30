/**
 * ============================================================================
 * [外部依賴 External Dependencies] - 前端組員請注意 ⚠️
 * ============================================================================
 * 這份網路模組會呼叫以下掛載在 `window` 上的方法，前端必須實作它們才能正常連動：
 * * 1. window.appendLog(msg: string, type: 'system' | 'error')
 * - 說明：顯示系統訊息或錯誤日誌在畫面上。
 * * 2. window.python_receive_from_network(peerId: string, jsonStr: string)
 * - 說明：接收來自其他玩家的遊戲操作或資料（只有在 Python 準備好後才會觸發）。
 * * 3. window.onRoomFull(selfId: string, players: string[])
 * - 說明：房間滿 4 人時觸發。此時前端應切換至「遊戲開始」畫面並初始化 Python。
 * - 參數：players 是包含 4 個字串的陣列 (自己與其他 3 人的 ID)。
 * * 4. window.onRoomDisband()
 * - 說明：房間因有人斷線且超時未歸而解散時觸發（僅限配對中或 Python 尚未載入完成時）。
 * * 5. window.onRoomJoinFailed(reason: string)
 * - 說明：指定加入特定房間失敗時觸發（例如房間已滿）。
 * * 6. window.onGameInterrupted() <-- [新增]
 * - 說明：遊戲正式開始後（Python已就緒）有人斷線觸發。預設行為會直接重新整理網頁。
 * ============================================================================
 */

import { joinRoom, selfId } from 'https://cdn.jsdelivr.net/npm/@trystero-p2p/mqtt/+esm';

// ============================================================================
// [內部狀態 Internal State] - 僅供網路層內部使用
// ============================================================================
let room;
let rawAction;
let sysAction;
let pythonCoreReady = false;
const MAX_PLAYERS = 4;

// 狀態控制
let currentRoomIdx = 1;
let myPeers = new Set();
let isRoomFull = false;
let isJoiningRoom = false;
let isJumping = false;
let isRandomMode = true; 
let activeAppId = null;  

// ============================================================================
// [公開介面 Public API] - 給前端或 Python 呼叫的方法
// ============================================================================

/**
 * @public
 * @description 初始化網路連線，開始尋找/加入房間。
 * @param {string} appId - 應用程式的唯一識別碼
 * @param {string} [targetRoomId=null] - 若傳入特定房號，則進入「指定房間模式」；若為空，則進入「隨機配對模式」。
 * @returns {string} 回傳本機玩家的專屬 ID (selfId)
 */
export function initNetwork(appId, targetRoomId = null) {
  window.js_send_to_network = sendToNetwork;
  activeAppId = appId;
  
  if (targetRoomId) {
    isRandomMode = false;
    searchAndJoinRoom(targetRoomId);
  } else {
    isRandomMode = true;
    currentRoomIdx = 1; 
    searchAndJoinRoom(`room_${currentRoomIdx}`);
  }
  
  return selfId;
}

/**
 * @public
 * @description 發送資料給房間內的所有其他玩家。
 * @param {string} jsonStr - 要發送的 JSON 格式字串
 */
export function sendToNetwork(jsonStr) {
  if (!rawAction || isJumping) return;
  rawAction.send(jsonStr);
}

/**
 * @public
 * @description 標記 Python 核心已完全就緒。這代表遊戲「正式開始」。
 */
export function setPythonReady() {
  pythonCoreReady = true;
}


// ============================================================================
// [內部輔助函數 Internal Helpers] - 負責處理房間配對、斷線重連與解散邏輯
// ============================================================================

/**
 * @internal
 * @description 尋找並加入 Trystero MQTT 房間的核心邏輯
 */
function searchAndJoinRoom(roomId) {
  if (isJoiningRoom) return; 
  isJoiningRoom = true;
  isJumping = false;
  
  myPeers.clear();
  isRoomFull = false;
  pythonCoreReady = false; // 每次進新房間重置狀態

  if(window.appendLog) window.appendLog(`[系統] 嘗試進入房間 ${roomId}...`, 'system');

  room = joinRoom({ 
    appId: activeAppId,
    relayConfig: { urls: ['wss://broker.emqx.io:8084/mqtt', 'wss://broker.hivemq.com:8884/mqtt'] },
    rtcConfig: { iceServers: [
      { urls: "stun:stun1.l.google.com:19302" },
      { urls: "stun:stun.relay.metered.ca:80" },
      {
        urls: "turn:global.relay.metered.ca:80",
        username: "04e809e8efce89e0b9ab6e97",
        credential: "EJc5HOWbi8M1bjne",
      },
      {
        urls: "turn:global.relay.metered.ca:80?transport=tcp",
        username: "04e809e8efce89e0b9ab6e97",
        credential: "EJc5HOWbi8M1bjne",
      },
      {
        urls: "turn:global.relay.metered.ca:443",
        username: "04e809e8efce89e0b9ab6e97",
        credential: "EJc5HOWbi8M1bjne",
      },
      {
        urls: "turns:global.relay.metered.ca:443?transport=tcp",
        username: "04e809e8efce89e0b9ab6e97",
        credential: "EJc5HOWbi8M1bjne",
      },
      ],
    }
  }, roomId);

  // --- 內部通道設定 ---

  rawAction = room.makeAction('rawJsonPayload');
  rawAction.onMessage = (jsonStr, { peerId }) => {
    if (isJumping) return; 
    if (pythonCoreReady && window.python_receive_from_network) {
      window.python_receive_from_network(peerId, jsonStr);
    }
  };

  sysAction = room.makeAction('sysInfo');
  sysAction.onMessage = (msg, { peerId }) => {
    if (isJumping) return; 
    if (msg.type === 'REJECT' && !isRoomFull) {
      if (isRandomMode) {
        if(window.appendLog) window.appendLog(`[系統] 此房間已經客滿，自動跳轉至下一間...`, 'error');
        jumpToNextRoom();
      } else {
        if(window.appendLog) window.appendLog(`[系統] 指定房間 ${roomId} 已客滿或遊戲已開始！`, 'error');
        if(window.onRoomJoinFailed) window.onRoomJoinFailed('ROOM_FULL');
        
        isJumping = true;
        if (room) { room.leave(); room = null; }
      }
    }
  };

  // --- 內部連線生命週期管理 ---

  let disbandTimeout = null;

  room.onPeerJoin = peerId => {
    if (isJumping) return; 

    if (isRoomFull) {
      sysAction.send({ type: 'REJECT' }, { target: peerId });
      return; 
    }

    myPeers.add(peerId);
    if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 加入。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    
    if (myPeers.size + 1 === MAX_PLAYERS) {
      isRoomFull = true;

      if (disbandTimeout) {
        clearTimeout(disbandTimeout);
        disbandTimeout = null;
        if(window.appendLog) window.appendLog(`[系統] 房間已重新滿員，危機解除！`, 'system');
        
        if (window.onRoomFull) {
          window.onRoomFull(selfId, [selfId, ...Array.from(myPeers)]);
        }
      } else {
        if(window.appendLog) window.appendLog(`[系統] 房間已滿 4 人！準備啟動應用程式...`, 'system');
        if (window.onRoomFull) {
          window.onRoomFull(selfId, [selfId, ...Array.from(myPeers)]);
        }
      }
    }
  };

  room.onPeerLeave = peerId => {
    if (isJumping) return; 
    myPeers.delete(peerId);

    if (isRoomFull) {
      // ==========================================
      // [核心修改] 判斷遊戲是否已正式開始
      // ==========================================
      if (pythonCoreReady) {
        // 遊戲已經開始，不給寬限期，直接強制全體重整
        if (window.appendLog) window.appendLog(`[系統] 致命錯誤：玩家 ${peerId.substring(0, 6)} 於遊戲中斷線，強制終止遊戲！`, 'error');
        
        isJumping = true; // 上鎖，停止後續封包處理
        if (room) { room.leave(); room = null; }

        if (window.onGameInterrupted) {
          window.onGameInterrupted();
        } else {
          // 如果前端沒有實作這個方法，預設行為就是直接重整網頁
          setTimeout(() => {
            alert('有玩家於遊戲中斷線，遊戲無法繼續，將重新整理頁面。');
            window.location.reload();
          }, 500); 
        }
        return;
      }

      // 尚未正式開始 (例如在剛滿員但 Python 還在載入的等待期) -> 給予 3 秒寬限期
      isRoomFull = false; 
      if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 網路閃斷，給予 3 秒重連時間...`, 'error');

      if (!disbandTimeout) {
        disbandTimeout = setTimeout(() => {
          if(window.appendLog) window.appendLog(`[系統] 玩家超時未歸，強制解散！`, 'error');
          disbandRoom();
          disbandTimeout = null;
        }, 3000);
      }
    } else {
      // 房間本來就沒滿，單純有人進出
      if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 離開。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    }
  };
  
  isJoiningRoom = false;
}

/**
 * @internal
 */
function jumpToNextRoom() {
  if (isRoomFull || isJumping) return; 
  isJumping = true; 
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  
  currentRoomIdx++;
  setTimeout(() => searchAndJoinRoom(`room_${currentRoomIdx}`), 300);
}

/**
 * @internal
 */
function disbandRoom() {
  isJumping = true; 
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  isRoomFull = false;

  if (window.onRoomDisband) {
    window.onRoomDisband();
  }

  if (isRandomMode) {
    currentRoomIdx = 1;
    setTimeout(() => searchAndJoinRoom(`room_${currentRoomIdx}`), 1500);
  } else {
    if(window.appendLog) window.appendLog(`[系統] 指定房間已解散，請重新建立或加入房間。`, 'system');
  }
}