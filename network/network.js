import { joinRoom, selfId } from 'https://esm.run/@trystero-p2p/mqtt';

let room;
let rawAction;
let pythonCoreReady = false;

// 初始化網路
export function initNetwork(appId, roomId) {
  room = joinRoom({ 
    appId: appId,
    relayConfig: {
      urls: [
        'wss://broker.emqx.io:8084/mqtt',
        'wss://broker.hivemq.com:8884/mqtt'
      ]
    },
    rtcConfig: {
      iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:global.stun.twilio.com:3478' }
      ]
    }
  }, roomId);

  rawAction = room.makeAction('rawJsonPayload');

  // 🌟 修正點 1：將 JS 函式綁定到 window，讓 Python (Pyodide) 可以透過 js.xxx 呼叫
  window.js_send_to_network = sendToNetwork;
  // 確保 window.appendLog 存在，若無則降級使用 console.log
  window.js_append_log = window.appendLog || console.log; 

  rawAction.onMessage = (jsonStr, { peerId }) => {
    console.log(`[JS 網路層] 收到來自 ${peerId} 的封包，轉交給 Python...`);
    if (pythonCoreReady && window.pyodide) {
      // 🌟 確保抓取的是 Python 全域的 receive_from_network 函式
      const pyReceive = window.pyodide.globals.get('receive_from_network');
      if (pyReceive) {
        pyReceive(peerId, jsonStr);
      } else {
        console.error("[JS 網路層] 找不到 Python 的 receive_from_network 函式");
      }
    }
  };

  room.onPeerJoin = peerId => {
    window.js_append_log(`[系統] 節點 ${peerId.substring(0, 6)}... 已連線加入`, 'system');
  };

  room.onPeerLeave = peerId => {
    window.js_append_log(`[系統] 節點 ${peerId.substring(0, 6)}... 已斷開連線`, 'system');
    // TODO: 未來可以在這裡觸發 Python 的機制，將斷線玩家從多數決清單中剔除
  };

  return selfId;
}

export function sendToNetwork(jsonStr) {
  if (!rawAction) {
    console.error("[JS 網路層] 網路尚未初始化！");
    return;
  }
  console.log(`[JS 網路層] 收到 Python 指令，準備廣播封包...`);
  rawAction.send(jsonStr);
}

export function setPythonReady() {
  pythonCoreReady = true;
}