import { joinRoom, selfId } from 'https://esm.run/@trystero-p2p/mqtt';

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

export function initNetwork(appId) {
  window.js_send_to_network = sendToNetwork;
  searchAndJoinRoom(appId);
  return selfId;
}

function searchAndJoinRoom(appId) {
  if (isJoiningRoom) return;
  isJoiningRoom = true;
  isJumping = false;
  
  myPeers.clear();
  isRoomFull = false;

  const roomId = `room_${currentRoomIdx}`;
  window.appendLog(`[系統] 嘗試進入房間 ${roomId}...`, 'system');

  room = joinRoom({ 
    appId: appId,
    relayConfig: { urls: ['wss://broker.emqx.io:8084/mqtt', 'wss://broker.hivemq.com:8884/mqtt'] },
    rtcConfig: { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] }
  }, roomId);

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
      window.appendLog(`[系統] 此房間已經客滿，自動跳轉至下一間...`, 'error');
      jumpToNextRoom(appId);
    }
  };

  // 🌟 新增：用來計算寬限期的計時器
  let disbandTimeout = null;

  room.onPeerJoin = peerId => {
    if (isJumping) return; 

    if (isRoomFull) {
      sysAction.send({ type: 'REJECT' }, { target: peerId });
      return; 
    }

    myPeers.add(peerId);
    window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 加入。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    
    if (myPeers.size + 1 === MAX_PLAYERS) {
      isRoomFull = true;

      // 🌟 危機解除：如果在讀秒期間有人連回來了（或是新玩家補上空缺）
      if (disbandTimeout) {
        clearTimeout(disbandTimeout);
        disbandTimeout = null;
        window.appendLog(`[系統] 房間已重新滿員，危機解除！`, 'system');
        
        // 通知 Python 重新整理玩家名單，繼續運作
        if (window.onRoomFull) {
          window.onRoomFull(selfId, [selfId, ...Array.from(myPeers)]);
        }
      } else {
        // 正常第一次滿員
        window.appendLog(`[系統] 房間已滿 4 人！準備啟動應用程式...`, 'system');
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
      // 🌟 寬限期邏輯：先解除滿員鎖定，讓原玩家有機會連回來
      isRoomFull = false; 
      window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 網路閃斷，給予 3 秒重連時間...`, 'error');

      // 如果還沒開始倒數，就啟動 3 秒倒數計時
      if (!disbandTimeout) {
        disbandTimeout = setTimeout(() => {
          window.appendLog(`[系統] 玩家超時未歸，強制解散！`, 'error');
          disbandRoom(appId);
          disbandTimeout = null;
        }, 3000);
      }
    } else {
      window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 離開。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    }
  };
  
  isJoiningRoom = false;
}

function jumpToNextRoom(appId) {
  if (isRoomFull || isJumping) return; 
  isJumping = true; 
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  
  currentRoomIdx++;
  setTimeout(() => searchAndJoinRoom(appId), 300);
}

// 🌟 新增：房間解散流程
function disbandRoom(appId) {
  isJumping = true; // 上鎖，防止解散過程中收到奇怪的封包
  
  if (room) { room.leave(); room = null; }
  myPeers.clear();
  isRoomFull = false;

  // 通知 index.html 鎖定 UI 並清理 Python 狀態
  if (window.onRoomDisband) {
    window.onRoomDisband();
  }

  // 重設為 room_1 重新排隊，給予 1.5 秒延遲讓玩家看清楚發生什麼事
  currentRoomIdx = 1;
  setTimeout(() => searchAndJoinRoom(appId), 1500);
}

export function sendToNetwork(jsonStr) {
  if (!rawAction || isJumping) return;
  rawAction.send(jsonStr);
}

export function setPythonReady() {
  pythonCoreReady = true;
}