# PeerPlay — 6 Nimmt! Browser P2P

瀏覽器端 P2P 的六隻牛遊戲，使用 Pyodide 在瀏覽器內執行 Python 加密協議，透過 Trystero/WebRTC 進行 P2P 通訊。

---

## 架構

```
玩家 A 瀏覽器 ─── WebRTC (Trystero/MQTT) ─── 玩家 B 瀏覽器
                                │
                         玩家 C 瀏覽器
                                │
                         玩家 D 瀏覽器
```

- **HTTP Server**：只負責提供靜態檔案（HTML / JS / Python 原始碼）
- **遊戲通訊**：4 個瀏覽器直接 P2P，不經過主機
- **Python 邏輯**：透過 Pyodide 在瀏覽器內執行（Mental Poker 發牌協議）

---

## 本地執行

```bash
cd PeerPlay
python3 -m http.server 8080
```

開啟 `http://localhost:8080/UI/show_ui.html`

---

## 線上測試（讓外網玩家連入）

需要將本地 HTTP server 暴露到網路上，使用 **Cloudflare Tunnel**（免帳號）：

### 步驟

1. **啟動 HTTP server**
   ```bash
   cd PeerPlay
   python3 -m http.server 8080
   ```

2. **下載並啟動 cloudflared**（另開一個終端機）
   ```bash
   # 下載（只需第一次）
   wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /tmp/cloudflared
   chmod +x /tmp/cloudflared

   # 啟動 tunnel
   /tmp/cloudflared tunnel --url http://localhost:8080
   ```

3. **取得公開網址**

   cloudflared 啟動後會輸出：
   ```
   Your quick Tunnel has been created! Visit it at:
   https://xxxx-yyyy-zzzz.trycloudflare.com
   ```

4. **分享給其他玩家**

   遊戲網址為：
   ```
   https://xxxx-yyyy-zzzz.trycloudflare.com/UI/show_ui.html
   ```
   4 個人各自用瀏覽器打開，進入同一個房間 (`room_1`) 即可開始。

> **注意**：主機的電腦必須全程保持開啟（HTTP server + cloudflared 都要跑著）

---

## 已知問題

### 1. 玩家載入時間不一致導致 Consensus Timeout

**症狀**：`ConsensusError: get_global_seed: commit phase timed out`

**原因**：Pyodide 需要下載並初始化 `ecdsa` + `cryptography` 套件，在網路較慢的環境下可能超過 60 秒。某個玩家還沒載入完成，其他人已開始 consensus 計時。

**現況**：遊戲會自動解散重組，重試通常可成功。

**TODO**：實作「等待所有人 Python ready 後再啟動 consensus」的同步機制。

---

### 2. WSL / Windows 磁碟寫入問題（開發環境）

**症狀**：修改 Python 原始碼後，瀏覽器仍拿到舊版本。

**原因**：VS Code 在 WSL 環境下，編輯工具修改的是記憶體 buffer，不一定立即寫入 Windows 磁碟（`/mnt/c/`）。HTTP server 讀取磁碟，因此看到舊版。

**解法**：用終端機的 `python3 -c` 或 `sed` 修改檔案，確保寫入磁碟。

---

## 依賴套件

```
ecdsa
cryptography
```

Pyodide 端於瀏覽器內自動透過 `micropip` 安裝，無需手動處理。
