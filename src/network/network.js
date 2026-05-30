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
 * - 說明：房間因有人斷線且超時未歸而解散時觸發。前端需「鎖定 UI」、「清理 Python 狀態」並顯示重新排隊畫面。
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
let currentRoomIdx = 1;
let myPeers = new Set();
let isRoomFull = false;
let isJoiningRoom = false;
let isJumping = false;


// ============================================================================
// [公開介面 Public API] - 給前端或 Python 呼叫的方法
// ============================================================================

/**
 * @public
 * @description 初始化網路連線，開始尋找並加入房間。
 * @param {string} appId - 應用程式的唯一識別碼
 * @returns {string} 回傳本機玩家的專屬 ID (selfId)
 */
export function initNetwork(appId) {
  // 將發送函數掛載到全域，方便 Python 或前端直接呼叫
  window.js_send_to_network = sendToNetwork;
  searchAndJoinRoom(appId);
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
 * @description 標記 Python 核心已完全就緒。前端必須在 Python 載入完成後呼叫此方法，網路層才會開始把資料往 Python 送。
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
function searchAndJoinRoom(appId) {
  if (isJoiningRoom) return; 
  isJoiningRoom = true;
  isJumping = false;
  
  myPeers.clear();
  isRoomFull = false;

  const roomId = `room_${currentRoomIdx}`;
  if(window.appendLog) window.appendLog(`[系統] 嘗試進入房間 ${roomId}...`, 'system');

  room = joinRoom({ 
    appId: appId,
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
      if(window.appendLog) window.appendLog(`[系統] 此房間已經客滿，自動跳轉至下一間...`, 'error');
      jumpToNextRoom(appId);
    }
  };

  // --- 內部連線生命週期管理 ---

  let disbandTimeout = null;

  room.onPeerJoin = peerId => {
    if (isJumping) return; 

    // 房間已滿則拒絕新玩家
    if (isRoomFull) {
      sysAction.send({ type: 'REJECT' }, { target: peerId });
      return; 
    }

    myPeers.add(peerId);
    if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 加入。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    
    // 檢查是否滿員
    if (myPeers.size + 1 === MAX_PLAYERS) {
      isRoomFull = true;

      // 若在斷線 3 秒寬限期內補滿人數，解除解散危機
      if (disbandTimeout) {
        clearTimeout(disbandTimeout);
        disbandTimeout = null;
        if(window.appendLog) window.appendLog(`[系統] 房間已重新滿員，危機解除！`, 'system');
        
        if (window.onRoomFull) {
          window.onRoomFull(selfId, [selfId, ...Array.from(myPeers)]);
        }
      } else {
        // 正常首次滿員
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
      // 滿員狀態下有人斷線，啟動 3 秒重連寬限期
      isRoomFull = false; 
      if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 網路閃斷，給予 3 秒重連時間...`, 'error');

      if (!disbandTimeout) {
        disbandTimeout = setTimeout(() => {
          if(window.appendLog) window.appendLog(`[系統] 玩家超時未歸，強制解散！`, 'error');
          disbandRoom(appId);
          disbandTimeout = null;
        }, 3000);
      }
    } else {
      if(window.appendLog) window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 離開。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    }
  };
  
  isJoiningRoom = false;
}

/**
 * @internal
 * @description 跳轉至下一個房間 (當前房間滿員時自動觸發)
 */
function jumpToNextRoom(appId) {
  if (isRoomFull || isJumping) return; 
  isJumping = true; // 上鎖，停止處理封包
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  
  currentRoomIdx++;
  setTimeout(() => searchAndJoinRoom(appId), 300);
}

/**
 * @internal
 * @description 房間強制解散流程 (當斷線超時觸發)
 */
function disbandRoom(appId) {
  isJumping = true; // 上鎖，停止處理封包
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  isRoomFull = false;

  // 通知前端進行 UI 鎖定與清理
  if (window.onRoomDisband) {
    window.onRoomDisband();
  }

  // 重設房間編號並延遲 1.5 秒後重新排隊，讓使用者看清提示訊息
  currentRoomIdx = 1;
  setTimeout(() => searchAndJoinRoom(appId), 1500);
}