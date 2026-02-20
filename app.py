import os, sqlite3, time, random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash

APP_SECRET = os.environ.get("APP_SECRET", "dev_secret_change_me")
DB_PATH = os.environ.get("DB_PATH", "/tmp/blackjack.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = APP_SECRET
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")



# ---------------- DB ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            passhash TEXT NOT NULL,
            chips INTEGER NOT NULL DEFAULT 2000,
            created_at INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS game_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            ts INTEGER NOT NULL,
            event TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def log_event(room: str, event: str):
    con = db()
    con.execute("INSERT INTO game_log(room, ts, event) VALUES(?,?,?)", (room, int(time.time()), event))
    con.commit()
    con.close()

def get_user(username: str):
    con = db()
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    con.close()
    return row

def create_user(username: str, password: str):
    con = db()
    con.execute(
        "INSERT INTO users(username, passhash, chips, created_at) VALUES(?,?,?,?)",
        (username, generate_password_hash(password), 2000, int(time.time()))
    )
    con.commit()
    con.close()

def update_chips(username: str, chips: int):
    con = db()
    con.execute("UPDATE users SET chips=? WHERE username=?", (chips, username))
    con.commit()
    con.close()

def fetch_recent_logs(room: str, limit: int = 60):
    con = db()
    rows = con.execute(
        "SELECT ts, event FROM game_log WHERE room=? ORDER BY id DESC LIMIT ?",
        (room, limit)
    ).fetchall()
    con.close()
    return list(reversed(rows))

# ---------------- Blackjack logic ----------------
def create_deck(num_decks=2):
    suits = ['♠','♥','♦','♣']
    ranks = [
        ('A',11),('2',2),('3',3),('4',4),('5',5),('6',6),
        ('7',7),('8',8),('9',9),('10',10),('J',10),('Q',10),('K',10)
    ]
    deck = []
    for _ in range(num_decks):
        for s in suits:
            for r,v in ranks:
                deck.append({'code': f"{r}{s}", 'rank': r, 'value': v})
    random.shuffle(deck)
    return deck

def hand_value(hand):
    total = sum(c['value'] for c in hand)
    aces = sum(1 for c in hand if c['rank'] == 'A')
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def is_blackjack(hand):
    return len(hand) == 2 and hand_value(hand) == 21

def can_split(hand):
    return len(hand) == 2 and hand[0]["rank"] == hand[1]["rank"]

def can_double(hand):
    # yaygın kural: sadece ilk 2 kartta double
    return len(hand) == 2

@dataclass
class HandState:
    cards: List[dict] = field(default_factory=list)
    bet: int = 0
    stood: bool = False
    busted: bool = False
    doubled: bool = False
    blackjack: bool = False

@dataclass
class PlayerState:
    username: str
    sid: str
    hands: List[HandState] = field(default_factory=list)
    active_hand: int = 0
    ready: bool = False
    insurance_bet: int = 0

    def current(self) -> HandState:
        return self.hands[self.active_hand]

@dataclass
class RoomState:
    code: str
    host_sid: str
    phase: str = "betting"  # betting, playing, dealer, finished
    deck: List[dict] = field(default_factory=list)
    dealer_hand: List[dict] = field(default_factory=list)
    players: Dict[str, PlayerState] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    turn_index: int = 0

    # rules
    num_decks: int = 2
    min_bet: int = 10
    max_bet: int = 1000
    allow_insurance: bool = True
    allow_split: bool = True
    allow_double: bool = True

rooms: Dict[str, RoomState] = {}

def room_list_public():
    # Oda listesi: kod, oyuncu sayısı, phase
    out = []
    for code, r in rooms.items():
        out.append({"room": code, "players": len(r.players), "phase": r.phase})
    out.sort(key=lambda x: (-x["players"], x["room"]))
    return out

def emit_rooms_list():
    socketio.emit("rooms_list", room_list_public())

def dealer_upcard(room: RoomState):
    return room.dealer_hand[0] if room.dealer_hand else None

def dealer_has_blackjack(room: RoomState):
    return is_blackjack(room.dealer_hand)

def pop_card(room: RoomState):
    if not room.deck:
        room.deck = create_deck(room.num_decks)
    return room.deck.pop()

def public_state(room: RoomState, reveal_dealer: bool):
    dealer = room.dealer_hand[:]
    if not reveal_dealer and len(dealer) >= 2 and room.phase in ("betting", "playing"):
        dealer = [dealer[0], {'code': '🂠', 'rank': '?', 'value': 0}]
    turn_sid = None
    if room.phase == "playing" and room.order:
        turn_sid = room.order[room.turn_index]

    players_payload = []
    for ps in room.players.values():
        hands_payload = []
        for hi, h in enumerate(ps.hands):
            hands_payload.append({
                "index": hi,
                "cards": h.cards,
                "value": hand_value(h.cards),
                "bet": h.bet,
                "stood": h.stood,
                "busted": h.busted,
                "doubled": h.doubled,
                "blackjack": h.blackjack
            })
        players_payload.append({
            "sid": ps.sid,
            "username": ps.username,
            "ready": ps.ready,
            "insurance_bet": ps.insurance_bet,
            "active_hand": ps.active_hand,
            "hands": hands_payload
        })

    return {
        "room": room.code,
        "phase": room.phase,
        "hostSid": room.host_sid,
        "turnSid": turn_sid,
        "dealerHand": dealer,
        "dealerValue": hand_value(room.dealer_hand) if reveal_dealer else (room.dealer_hand[0]["value"] if room.dealer_hand else 0),
        "rules": {
            "minBet": room.min_bet,
            "maxBet": room.max_bet,
            "numDecks": room.num_decks,
            "allowSplit": room.allow_split,
            "allowDouble": room.allow_double,
            "allowInsurance": room.allow_insurance
        },
        "players": players_payload
    }

def broadcast_room(room: RoomState, reveal_dealer: bool = False):
    socketio.emit("room_state", public_state(room, reveal_dealer), to=room.code)
    logs = [{"ts": r["ts"], "event": r["event"]} for r in fetch_recent_logs(room.code, 60)]
    socketio.emit("room_logs", logs, to=room.code)
    emit_rooms_list()

def next_turn(room: RoomState):
    # sıradaki oyuncu + onun sıradaki eli
    n = len(room.order)
    if n == 0:
        return
    for step in range(1, n + 1):
        idx = (room.turn_index + step) % n
        sid = room.order[idx]
        ps = room.players.get(sid)
        if not ps:
            continue
        # ps içinde bitmemiş el var mı?
        for hi, h in enumerate(ps.hands):
            if not h.stood and not h.busted and not h.blackjack:
                ps.active_hand = hi
                room.turn_index = idx
                return
    room.phase = "dealer"

def all_players_done(room: RoomState):
    for ps in room.players.values():
        for h in ps.hands:
            if not (h.stood or h.busted or h.blackjack):
                return False
    return True

def settle_round(room: RoomState):
    dealer_val = hand_value(room.dealer_hand)
    dealer_bust = dealer_val > 21
    dealer_bj = dealer_has_blackjack(room)

    # Insurance önce çözülür (dealer BJ ise 2:1)
    for ps in list(room.players.values()):
        if ps.insurance_bet > 0:
            u = get_user(ps.username)
            chips = int(u["chips"])
            if dealer_bj:
                payout = ps.insurance_bet * 3  # bet geri + 2:1 kâr => toplam 3x
                chips += payout
                log_event(room.code, f"🛡️ {ps.username} INSURANCE kazandı (+{payout})")
            else:
                log_event(room.code, f"🛡️ {ps.username} INSURANCE kaybetti (-{ps.insurance_bet})")
            ps.insurance_bet = 0
            update_chips(ps.username, chips)

    # Ana eller
    for ps in list(room.players.values()):
        u = get_user(ps.username)
        if not u:
            continue
        chips = int(u["chips"])

        for h in ps.hands:
            pv = hand_value(h.cards)
            bet = h.bet

            payout = 0
            result = "LOSE"

            if h.busted:
                result = "BUST"
                payout = 0
            else:
                if dealer_bj and not h.blackjack:
                    result = "LOSE (dealer BJ)"
                    payout = 0
                elif h.blackjack and not dealer_bj:
                    result = "BLACKJACK"
                    payout = int(bet * 2.5)  # 3:2 dahil
                else:
                    if dealer_bust:
                        result = "WIN (dealer bust)"
                        payout = bet * 2
                    else:
                        if pv > dealer_val:
                            result = "WIN"
                            payout = bet * 2
                        elif pv == dealer_val:
                            result = "PUSH"
                            payout = bet
                        else:
                            result = "LOSE"
                            payout = 0

            chips += payout
            log_event(room.code, f"🧾 {ps.username}: {result} (bet={bet}, payout={payout})")

        update_chips(ps.username, chips)

    room.phase = "finished"

def reset_for_next_round(room: RoomState):
    room.phase = "betting"
    room.deck = create_deck(room.num_decks)
    room.dealer_hand = []
    room.order = []
    room.turn_index = 0
    for ps in room.players.values():
        ps.hands = []
        ps.active_hand = 0
        ps.ready = False
        ps.insurance_bet = 0

# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")

# ---------------- Socket events ----------------
@socketio.on("auth_register")
def auth_register(data):
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if len(username) < 3 or len(password) < 4:
        return emit("auth_result", {"ok": False, "msg": "Kullanıcı adı ≥3, şifre ≥4 olmalı."})
    if get_user(username):
        return emit("auth_result", {"ok": False, "msg": "Bu kullanıcı adı alınmış."})
    create_user(username, password)
    emit("auth_result", {"ok": True, "msg": "Kayıt tamam ✅ Şimdi giriş yap."})

@socketio.on("auth_login")
def auth_login(data):
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    row = get_user(username)
    if not row or not check_password_hash(row["passhash"], password):
        return emit("auth_result", {"ok": False, "msg": "Giriş başarısız."})
    session["username"] = username
    emit("auth_result", {"ok": True, "msg": "Giriş OK ✅", "username": username, "chips": int(row["chips"])})
    emit_rooms_list()

@socketio.on("get_rooms")
def get_rooms():
    emit_rooms_list()

@socketio.on("create_room")
def create_room_evt(data):
    username = session.get("username")
    if not username:
        return emit("toast", "Önce giriş yap.")
    code = (data.get("room") or "").strip().upper()
    if not code or len(code) > 12:
        return emit("toast", "Oda kodu 1-12 karakter.")
    if code in rooms:
        return emit("toast", "Bu oda zaten var.")
    rooms[code] = RoomState(code=code, host_sid=request.sid)
    join_room(code)
    rooms[code].players[request.sid] = PlayerState(username=username, sid=request.sid)
    log_event(code, f"🏠 Oda kuruldu: {code} (host={username})")
    emit("toast", f"Oda oluşturuldu: {code}")
    broadcast_room(rooms[code])

@socketio.on("join_room")
def join_room_evt(data):
    username = session.get("username")
    if not username:
        return emit("toast", "Önce giriş yap.")
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    if not room:
        return emit("toast", "Oda bulunamadı.")
    join_room(code)
    if request.sid not in room.players:
        room.players[request.sid] = PlayerState(username=username, sid=request.sid)
    log_event(code, f"👤 {username} odaya katıldı.")
    emit("toast", f"Odaya girdin: {code}")
    broadcast_room(room)

@socketio.on("leave_room")
def leave_room_evt(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username", "unknown")
    if room:
        room.players.pop(request.sid, None)
        if request.sid in room.order:
            room.order.remove(request.sid)
        if room.host_sid == request.sid:
            room.host_sid = next(iter(room.players.keys()), "")
            log_event(code, f"👑 Host değişti.")
        leave_room(code)
        log_event(code, f"🚪 {username} çıktı.")
        if not room.players:
            rooms.pop(code, None)
        else:
            broadcast_room(room)
    emit_rooms_list()

@socketio.on("chat")
def chat_evt(data):
    username = session.get("username")
    code = (data.get("room") or "").strip().upper()
    msg = (data.get("msg") or "").strip()
    if not username or not msg:
        return
    msg = msg[:200]
    log_event(code, f"💬 {username}: {msg}")
    room = rooms.get(code)
    if room:
        broadcast_room(room)

@socketio.on("set_bet_ready")
def set_bet_ready(data):
    username = session.get("username")
    code = (data.get("room") or "").strip().upper()
    bet = int(data.get("bet") or 0)
    room = rooms.get(code)
    if not username or not room:
        return
    ps = room.players.get(request.sid)
    if not ps:
        return emit("toast", "Odaya katılmadın.")
    if room.phase != "betting":
        return emit("toast", "Şu an bahis aşaması değil.")
    if bet < room.min_bet or bet > room.max_bet:
        return emit("toast", f"Bahis {room.min_bet}-{room.max_bet} arası olmalı.")

    u = get_user(username)
    chips = int(u["chips"]) if u else 0
    if bet > chips:
        return emit("toast", "Yetersiz chip.")

    # sadece hazır işaretle, para tur başlarken düşecek
    ps.hands = [HandState(cards=[], bet=bet)]
    ps.active_hand = 0
    ps.ready = True
    log_event(code, f"🎯 {username} READY, bet={bet}")
    broadcast_room(room)

@socketio.on("start_round")
def start_round(data):
    username = session.get("username")
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    if not username or not room:
        return
    if request.sid != room.host_sid:
        return emit("toast", "Sadece host başlatabilir.")
    if room.phase not in ("betting", "finished"):
        return emit("toast", "Şu an başlatılamaz.")

    players = list(room.players.values())
    if not players:
        return emit("toast", "Oyuncu yok.")
    for ps in players:
        if not ps.ready or not ps.hands or ps.hands[0].bet <= 0:
            return emit("toast", "Herkes READY olmalı ve bahis koymalı.")

    room.deck = create_deck(room.num_decks)
    room.dealer_hand = [pop_card(room), pop_card(room)]

    # bahis düş + dağıt
    for ps in players:
        u = get_user(ps.username)
        chips = int(u["chips"])
        bet = ps.hands[0].bet
        chips -= bet
        update_chips(ps.username, chips)

        ps.hands[0].cards = [pop_card(room), pop_card(room)]
        ps.hands[0].blackjack = is_blackjack(ps.hands[0].cards)
        ps.hands[0].stood = ps.hands[0].blackjack
        ps.hands[0].busted = False
        ps.insurance_bet = 0
        ps.active_hand = 0

    room.order = [ps.sid for ps in players]
    room.turn_index = 0
    room.phase = "playing"
    up = dealer_upcard(room)
    log_event(code, f"🃏 Tur başladı. Dealer upcard: {up['code'] if up else '-'}")

    # Insurance opsiyonunu bildir
    if room.allow_insurance and up and up["rank"] == "A":
        log_event(code, "🛡️ Dealer As açtı: Insurance alınabilir (betin yarısı).")
    # Eğer dealer BJ ise tur hemen biter (insurance çöz + settle)
    if dealer_has_blackjack(room):
        log_event(code, "⚠️ Dealer BLACKJACK!")
        # insurance: oyuncu almışsa sonra settle_round çözer
        room.phase = "finished"
        settle_round(room)
        broadcast_room(room, reveal_dealer=True)
        return

    # blackjack olanlar otomatik stood; eğer herkes blackjack/stand olduysa dealer oynasın
    if all_players_done(room):
        room.phase = "dealer"
    if room.phase == "dealer":
        while hand_value(room.dealer_hand) < 17:
            room.dealer_hand.append(pop_card(room))
        log_event(code, f"🤖 Dealer bitirdi: {hand_value(room.dealer_hand)}")
        settle_round(room)
        broadcast_room(room, reveal_dealer=True)
    else:
        broadcast_room(room)

@socketio.on("insurance")
def take_insurance(data):
    username = session.get("username")
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    if not username or not room:
        return
    if not room.allow_insurance or room.phase != "playing":
        return emit("toast", "Insurance şu an alınamaz.")
    up = dealer_upcard(room)
    if not up or up["rank"] != "A":
        return emit("toast", "Insurance sadece dealer As açarsa.")
    ps = room.players.get(request.sid)
    if not ps or not ps.hands:
        return

    main_bet = ps.hands[0].bet
    ins = int(data.get("amount") or 0)
    max_ins = main_bet // 2
    if ins <= 0 or ins > max_ins:
        return emit("toast", f"Insurance 1 - {max_ins} arası olmalı.")

    u = get_user(username)
    chips = int(u["chips"])
    if ins > chips:
        return emit("toast", "Yetersiz chip.")
    chips -= ins
    update_chips(username, chips)
    ps.insurance_bet = ins
    log_event(code, f"🛡️ {username} insurance aldı: {ins}")
    broadcast_room(room)

def ensure_turn(room: RoomState, sid: str):
    if room.phase != "playing" or not room.order:
        return False
    return room.order[room.turn_index] == sid

def advance_after_action(room: RoomState):
    # Eğer mevcut oyuncunun tüm elleri bitti ise next_turn
    cur_sid = room.order[room.turn_index]
    ps = room.players.get(cur_sid)
    if not ps:
        next_turn(room)
        return
    # oyuncunun bitmemiş eli var mı?
    for hi, h in enumerate(ps.hands):
        if not (h.stood or h.busted or h.blackjack):
            ps.active_hand = hi
            return
    next_turn(room)

@socketio.on("hit")
def hit(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username")
    if not room or not username:
        return
    if not ensure_turn(room, request.sid):
        return emit("toast", "Sıra sende değil.")
    ps = room.players.get(request.sid)
    h = ps.current()
    if h.stood or h.busted or h.blackjack:
        return

    h.cards.append(pop_card(room))
    v = hand_value(h.cards)
    log_event(code, f"➕ {ps.username} HIT ({v})")
    if v > 21:
        h.busted = True
        h.stood = True
        log_event(code, f"💥 {ps.username} BUST!")
        advance_after_action(room)

    if all_players_done(room):
        room.phase = "dealer"

    if room.phase == "dealer":
        while hand_value(room.dealer_hand) < 17:
            room.dealer_hand.append(pop_card(room))
        log_event(code, f"🤖 Dealer bitirdi: {hand_value(room.dealer_hand)}")
        settle_round(room)
        broadcast_room(room, reveal_dealer=True)
    else:
        broadcast_room(room)

@socketio.on("stand")
def stand(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username")
    if not room or not username:
        return
    if not ensure_turn(room, request.sid):
        return emit("toast", "Sıra sende değil.")
    ps = room.players.get(request.sid)
    h = ps.current()
    h.stood = True
    log_event(code, f"🛑 {ps.username} STAND ({hand_value(h.cards)})")

    advance_after_action(room)
    if all_players_done(room):
        room.phase = "dealer"

    if room.phase == "dealer":
        while hand_value(room.dealer_hand) < 17:
            room.dealer_hand.append(pop_card(room))
        log_event(code, f"🤖 Dealer bitirdi: {hand_value(room.dealer_hand)}")
        settle_round(room)
        broadcast_room(room, reveal_dealer=True)
    else:
        broadcast_room(room)

@socketio.on("double")
def double_down(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username")
    if not room or not username:
        return
    if not room.allow_double:
        return emit("toast", "Double kapalı.")
    if not ensure_turn(room, request.sid):
        return emit("toast", "Sıra sende değil.")
    ps = room.players.get(request.sid)
    h = ps.current()
    if not can_double(h.cards) or h.doubled:
        return emit("toast", "Double sadece ilk 2 kartta.")

    u = get_user(username)
    chips = int(u["chips"])
    if h.bet > chips:
        return emit("toast", "Double için yeterli chip yok.")
    chips -= h.bet
    update_chips(username, chips)

    h.bet *= 2
    h.doubled = True
    h.cards.append(pop_card(room))
    v = hand_value(h.cards)
    log_event(code, f"⏫ {ps.username} DOUBLE, bet={h.bet} ({v})")

    # Double sonrası otomatik stand
    if v > 21:
        h.busted = True
        log_event(code, f"💥 {ps.username} BUST (double)!")
    h.stood = True
    advance_after_action(room)

    if all_players_done(room):
        room.phase = "dealer"

    if room.phase == "dealer":
        while hand_value(room.dealer_hand) < 17:
            room.dealer_hand.append(pop_card(room))
        log_event(code, f"🤖 Dealer bitirdi: {hand_value(room.dealer_hand)}")
        settle_round(room)
        broadcast_room(room, reveal_dealer=True)
    else:
        broadcast_room(room)

@socketio.on("split")
def split_hand(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username")
    if not room or not username:
        return
    if not room.allow_split:
        return emit("toast", "Split kapalı.")
    if not ensure_turn(room, request.sid):
        return emit("toast", "Sıra sende değil.")
    ps = room.players.get(request.sid)
    h = ps.current()
    if len(ps.hands) >= 4:
        return emit("toast", "Maks 4 el (limit).")
    if not can_split(h.cards):
        return emit("toast", "Split için iki kart aynı olmalı.")

    u = get_user(username)
    chips = int(u["chips"])
    if h.bet > chips:
        return emit("toast", "Split için yeterli chip yok.")
    chips -= h.bet
    update_chips(username, chips)

    c1, c2 = h.cards[0], h.cards[1]
    # mevcut el -> c1 + yeni kart
    h.cards = [c1, pop_card(room)]
    h.blackjack = is_blackjack(h.cards)
    h.stood = h.blackjack

    # yeni el -> c2 + yeni kart
    new_hand = HandState(cards=[c2, pop_card(room)], bet=h.bet)
    new_hand.blackjack = is_blackjack(new_hand.cards)
    new_hand.stood = new_hand.blackjack

    ps.hands.insert(ps.active_hand + 1, new_hand)
    log_event(code, f"✂️ {ps.username} SPLIT yaptı. El sayısı: {len(ps.hands)}")
    broadcast_room(room)

@socketio.on("next_round")
def next_round(data):
    code = (data.get("room") or "").strip().upper()
    room = rooms.get(code)
    username = session.get("username")
    if not room or not username:
        return
    if request.sid != room.host_sid:
        return emit("toast", "Sadece host yeni tura geçirir.")
    reset_for_next_round(room)
    log_event(code, "🔄 Yeni tur: bahis aşaması.")
    broadcast_room(room)

@socketio.on("disconnect")
def disconnect():
    username = session.get("username", "unknown")
    for code, room in list(rooms.items()):
        if request.sid in room.players:
            room.players.pop(request.sid, None)
            if request.sid in room.order:
                room.order.remove(request.sid)
            if room.host_sid == request.sid:
                room.host_sid = next(iter(room.players.keys()), "")
                log_event(code, "👑 Host değişti (disconnect).")
            log_event(code, f"❌ {username} bağlantısı koptu.")
            if not room.players:
                rooms.pop(code, None)
            else:
                broadcast_room(room)
    emit_rooms_list()

if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000)
