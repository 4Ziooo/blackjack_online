const socket = io();

let my = { username: null, chips: 0, room: null, sid: null };

const $ = (id) => document.getElementById(id);
const toast = (t) => { $("toast").innerText = t; setTimeout(()=>{$("toast").innerText=""}, 2600); };

socket.on("connect", () => {
  my.sid = socket.id;
  socket.emit("get_rooms");
});

function updateMe(){
  $("meLine").innerText = my.username
    ? `👤 ${my.username} | 💰 Chips: ${my.chips} | 🔗 Oda: ${my.room || "-"}`
    : "Giriş yapmadın";
}

$("btnReg").onclick = () => socket.emit("auth_register", { username: $("u").value, password: $("p").value });
$("btnLogin").onclick = () => socket.emit("auth_login", { username: $("u").value, password: $("p").value });

socket.on("auth_result", (r) => {
  $("authMsg").innerText = r.msg || "";
  if (r.ok && r.username) {
    my.username = r.username;
    my.chips = r.chips ?? my.chips;
    updateMe();
    toast("Giriş tamam ✅");
    socket.emit("get_rooms");
  }
});

socket.on("rooms_list", (list) => {
  const box = $("rooms");
  box.innerHTML = "";
  if (!list || list.length === 0) {
    box.innerHTML = `<div class="sub">Şu an oda yok.</div>`;
    return;
  }
  for (const r of list) {
    const row = document.createElement("div");
    row.className = "roomitem";
    row.innerHTML = `<div>🏠 <b>${r.room}</b> | 👥 ${r.players} | ⏱️ ${r.phase}</div>`;
    const btn = document.createElement("button");
    btn.className = "ghost";
    btn.innerText = "Katıl";
    btn.onclick = () => {
      $("room").value = r.room;
      joinRoom();
    };
    row.appendChild(btn);
    box.appendChild(row);
  }
});

function createRoom(){
  const room = $("room").value.trim().toUpperCase();
  socket.emit("create_room", { room });
  my.room = room;
  updateMe();
}
function joinRoom(){
  const room = $("room").value.trim().toUpperCase();
  socket.emit("join_room", { room });
  my.room = room;
  updateMe();
}
function leaveRoom(){
  if (!my.room) return;
  socket.emit("leave_room", { room: my.room });
  my.room = null;
  updateMe();
}

$("btnCreate").onclick = createRoom;
$("btnJoin").onclick = joinRoom;
$("btnLeave").onclick = leaveRoom;

$("btnChat").onclick = () => {
  if (!my.room) return toast("Önce odaya gir.");
  const msg = $("chat").value.trim();
  if (!msg) return;
  socket.emit("chat", { room: my.room, msg });
  $("chat").value = "";
};

$("btnReady").onclick = () => {
  if (!my.room) return toast("Önce odaya gir.");
  const bet = parseInt($("bet").value || "0", 10);
  socket.emit("set_bet_ready", { room: my.room, bet });
};

$("btnStartRound").onclick = () => {
  if (!my.room) return toast("Önce odaya gir.");
  socket.emit("start_round", { room: my.room });
};

$("btnNextRound").onclick = () => {
  if (!my.room) return toast("Önce odaya gir.");
  socket.emit("next_round", { room: my.room });
};

$("btnHit").onclick = () => my.room && socket.emit("hit", { room: my.room });
$("btnStand").onclick = () => my.room && socket.emit("stand", { room: my.room });
$("btnDouble").onclick = () => my.room && socket.emit("double", { room: my.room });
$("btnSplit").onclick = () => my.room && socket.emit("split", { room: my.room });

$("btnIns").onclick = () => {
  if (!my.room) return toast("Önce odaya gir.");
  const amount = parseInt($("ins").value || "0", 10);
  socket.emit("insurance", { room: my.room, amount });
};

socket.on("toast", toast);

socket.on("room_logs", (logs) => {
  const box = $("logs");
  box.innerHTML = "";
  for (const l of logs) {
    const d = new Date(l.ts * 1000);
    const line = document.createElement("div");
    line.className = "logline";
    line.innerText = `[${d.toLocaleTimeString()}] ${l.event}`;
    box.appendChild(line);
  }
  box.scrollTop = box.scrollHeight;
});

socket.on("room_state", (s) => {
  renderState(s);
  // chip güncellemesi için: server her payout sonrası DB güncelliyor, biz sayfada manuel yenilemeye bırakıyoruz.
  // İstersen "get_chips" event'i de ekleriz.
});

function renderState(s){
  $("phaseLine").innerText = `Durum: ${s.phase}`;

  // dealer
  $("dealerHand").innerHTML = "";
  for (const c of s.dealerHand) {
    const el = document.createElement("div");
    el.className = "cardchip";
    el.innerText = c.code;
    $("dealerHand").appendChild(el);
  }
  $("dealerVal").innerText = `Dealer değer: ${s.dealerValue}`;

  // players
  const list = $("players");
  list.innerHTML = "";

  for (const p of s.players) {
    const wrap = document.createElement("div");
    wrap.className = "player" + (p.sid === my.sid ? " you" : "");

    const top = document.createElement("div");
    top.innerText = `👤 ${p.username}`;
    if (p.sid === s.hostSid) top.innerHTML += ` <span class="badge">HOST</span>`;
    if (p.sid === s.turnSid) top.innerHTML += ` <span class="badge">SIRA</span>`;
    if (p.ready) top.innerHTML += ` <span class="badge">READY</span>`;
    if (p.insurance_bet > 0) top.innerHTML += ` <span class="badge">INS ${p.insurance_bet}</span>`;
    wrap.appendChild(top);

    for (const h of p.hands) {
      const handRow = document.createElement("div");
      handRow.className = "sub";
      handRow.innerHTML = `El #${h.index} | bet=${h.bet} | değer=${h.value}`
        + (p.active_hand === h.index ? " <span class='badge'>AKTİF</span>" : "")
        + (h.blackjack ? " <span class='badge'>BJ</span>" : "")
        + (h.doubled ? " <span class='badge'>DBL</span>" : "")
        + (h.busted ? " <span class='badge'>BUST</span>" : "")
        + (h.stood ? " <span class='badge'>STAND</span>" : "");
      wrap.appendChild(handRow);

      const hand = document.createElement("div");
      hand.className = "hand";
      for (const c of h.cards) {
        const el = document.createElement("div");
        el.className = "cardchip";
        el.innerText = c.code;
        hand.appendChild(el);
      }
      wrap.appendChild(hand);
    }

    list.appendChild(wrap);
  }

  // controls enable
  const isMyTurn = (s.phase === "playing" && s.turnSid === my.sid);
  $("btnHit").disabled = !isMyTurn;
  $("btnStand").disabled = !isMyTurn;
  $("btnDouble").disabled = !isMyTurn;
  $("btnSplit").disabled = !isMyTurn;

  const amHost = (s.hostSid === my.sid);
  $("btnStartRound").disabled = !amHost;
  $("btnNextRound").disabled = !amHost;

  $("btnReady").disabled = !(s.phase === "betting");
}