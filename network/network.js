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
  isJumping = false; // 進入新房間，重設跳轉鎖
  
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
    // 🛡️ 防禦：跳轉中不收任何封包
    if (isJumping) return; 
    
    if (pythonCoreReady && window.python_receive_from_network) {
      window.python_receive_from_network(peerId, jsonStr);
    }
  };

  sysAction = room.makeAction('sysInfo');
  sysAction.onMessage = (msg, { peerId }) => {
    // 🛡️ 防禦：已經在跳轉了，無視其他老玩家重複發送的 REJECT
    if (isJumping) return; 
    
    if (msg.type === 'REJECT' && !isRoomFull) {
      window.appendLog(`[系統] 此房間已經客滿，自動跳轉至下一間...`, 'error');
      jumpToNextRoom(appId);
    }
  };

  room.onPeerJoin = peerId => {
    // 🛡️ 防禦：我已經在跳轉了，舊房間的殘留連線直接無視
    if (isJumping) return; 

    if (isRoomFull) {
      sysAction.send({ type: 'REJECT' }, { target: peerId });
      return; 
    }

    myPeers.add(peerId);
    window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 加入。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    
    if (myPeers.size + 1 === MAX_PLAYERS) {
      isRoomFull = true;
      window.appendLog(`[系統] 房間已滿 4 人！準備啟動應用程式...`, 'system');
      
      if (window.onRoomFull) {
        window.onRoomFull(selfId, [selfId, ...Array.from(myPeers)]);
      }
    }
  };

  room.onPeerLeave = peerId => {
    // 🛡️ 防禦：跳轉中無視舊房間的離開事件
    if (isJumping) return; 

    myPeers.delete(peerId);
    if (!isRoomFull) {
      window.appendLog(`[系統] 玩家 ${peerId.substring(0, 6)} 離開。目前 ${myPeers.size + 1}/${MAX_PLAYERS} 人`, 'system');
    }
  };
  
  isJoiningRoom = false;
}

function jumpToNextRoom(appId) {
  if (isRoomFull || isJumping) return; 
  isJumping = true; 
  
  // 同步斷開舊房間，徹底斬斷 WebRTC 連線
  if (room) {
    room.leave();
    room = null;
  }
  myPeers.clear();
  
  currentRoomIdx++;
  setTimeout(() => searchAndJoinRoom(appId), 300);
}

export function sendToNetwork(jsonStr) {
  if (!rawAction || isJumping) return;
  rawAction.send(jsonStr);
}

export function setPythonReady() {
  pythonCoreReady = true;
}