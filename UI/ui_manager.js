// ui_manager.js  — PeerPlay 六隻牛 Game UI Logic
// Called by Python via window.* and by the network module script.
// Works in both "real" mode (Python/Pyodide loaded) and "mock" mode (no Python).

// ─── Global UI state ─────────────────────────────────────────────────────────
const uiState = {
  canPlay:         false,
  selectedCard:    null,
  pendingRowSelect: false,  // true when player must choose a row
  myName:          null,
  allPlayers:      [],
  prevScores:      {},
};

let timerInterval = null;
let _rowSelectCallback = null;

// ─── Loading helpers (called from module script) ──────────────────────────────
function showLoading(msg) {
  const el = document.getElementById('loading-overlay');
  const msgEl = document.getElementById('loading-msg');
  if (el) el.classList.add('visible');
  if (msgEl) msgEl.textContent = msg || '載入中...';
}

function updateLoading(msg) {
  const msgEl = document.getElementById('loading-msg');
  if (msgEl) msgEl.textContent = msg;
}

function hideLoading() {
  const el = document.getElementById('loading-overlay');
  if (el) el.classList.remove('visible');
}

window.showLoading  = showLoading;
window.updateLoading = updateLoading;
window.hideLoading  = hideLoading;


// ═════════════════════════════════════════════════════════════════════════════
//  window.renderGamePage(gameStateJson)
//  Called by Python after init_game or after each round resolves.
//  JSON: { scores:{}, table_rows:[[...],[...],[...],[...]], my_hand:[],
//          my_name:str, all_players:[str,...] }
// ═════════════════════════════════════════════════════════════════════════════
window.renderGamePage = function(gameStateJson) {
  const data = (typeof gameStateJson === 'string')
    ? JSON.parse(gameStateJson) : gameStateJson;

  console.log('[renderGamePage] table_rows:', JSON.stringify(data.table_rows));

  if (data.my_name)    uiState.myName    = data.my_name;
  if (data.all_players && data.all_players.length)
    uiState.allPlayers = data.all_players;

  // Update score board
  updateScoreBoard(data.scores || {});

  // Update 4 table rows
  for (let i = 0; i < 4; i++) {
    const rowEl = document.getElementById(`row-${i}`);
    if (!rowEl) { console.warn('[renderGamePage] row-' + i + ' not found!'); continue; }
    rowEl.innerHTML = '';
    const cards = (data.table_rows || data.rows || [])[i] || [];
    cards.forEach((n, idx) => {
      const card = createCardDOM(n);
      // Mark 6th card slot as danger (row full — next placement takes this row)
      if (idx === 5) card.classList.add('danger-card');
      rowEl.appendChild(card);
    });
    // Show row horn count
    const hornSum = cards.reduce((s, n) => s + cardHorns(n), 0);
    const hornEl = document.getElementById(`row-horns-${i}`);
    if (hornEl) hornEl.textContent = cards.length ? `${hornSum}🐂` : '';
  }

  // Update hand
  renderHand(data.my_hand || []);
};


// ─── Render hand cards ────────────────────────────────────────────────────────
function renderHand(cards) {
  const hand = document.getElementById('my-hand');
  if (!hand) return;
  hand.innerHTML = '';
  if (!cards.length) {
    hand.innerHTML = '<span class="empty-hint">手牌已出盡</span>';
    return;
  }
  cards.forEach(n => {
    const el = createCardDOM(n);
    if (uiState.canPlay) {
      el.addEventListener('click', () => selectCard(n, el));
    }
    hand.appendChild(el);
  });
}


// ═════════════════════════════════════════════════════════════════════════════
//  Card selection
// ═════════════════════════════════════════════════════════════════════════════
function selectCard(cardNum, cardEl) {
  document.querySelectorAll('#my-hand .card').forEach(c => c.classList.remove('selected'));
  cardEl.classList.add('selected');
  uiState.selectedCard = cardNum;
  const btn = document.getElementById('commit-btn');
  if (btn) btn.disabled = false;
}


// ═════════════════════════════════════════════════════════════════════════════
//  onCommitButtonClick  — user presses "確認出牌"
// ═════════════════════════════════════════════════════════════════════════════
async function onCommitButtonClick() {
  if (!uiState.selectedCard) return;
  const card = uiState.selectedCard;

  // Visually remove card from hand immediately
  document.querySelectorAll('#my-hand .card').forEach(el => {
    if (parseInt(el.dataset.num) === card) el.remove();
  });

  // Show my card in played zone (face-up)
  initPlayedZoneIfNeeded();
  markCardPlayed(uiState.myName || 'Me', card, true);

  // Disable play phase while waiting
  switchPlayPhase(false, '等待其他玩家出牌...');

  // ── Call Python or mock ───────────────────────────────────────────────────
  if (window.python_receive_input) {
    try {
      window.python_receive_input(JSON.stringify({ action: 'PLAY_CARD', card: card }));
    } catch(e) {
      console.error('[UI] python_receive_input error:', e);
    }
  } else {
    // Mock: simulate other players committing
    _mockOtherPlayersCommit(card);
  }
}

window.onCommitButtonClick = onCommitButtonClick;


// ═════════════════════════════════════════════════════════════════════════════
//  window.switchPlayPhase(allowed, message, timerSeconds)
//  Enable or disable the play phase.
// ═════════════════════════════════════════════════════════════════════════════
function switchPlayPhase(allowed, message, timerSeconds) {
  uiState.canPlay = allowed;
  const msgEl  = document.getElementById('status-message');
  const btn    = document.getElementById('commit-btn');
  const hand   = document.getElementById('my-hand');

  if (msgEl) msgEl.textContent = message || '';

  if (!allowed) {
    stopTimer();
    if (btn)  btn.disabled = true;
    if (hand) hand.classList.add('disabled-layer');
  } else {
    if (hand) hand.classList.remove('disabled-layer');
    uiState.selectedCard = null;
    if (btn)  btn.disabled = true;

    // Re-bind click listeners (cloneNode removes old ones cleanly)
    document.querySelectorAll('#my-hand .card').forEach(el => {
      const fresh = el.cloneNode(true);
      const n = parseInt(fresh.dataset.num);
      fresh.addEventListener('click', () => selectCard(n, fresh));
      el.replaceWith(fresh);
    });

    if (timerSeconds && timerSeconds > 0) startTimer(timerSeconds);
  }
}

window.switchPlayPhase = switchPlayPhase;


// ═════════════════════════════════════════════════════════════════════════════
//  Row selection mode
//  Called when player's card is lower than all rows (must choose which to take).
//  window.enableRowSelection(callback) — callback(rowIndex: 0-3)
//  window.disableRowSelection()
// ═════════════════════════════════════════════════════════════════════════════
window.enableRowSelection = function(callback) {
  _rowSelectCallback = callback;
  uiState.pendingRowSelect = true;
  document.getElementById('row-select-banner').classList.add('visible');
  for (let i = 0; i < 4; i++) {
    const wrap = document.getElementById(`row-wrap-${i}`);
    if (!wrap) continue;
    wrap.classList.add('selectable');
    wrap.addEventListener('click', _rowClickHandler);
  }
};

window.disableRowSelection = function() {
  uiState.pendingRowSelect = false;
  _rowSelectCallback = null;
  document.getElementById('row-select-banner').classList.remove('visible');
  for (let i = 0; i < 4; i++) {
    const wrap = document.getElementById(`row-wrap-${i}`);
    if (!wrap) continue;
    wrap.classList.remove('selectable');
    wrap.removeEventListener('click', _rowClickHandler);
  }
};

function _rowClickHandler(e) {
  const wrap = e.currentTarget;
  const rowIdx = parseInt(wrap.dataset.row);
  const cb = _rowSelectCallback;   // save BEFORE disableRowSelection nulls it
  window.disableRowSelection();
  if (cb) cb(rowIdx);
}


// ═════════════════════════════════════════════════════════════════════════════
//  Timer
// ═════════════════════════════════════════════════════════════════════════════
function startTimer(seconds) {
  stopTimer();
  const wrap  = document.getElementById('timer-wrap');
  const fill  = document.getElementById('timer-fill');
  const count = document.getElementById('timer-count');
  if (!wrap) return;

  wrap.classList.add('active');
  fill.classList.remove('urgent');
  count.classList.remove('urgent');
  count.textContent = seconds;

  // CSS transition for smooth bar shrink
  fill.style.transition = 'none';
  fill.style.width = '100%';
  requestAnimationFrame(() => {
    fill.style.transition = `width ${seconds}s linear`;
    fill.style.width = '0%';
  });

  let remaining = seconds;
  timerInterval = setInterval(() => {
    remaining--;
    count.textContent = remaining;
    if (remaining <= 5) {
      fill.classList.add('urgent');
      count.classList.add('urgent');
    }
    if (remaining <= 0) {
      stopTimer();
      // Auto-submit: pick first card in hand
      const firstCard = document.querySelector('#my-hand .card:not(.disabled-layer)');
      if (firstCard) {
        firstCard.click();
        setTimeout(() => {
          const btn = document.getElementById('commit-btn');
          if (btn && !btn.disabled) btn.click();
        }, 350);
      }
    }
  }, 1000);
}

function stopTimer() {
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
  const wrap = document.getElementById('timer-wrap');
  if (wrap) wrap.classList.remove('active');
}

window.startTimer = startTimer;
window.stopTimer  = stopTimer;


// ═════════════════════════════════════════════════════════════════════════════
//  Played zone  — shows each player's face-down card, then reveals
// ═════════════════════════════════════════════════════════════════════════════
function initPlayedZoneIfNeeded() {
  const zone = document.getElementById('played-zone');
  if (zone.classList.contains('visible')) return;
  zone.innerHTML = '<div class="played-label">本回合出牌</div>';
  const players = uiState.allPlayers.length ? uiState.allPlayers : [uiState.myName || 'Me'];
  players.forEach(name => {
    const slot = document.createElement('div');
    slot.className = 'played-slot';
    slot.id = `pslot-${name}`;

    const label = document.createElement('div');
    label.className = 'played-name' + (name === uiState.myName ? ' is-me' : '');
    label.textContent = name.substring(0, 12);

    const card = document.createElement('div');
    card.className = 'card face-down';
    card.id = `pcard-${name}`;
    card.innerHTML = `<span class="card-num">?</span>
                      <span class="card-center"></span>
                      <span class="card-horns"></span>`;
    slot.appendChild(label);
    slot.appendChild(card);
    zone.appendChild(slot);
  });
  zone.classList.add('visible');
}

function markCardPlayed(playerName, cardNum, isMine) {
  initPlayedZoneIfNeeded();
  const el = document.getElementById(`pcard-${playerName}`);
  if (!el) return;
  el.className = 'card played-card';
  if (isMine) {
    el.dataset.horns = cardHorns(cardNum);
    el.classList.remove('face-down');
    el.innerHTML = `<span class="card-num">${cardNum}</span>
                    <span class="card-center">🐄</span>
                    <span class="card-horns">${hornLabel(cardNum)}</span>`;
  } else {
    // Face-down until revealed
    el.classList.add('face-down');
  }
}

// Reveal all played cards (called by Python after all commits received)
window.revealAllPlayed = function(playsJson) {
  const plays = (typeof playsJson === 'string') ? JSON.parse(playsJson) : playsJson;
  plays.forEach(({ player, card }) => {
    const el = document.getElementById(`pcard-${player}`);
    if (!el) return;
    el.className = 'card played-card';
    el.dataset.horns = cardHorns(card);
    el.innerHTML = `<span class="card-num">${card}</span>
                    <span class="card-center">🐄</span>
                    <span class="card-horns">${hornLabel(card)}</span>`;
  });
};

// Clear played zone (call at start of new round)
window.clearPlayedZone = function() {
  const zone = document.getElementById('played-zone');
  zone.classList.remove('visible');
  zone.innerHTML = '<div class="played-label">本回合出牌</div>';
};


// ═════════════════════════════════════════════════════════════════════════════
//  window.revealAllPlayedStaggered(cards)
//  cards: { player_id: card_num, ... }
//  Flips each player's card face-up one by one, sorted by card value ascending.
// ═════════════════════════════════════════════════════════════════════════════
window.revealAllPlayedStaggered = function(cards) {
  const entries = Object.entries(cards).sort((a, b) => a[1] - b[1]);
  entries.forEach(([player, card], idx) => {
    setTimeout(() => {
      const el = document.getElementById(`pcard-${player}`);
      if (!el) return;
      el.className = 'card played-card';
      el.dataset.horns = cardHorns(card);
      el.innerHTML = `<span class="card-num">${card}</span>
                      <span class="card-center">🐄</span>
                      <span class="card-horns">${hornLabel(card)}</span>`;
    }, idx * 380);
  });
};


// ═════════════════════════════════════════════════════════════════════════════
//  window.animateRoundResolution(plays, newRows, scores, myName, allPlayers)
//  plays: [{player, card, target_row, action, score_added}] sorted by card asc
//  Progressively adds each card into its row DOM with drop-in animation.
//  Does NOT call renderGamePage — NEXT_TURN will do a clean full render.
// ═════════════════════════════════════════════════════════════════════════════
window.animateRoundResolution = function(plays, newRows, scores, myName, allPlayers) {
  const STEP_MS = 700;  // delay between each card being placed

  console.log('[animateRoundResolution] plays:', JSON.stringify(plays), 'count:', plays.length);

  plays.forEach((play, idx) => {
    setTimeout(() => {
      console.log(`[animate] step ${idx}: player=${play.player} card=${play.card} target_row=${play.target_row} action=${play.action}`);
      // 1. Highlight the played-zone card for this player
      const pcardEl = document.getElementById(`pcard-${play.player}`);
      if (pcardEl) {
        pcardEl.classList.remove('glowing');
        void pcardEl.offsetWidth;
        pcardEl.classList.add('glowing');
      } else {
        console.warn(`[animate] pcard-${play.player} NOT FOUND in DOM`);
      }

      // 2. Mutate the row DOM directly
      const rowEl   = document.getElementById(`row-${play.target_row}`);
      const rowWrap = document.getElementById(`row-wrap-${play.target_row}`);

      if (rowEl) {
        if (play.action === 'took_row') {
          // Clear the row (player takes all cards), add new card on top
          rowEl.innerHTML = '';
          if (rowWrap) {
            rowWrap.classList.remove('flash-place', 'flash-take');
            void rowWrap.offsetWidth;
            rowWrap.classList.add('flash-take');
          }
        } else {
          if (rowWrap) {
            rowWrap.classList.remove('flash-place', 'flash-take');
            void rowWrap.offsetWidth;
            rowWrap.classList.add('flash-place');
          }
        }

        // Append the played card with drop-in animation
        const cardEl = createCardDOM(play.card);
        cardEl.classList.add('drop-in');
        rowEl.appendChild(cardEl);

        // Update horn count label
        const hornEl = document.getElementById(`row-horns-${play.target_row}`);
        if (hornEl) {
          const rowCards = Array.from(rowEl.querySelectorAll('.card')).map(el => parseInt(el.dataset.num));
          const hornSum  = rowCards.reduce((s, n) => s + cardHorns(n), 0);
          hornEl.textContent = rowCards.length ? `${hornSum}🐂` : '';
        }
      } else {
        console.warn(`[animate] row-${play.target_row} NOT FOUND in DOM`);
      }
      if (idx === plays.length - 1) {
        setTimeout(() => {
          updateScoreBoard(scores, true);
          window.clearPlayedZone();
        }, 500);
      }
    }, idx * STEP_MS);
  });

  // Fallback: if plays is empty just clear zone
  if (!plays.length) {
    console.warn('[animateRoundResolution] plays is EMPTY — nothing to animate');
    updateScoreBoard(scores, true);
    window.clearPlayedZone();
  }
};


// ═════════════════════════════════════════════════════════════════════════════
//  window.showGameOverScreen(scores, myName)
//  Shows the game-over overlay with ranked final scores.
// ═════════════════════════════════════════════════════════════════════════════
window.showGameOverScreen = function(scores, myName) {
  const ranked = Object.entries(scores).sort((a, b) => a[1] - b[1]);
  const podium = document.getElementById('go-podium');
  const title  = document.getElementById('go-title');
  if (!podium) return;

  const medals = ['🥇', '🥈', '🥉', '4️⃣'];
  const myScore = scores[myName];
  const myRank  = ranked.findIndex(([n]) => n === myName);

  if (myRank === 0) {
    title.textContent = '🎉 你贏了！';
  } else if (myRank === ranked.length - 1) {
    title.textContent = '😢 你輸了...';
  } else {
    title.textContent = `🏁 第 ${myRank + 1} 名`;
  }

  podium.innerHTML = '';
  ranked.forEach(([name, score], i) => {
    const row = document.createElement('div');
    const isWinner = i === 0;
    const isLoser  = i === ranked.length - 1;
    const isMe     = name === myName;
    row.className  = `go-row${isWinner ? ' winner' : ''}${isLoser ? ' loser' : ''}`;
    row.innerHTML  =
      `<span class="go-rank">${medals[i] || (i + 1)}</span>` +
      `<span class="go-name${isMe ? ' is-me' : ''}">${name.substring(0, 16)}${isMe ? ' (你)' : ''}</span>` +
      `<span class="go-score">${score} 🐂</span>`;
    podium.appendChild(row);
  });

  document.getElementById('gameover-overlay').classList.add('visible');
};


// ═════════════════════════════════════════════════════════════════════════════
//  window.showRoundResult(roundResultJson)
//  JSON: { plays:[{player, card, action, score_added}], scores_after:{} }
//  action: 'placed' | 'took_row'
// ═════════════════════════════════════════════════════════════════════════════
window.showRoundResult = function(roundResultJson) {
  const data = (typeof roundResultJson === 'string')
    ? JSON.parse(roundResultJson) : roundResultJson;

  // Update score board with animation
  updateScoreBoard(data.scores_after || {}, true);

  // Build result table
  const body = document.getElementById('round-result-body');
  body.innerHTML = '';
  (data.plays || []).forEach(p => {
    const row = document.createElement('div');
    row.className = 'round-row';
    const delta = p.score_added || 0;
    const isMe  = p.player === uiState.myName;
    row.innerHTML = `
      <span class="round-player">${p.player}${isMe ? ' ★' : ''}</span>
      <span class="round-card-played">出 <strong>${p.card}</strong> 號</span>
      <span class="round-action">${p.action === 'took_row' ? '🔴 收走一列' : '✅ 放置'}</span>
      <span class="round-score-delta ${delta === 0 ? 'zero' : ''}">
        ${delta > 0 ? `+${delta} 🐂` : '—'}
      </span>`;
    body.appendChild(row);
  });

  const overlay = document.getElementById('round-overlay');
  overlay.classList.add('visible');
  setTimeout(() => {
    overlay.classList.remove('visible');
    window.clearPlayedZone();
  }, 3500);
};


// ═════════════════════════════════════════════════════════════════════════════
//  Score board
// ═════════════════════════════════════════════════════════════════════════════
function updateScoreBoard(scores, animate) {
  const board = document.getElementById('score-board');
  if (!board) return;
  board.innerHTML = '';
  Object.entries(scores).forEach(([name, score]) => {
    const item = document.createElement('div');
    item.className = 'score-item';
    item.id = `score-${name}`;
    const delta = (animate && uiState.prevScores[name] != null)
      ? score - uiState.prevScores[name] : 0;
    const isMe = (name === uiState.myName);
    item.innerHTML =
      `<span class="score-name${isMe ? ' is-me' : ''}">${name}</span>` +
      `<span class="score-val">${score} 🐂` +
        (delta > 0 ? `<span class="score-delta">+${delta}</span>` : '') +
      `</span>`;
    if (animate && delta > 0) {
      requestAnimationFrame(() => {
        item.classList.add('flashing');
        item.addEventListener('animationend', () => item.classList.remove('flashing'), { once: true });
      });
    }
    board.appendChild(item);
  });
  uiState.prevScores = { ...scores };
}


// ═════════════════════════════════════════════════════════════════════════════
//  Card DOM helpers
// ═════════════════════════════════════════════════════════════════════════════
function cardHorns(n) {
  if (n === 55)     return 7;
  if (n % 11 === 0) return 5;
  if (n % 10 === 0) return 3;
  if (n % 5  === 0) return 2;
  return 1;
}

function hornLabel(n) {
  const h = cardHorns(n);
  return '🐂'.repeat(h) + (h > 1 ? ` ×${h}` : '');
}

function createCardDOM(num) {
  const h = cardHorns(num);
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.num   = num;
  card.dataset.horns = h;
  card.innerHTML =
    `<span class="card-num">${num}</span>` +
    `<span class="card-center">🐄</span>` +
    `<span class="card-horns">${hornLabel(num)}</span>`;
  return card;
}


// ═════════════════════════════════════════════════════════════════════════════
//  Mock mode helpers
//  Called when Python / Pyodide is not available (dev / standalone testing)
// ═════════════════════════════════════════════════════════════════════════════

// Generate a shuffled deck and deal cards (Fisher-Yates)
function _mockDeal(players) {
  const deck = [];
  for (let i = 1; i <= 104; i++) deck.push(i);
  for (let i = deck.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [deck[i], deck[j]] = [deck[j], deck[i]];
  }
  const hands = {};
  players.forEach((p, idx) => {
    hands[p] = deck.slice(idx * 10, idx * 10 + 10).sort((a, b) => a - b);
  });
  const tableCards = deck.slice(40, 44); // 4 starter cards
  const tableRows  = tableCards.map(c => [c]);
  const scores     = {};
  players.forEach(p => { scores[p] = 0; });
  return { hands, tableRows, scores };
}

// Render mock game state (no Python)
function renderMockGameState(selfId, players) {
  uiState.myName    = selfId;
  uiState.allPlayers = players;

  const { hands, tableRows, scores } = _mockDeal(players);
  window._mockHands  = hands;
  window._mockScores = scores;
  window._mockRows   = tableRows;

  window.renderGamePage(JSON.stringify({
    scores,
    table_rows:  tableRows,
    my_hand:     hands[selfId] || hands[players[0]],
    my_name:     selfId,
    all_players: players,
  }));

  switchPlayPhase(true, '輪到你出牌，請選一張手牌！', 30);
}

window.renderMockGameState = renderMockGameState;

// Simulate other players committing + round resolution
function _mockOtherPlayersCommit(myCard) {
  if (!uiState.allPlayers.length) return;

  initPlayedZoneIfNeeded();

  const others = uiState.allPlayers.filter(p => p !== uiState.myName);
  const mockHands = window._mockHands || {};

  // Other players "commit" (show face-down) after 1s
  setTimeout(() => {
    others.forEach(name => markCardPlayed(name, null, false));

    // Reveal + settle after another 1.5s
    setTimeout(() => {
      // Pick a random card from each other player's hand
      const plays = [{
        player: uiState.myName,
        card: myCard,
        action: 'placed',
        score_added: 0,
      }];

      others.forEach(name => {
        const hand = mockHands[name] || [10, 20, 30];
        const card = hand[Math.floor(Math.random() * hand.length)];
        plays.push({ player: name, card, action: 'placed', score_added: 0 });
      });

      // Simulate one random player taking a row
      const luckyIdx = Math.floor(Math.random() * plays.length);
      plays[luckyIdx].action = 'took_row';
      plays[luckyIdx].score_added = Math.floor(Math.random() * 5) + 1;

      // Reveal all
      window.revealAllPlayed(JSON.stringify(plays.map(p => ({ player: p.player, card: p.card }))));

      // Update mock scores
      const scores = { ...(window._mockScores || {}) };
      plays.forEach(p => { scores[p.player] = (scores[p.player] || 0) + p.score_added; });
      window._mockScores = scores;

      // Show round result after short delay
      setTimeout(() => {
        window.showRoundResult(JSON.stringify({ plays, scores_after: scores }));

        // Next round: re-enable play after overlay closes
        setTimeout(() => {
          // Remove played card from mock hand
          const myHand = (mockHands[uiState.myName] || []).filter(c => c !== myCard);
          mockHands[uiState.myName] = myHand;

          if (myHand.length === 0) {
            switchPlayPhase(false, '🏁 遊戲結束！');
            return;
          }

          window.renderGamePage(JSON.stringify({
            scores,
            table_rows:  window._mockRows || [[],[],[],[]],
            my_hand:     myHand,
            my_name:     uiState.myName,
            all_players: uiState.allPlayers,
          }));
          switchPlayPhase(true, '輪到你出牌，請選一張手牌！', 30);
        }, 3700);
      }, 800);
    }, 1500);
  }, 1000);
}
