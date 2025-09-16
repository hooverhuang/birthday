# app.py
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import random
import uuid
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

# ===== 遊戲狀態 =====
game_state = {
    "players": {},            # { name: {score, roles, socket_id, guardian_active, mark_target, mark_used_turn, is_admin} }
    "logs": [],
    "current_turn": None,
    "room_id": "game_room_1",
    "game_started": False,
    "max_players": 6,
    "pending_prompt_id": None,   # 當前待拆穿 prompt
    "draw_pile": [],             # 牌庫（抽牌堆）
    "discard_pile": [],          # 棄牌堆
    "turn_marker": None          # 回合標記（壽星加成用一次）
}

connected_players = {}           # sid -> name
pending_prompts = {}             # prompt_id -> {attacker, role, target, had_card, timer, extra}
force_choice_timers = {}         # player_name -> threading.Timer（不放進 game_state，避免序列化錯誤）

# ===== 牌庫（30張，操作型）=====
# 小丑 8、贈禮者 8、狙擊手 5、守護者 4、壽星 3、偵探 2
ROLE_POOL_30 = (
    [" 小丑"] * 8 +
    [" 贈禮者"] * 8 +
    [" 狙擊手"] * 5 +
    [" 守護者"] * 4 +
    [" 壽星"] * 3 +
    ["️ 偵探"] * 2
)

# 顯示名稱
ROLE_DISPLAY_NAMES = {
    " 壽星": "斬魂米娜",
    " 小丑": "海賊王",
    " 贈禮者": "假純愛戰士",
    "️ 偵探": "小菊獸",
    " 守護者": "烈焰雯の魂",
    " 狙擊手": "祖濕爺"
}

ATTACK_ROLES = {" 小丑", " 贈禮者", " 狙擊手"}
TARGET_ROLES = {" 壽星", " 小丑", " 贈禮者", "️ 偵探", " 狙擊手"}  # 需要指定他人的都會進入拆穿
CHALLENGE_TIMEOUT_MS = 5000

# ===== 工具 =====
def log(msg):
    game_state["logs"].append(msg)

def make_public_state():
    # 產生可序列化且不包含內部物件的狀態
    players_clean = {}
    for name, pdata in game_state["players"].items():
        safe = {k: v for k, v in pdata.items() if k not in ("force_choice_timer",)}
        players_clean[name] = safe
    public_state = dict(game_state)
    public_state["players"] = players_clean
    return public_state

def broadcast_state():
    socketio.emit('game_state', make_public_state(), room=game_state['room_id'])

def emit_state_to_sid(sid):
    socketio.emit('game_state', make_public_state(), room=sid)

def emit_to_player(player_name, event, data):
    sid = game_state["players"].get(player_name, {}).get("socket_id")
    if sid:
        socketio.emit(event, data, room=sid)

def get_players_list():
    return list(game_state["players"].keys())

def advance_turn(from_player):
    players = get_players_list()
    if not players:
        game_state["current_turn"] = None
        return
    if from_player in players:
        idx = players.index(from_player)
        next_idx = (idx + 1) % len(players)
        game_state["current_turn"] = players[next_idx]
    else:
        cur = game_state["current_turn"]
        if cur in players:
            idx = players.index(cur)
            next_idx = (idx + 1) % len(players)
            game_state["current_turn"] = players[next_idx]
        else:
            game_state["current_turn"] = players[0]

def safe_remove_one_role(player_name, role):
    roles = game_state["players"][player_name]["roles"]
    if role in roles:
        roles.remove(role)

def discard_to_pile(role):
    game_state["discard_pile"].append(role)

def draw_one():
    if not game_state["draw_pile"] and game_state["discard_pile"]:
        random.shuffle(game_state["discard_pile"])
        game_state["draw_pile"] = game_state["discard_pile"]
        game_state["discard_pile"] = []
    if game_state["draw_pile"]:
        return game_state["draw_pile"].pop()
    return None

# ===== 效果結算（含新規）=====
def apply_mark_bonus_if_any(attacker, role, target, base_damage_to_target):
    p = game_state["players"].get(attacker)
    if not p or role not in ATTACK_ROLES or not target:
        return base_damage_to_target
    mark_target = p.get("mark_target")
    mark_used_turn = p.get("mark_used_turn")
    if mark_target == target and mark_used_turn != game_state.get("turn_marker"):
        p["mark_used_turn"] = game_state.get("turn_marker")
        return base_damage_to_target - 1  # 額外 -1
    return base_damage_to_target

def resolve_effect(attacker, role, target, consume_if_has=True, had_card=False, extra=None):
    is_admin = game_state["players"].get(attacker, {}).get("is_admin", False)
    if is_admin:
        consume_if_has = False

    role_display = ROLE_DISPLAY_NAMES.get(role, role)

    # 守護者：啟動防護（隱形，不記錄公共日誌）
    if role == " 守護者":
        game_state["players"][attacker]["guardian_active"] = True
        if consume_if_has and had_card:
            safe_remove_one_role(attacker, role)
            discard_to_pile(role)
        return

    # 壽星：偷看 + 標記（本回合你的第一張攻擊對該目標額外 -1）
    if role == " 壽星":
        if target in game_state["players"]:
            target_roles = game_state["players"][target]["roles"]
            if target_roles:
                revealed = target_roles[0]
                revealed_display = ROLE_DISPLAY_NAMES.get(revealed, revealed)
                log(f"{attacker} 使用 {role_display} 偷看了 {target} 的一張手牌：{revealed_display}（已標記）")
            else:
                log(f"{attacker} 使用 {role_display} 想偷看 {target}，但對方沒有手牌（仍標記）")
            game_state["players"][attacker]["mark_target"] = target
            game_state["players"][attacker]["mark_used_turn"] = None
        if consume_if_has and had_card:
            safe_remove_one_role(attacker, role)
            discard_to_pile(role)
        return

    # 偵探：公開 + 強制二選一（棄1或-1，5秒逾時自動 -1）
    if role == "️ 偵探":
        if target in game_state["players"]:
            target_roles = game_state["players"][target]["roles"]
            if target_roles:
                revealed = target_roles[0]
                revealed_display = ROLE_DISPLAY_NAMES.get(revealed, revealed)
                log(f"{attacker} 使用 {role_display} → 公開 {target} 的手牌：{revealed_display}")
            else:
                log(f"{attacker} 使用 {role_display} 想公開 {target}，但對方沒有手牌")
            send_force_choice_discard_or_lose1(target)
        if consume_if_has and had_card:
            safe_remove_one_role(attacker, role)
            discard_to_pile(role)
        return

    # 攻擊類（受守護者影響；含壽星標記；狙擊手早保護）
    if role in ATTACK_ROLES and target in game_state["players"]:
        target_state = game_state["players"][target]
        attacker_state = game_state["players"][attacker]

        # 贈禮者模式 B 分散：提前處理
        if role == " 贈禮者" and (extra or {}).get("mode") == "B":
            second_target = (extra or {}).get("second_target")
            targets_hit = []
            if second_target and second_target in game_state["players"] and second_target != target:
                targets_hit = [target, second_target]
            else:
                targets_hit = [target]
            for idx, t in enumerate(targets_hit):
                if game_state["players"][t]["guardian_active"]:
                    attacker_state["score"] -= 1
                    game_state["players"][t]["guardian_active"] = False
                    log(f"{attacker} 使用 {role_display} 攻擊 {t}，但被 {ROLE_DISPLAY_NAMES.get(' 守護者','守護者')} 擋下！{attacker} 受到反擊 -1 分")
                else:
                    base = -1
                    adj = base
                    if idx == 0:
                        adj = apply_mark_bonus_if_any(attacker, role, t, base)
                    game_state["players"][t]["score"] += adj
                    log(f"{attacker} 使用 {role_display}（分散） → {t} {adj} 分")
            if consume_if_has and had_card:
                safe_remove_one_role(attacker, role)
                discard_to_pile(role)
            return

        # 其他攻擊類
        dmg_to_target = 0
        self_penalty = 0
        if role == " 小丑":
            dmg_to_target = -2
            self_penalty = -1
        elif role == " 贈禮者":
            attacker_state["score"] += 1  # 模式 A
            dmg_to_target = -1
        elif role == " 狙擊手":
            dmg_to_target = -3
            self_penalty = -1
            if target_state["score"] > 80:
                dmg_to_target = -2  # 早期保護

        if target_state["guardian_active"]:
            attacker_state["score"] += (-2 if role == " 狙擊手" else -1)
            target_state["guardian_active"] = False
            log(f"{attacker} 使用 {role_display} 攻擊 {target}，但被 {ROLE_DISPLAY_NAMES.get(' 守護者','守護者')} 擋下！{attacker} 受到反擊 {'-2' if role == ' 狙擊手' else '-1'} 分")
        else:
            dmg_to_target = apply_mark_bonus_if_any(attacker, role, target, dmg_to_target)
            target_state["score"] += dmg_to_target
            if self_penalty:
                attacker_state["score"] += self_penalty
            if role == " 贈禮者" and (extra or {}).get("mode") == "A":
                log(f"{attacker} 使用 {role_display} → 自己 +1 分，{target} {dmg_to_target} 分")
            elif role == " 小丑":
                log(f"{attacker} 使用 {role_display} → {target} {dmg_to_target} 分，自己 {self_penalty} 分")
            elif role == " 狙擊手":
                log(f"{attacker} 使用 {role_display} → {target} {dmg_to_target} 分，自己 {self_penalty} 分")
            else:
                log(f"{attacker} 使用 {role_display} → {target} {dmg_to_target} 分")

        if consume_if_has and had_card:
            safe_remove_one_role(attacker, role)
            discard_to_pile(role)
        return

# ===== 偵探的強制選擇（棄1 或 -1）=====
def send_force_choice_discard_or_lose1(target):
    prompt_id = f"force_{uuid.uuid4()}"

    def on_timeout():
        # 逾時：自動 -1
        force_choice_timers.pop(target, None)
        if game_state["players"].get(target):
            game_state["players"][target]["score"] -= 1
            log(f"{target} 未在時限內選擇（偵探），自動 -1 分")
            broadcast_state()

    timer = threading.Timer(CHALLENGE_TIMEOUT_MS/1000.0, on_timeout)
    timer.start()
    force_choice_timers[target] = timer

    emit_to_player(target, 'force_choice', {
        "prompt_id": prompt_id,
        "timeout_ms": CHALLENGE_TIMEOUT_MS
    })

@socketio.on('force_choice_answer')
def handle_force_choice_answer(data):
    player = data.get("player")
    choice = data.get("choice")
    discard_role = data.get("discard_role")
    p = game_state["players"].get(player)
    if not p:
        return
    timer = force_choice_timers.pop(player, None)
    if timer:
        try:
            timer.cancel()
        except Exception:
            pass

    if choice == "discard_one":
        if p["roles"]:
            role_to_discard = discard_role if discard_role in p["roles"] else p["roles"][0]
            p["roles"].remove(role_to_discard)
            discard_to_pile(role_to_discard)
            log(f"{player} 在偵探強制下丟掉了 1 張牌")
        else:
            p["score"] -= 1
            log(f"{player} 沒有手牌可丟，改為 -1 分")
    else:
        p["score"] -= 1
        log(f"{player} 選擇 -1 分")
    broadcast_state()

# ===== 拆穿（統一流程 + 傳遞 extra）=====
def finish_prompt(prompt_id, advance_from_player=None):
    if prompt_id in pending_prompts:
        timer = pending_prompts[prompt_id].get("timer")
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        pending_prompts.pop(prompt_id, None)
    if game_state.get("pending_prompt_id") == prompt_id:
        game_state["pending_prompt_id"] = None
    if advance_from_player:
        advance_turn(advance_from_player)
        game_state["turn_marker"] = str(uuid.uuid4())  # 新回合標記
    socketio.emit('game_state', make_public_state(), room=game_state['room_id'])

def on_not_call_internal(prompt_id, silent=False):
    prompt = pending_prompts.get(prompt_id)
    if not prompt:
        return
    attacker = prompt["attacker"]
    role = prompt["role"]
    target = prompt["target"]
    had_card = prompt["had_card"]
    extra = prompt.get("extra")

    socketio.emit('bluff_result', {
        "success": True,
        "message": f"{target} {'逾時未' if silent else '未'}揭穿，{attacker} 的行動生效"
    }, room=game_state['room_id'])

    resolve_effect(attacker, role, target, consume_if_has=True, had_card=had_card, extra=extra)
    finish_prompt(prompt_id, advance_from_player=attacker)

def create_challenge_prompt(attacker, role, target, extra=None):
    prompt_id = str(uuid.uuid4())
    is_admin = game_state["players"].get(attacker, {}).get("is_admin", False)
    had_card = True if is_admin else (role in game_state["players"][attacker]["roles"])

    def on_timeout():
        if prompt_id in pending_prompts:
            on_not_call_internal(prompt_id, silent=True)

    timer = threading.Timer(CHALLENGE_TIMEOUT_MS/1000.0, on_timeout)
    pending_prompts[prompt_id] = {
        "attacker": attacker,
        "role": role,
        "target": target,
        "had_card": had_card,
        "timer": timer,
        "extra": extra
    }
    game_state["pending_prompt_id"] = prompt_id
    timer.start()
    emit_to_player(target, 'bluff_challenge', {
        "prompt_id": prompt_id,
        "player": attacker,
        "role": role,
        "target": target,
        "timeout_ms": CHALLENGE_TIMEOUT_MS
    })

# ===== 路由與事件 =====
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/state")
def state():
    return jsonify(make_public_state())

@socketio.on('connect')
def handle_connect():
    socketio.emit('connected', {'message': '連線成功'}, room=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in connected_players:
        player_name = connected_players[sid]

        # 拆穿中的情境處理
        for pid, p in list(pending_prompts.items()):
            if p["target"] == player_name:
                on_not_call_internal(pid, silent=True)
            elif p["attacker"] == player_name:
                socketio.emit('bluff_result', {
                    "success": False,
                    "message": f"{player_name} 斷線，該次行動取消"
                }, room=game_state['room_id'])
                finish_prompt(pid, advance_from_player=player_name)

        # 偵探選擇計時器取消
        t = force_choice_timers.pop(player_name, None)
        if t:
            try: t.cancel()
            except Exception: pass

        # 移除玩家
        if player_name in game_state["players"]:
            del game_state["players"][player_name]
        del connected_players[sid]

        socketio.emit('player_left', {'player': player_name}, room=game_state['room_id'])
        if game_state["current_turn"] == player_name:
            advance_turn(player_name)
            game_state["turn_marker"] = str(uuid.uuid4())
        broadcast_state()

@socketio.on('join_game')
def handle_join_game(data):
    player_name = data.get('player_name', '').strip()
    if not player_name:
        socketio.emit('error', {'message': '請輸入玩家名稱'}, room=request.sid); return
    if len(game_state["players"]) >= game_state["max_players"]:
        socketio.emit('error', {'message': '遊戲已滿，無法加入'}, room=request.sid); return
    if player_name in game_state["players"]:
        socketio.emit('error', {'message': '玩家名稱已存在'}, room=request.sid); return

    connected_players[request.sid] = player_name
    game_state["players"][player_name] = {
        "score": 100,
        "roles": [],
        "socket_id": request.sid,
        "guardian_active": False,
        "mark_target": None,
        "mark_used_turn": None,
        "is_admin": (player_name.lower() == 'admin')
    }
    join_room(game_state['room_id'])
    socketio.emit('player_joined', {'player': player_name, 'total_players': len(game_state["players"])}, room=game_state['room_id'])
    broadcast_state()

@socketio.on('start_game')
def handle_start_game():
    if len(game_state["players"]) < 2:
        socketio.emit('error', {'message': '至少需要2個玩家才能開始遊戲'}, room=request.sid); return

    deck = ROLE_POOL_30.copy()
    random.shuffle(deck)

    player_names = list(game_state["players"].keys())
    random.shuffle(player_names)

    cards_per_player = 5
    for i, name in enumerate(player_names):
        start = i * cards_per_player
        end = start + cards_per_player
        game_state["players"][name]["roles"] = deck[start:end]
        game_state["players"][name]["guardian_active"] = False
        game_state["players"][name]["mark_target"] = None
        game_state["players"][name]["mark_used_turn"] = None

    game_state["draw_pile"] = deck[len(player_names)*cards_per_player:]
    game_state["discard_pile"] = []

    game_state["current_turn"] = player_names[0]
    game_state["game_started"] = True
    game_state["turn_marker"] = str(uuid.uuid4())

    socketio.emit('game_started', {'message': '遊戲開始！每個玩家獲得5張牌（30張操作型）'}, room=game_state['room_id'])
    broadcast_state()

@socketio.on('get_game_state')
def handle_get_game_state():
    emit_state_to_sid(request.sid)

@socketio.on('get_my_cards')
def handle_get_my_cards(data):
    player = data.get('player')
    if player in game_state["players"]:
        if game_state["players"][player].get("is_admin"):
            admin_cards = [" 壽星"," 小丑"," 贈禮者","️ 偵探"," 守護者"," 狙擊手"]
            socketio.emit('my_cards', {'cards': admin_cards}, room=request.sid)
        else:
            socketio.emit('my_cards', {'cards': game_state["players"][player]["roles"]}, room=request.sid)

@socketio.on('play_card')
def handle_play_card(data):
    if not game_state["game_started"]:
        socketio.emit('error', {'message': '遊戲尚未開始'}, room=request.sid); return

    player = data.get('player')
    role = data.get('role')
    target = data.get('target')
    extra = data.get('extra') or {}

    if player not in game_state["players"]:
        socketio.emit('error', {'message': '未知玩家'}, room=request.sid); return
    if game_state['current_turn'] != player:
        socketio.emit('error', {'message': '還沒輪到你！'}, room=request.sid); return
    if game_state.get("pending_prompt_id"):
        socketio.emit('error', {'message': '上一個行動待拆穿中，請稍候'}, room=request.sid); return

    game_state["turn_marker"] = game_state.get("turn_marker") or str(uuid.uuid4())

    if role in TARGET_ROLES:
        if not target or target not in game_state["players"]:
            socketio.emit('error', {'message': '請選擇有效目標'}, room=request.sid); return
        pending_extra = extra if role == " 贈禮者" else None
        create_challenge_prompt(player, role, target, extra=pending_extra)
        log(f"{player} 宣告對 {target} 使用 {ROLE_DISPLAY_NAMES.get(role, role)}（等待是否揭穿）")
        broadcast_state()
        return

    if role == " 守護者":
        # 管理員或一般玩家都可直接啟動，且回合結束
        if (not game_state["players"][player].get("is_admin")) and role not in game_state["players"][player]["roles"]:
            socketio.emit('error', {'message': '你沒有這張卡'}, room=request.sid); return
        had = True if game_state["players"][player].get("is_admin") else True
        resolve_effect(player, role, None, consume_if_has=True, had_card=had)
        advance_turn(player)
        game_state["turn_marker"] = str(uuid.uuid4())
        broadcast_state()
        return

    socketio.emit('error', {'message': '未知的卡片/用法'}, room=request.sid)

@socketio.on('call_bluff')
def handle_call_bluff(data):
    prompt_id = data.get('prompt_id')
    player = data.get('player')
    prompt = pending_prompts.get(prompt_id)
    if not prompt:
        return
    attacker = prompt["attacker"]; role = prompt["role"]; target = prompt["target"]
    had_card = prompt["had_card"]; extra = prompt.get("extra")
    if player != target:
        return

    timer = prompt.get("timer")
    if timer:
        try: timer.cancel()
        except Exception: pass

    role_display = ROLE_DISPLAY_NAMES.get(role, role)
    if not had_card:
        game_state["players"][attacker]["score"] -= 5
        msg = f"{target} 揭穿成功！{attacker} 並未持有 {role_display}，{attacker} -5 分，效果取消"
        log(msg)
        socketio.emit('bluff_result', {"success": True, "message": msg}, room=game_state['room_id'])
        finish_prompt(prompt_id, advance_from_player=attacker)
    else:
        game_state["players"][target]["score"] -= 3
        msg = f"{target} 揭穿失敗！{attacker} 確實持有 {role_display}，{target} -3 分，效果生效"
        log(msg)
        socketio.emit('bluff_result', {"success": False, "message": msg}, room=game_state['room_id'])
        resolve_effect(attacker, role, target, consume_if_has=True, had_card=True, extra=extra)
        finish_prompt(prompt_id, advance_from_player=attacker)

@socketio.on('not_call_bluff')
def handle_not_call_bluff(data):
    prompt_id = data.get('prompt_id')
    player = data.get('player')
    prompt = pending_prompts.get(prompt_id)
    if not prompt:
        return
    if player != prompt["target"]:
        return

    timer = prompt.get("timer")
    if timer:
        try: timer.cancel()
        except Exception: pass
    on_not_call_internal(prompt_id, silent=False)

# 可選：結束回合 / 棄1抽1
@socketio.on('end_turn_discard_draw')
def handle_end_turn_discard_draw(data):
    player = data.get("player")
    discard_role = data.get("discard_role")
    if game_state['current_turn'] != player:
        socketio.emit('error', {'message': '不是你的回合'}, room=request.sid); return

    if discard_role and discard_role in game_state["players"][player]["roles"]:
        game_state["players"][player]["roles"].remove(discard_role)
        discard_to_pile(discard_role)
        drawn = draw_one()
        if drawn:
            game_state["players"][player]["roles"].append(drawn)
            log(f"{player} 棄1抽1：丟掉一張並抽到一張新牌")
        else:
            log(f"{player} 棄1未抽到牌（牌庫不足）")
    else:
        log(f"{player} 結束回合（未棄牌）")

    advance_turn(player)
    game_state["turn_marker"] = str(uuid.uuid4())
    broadcast_state()

# ===== 管理員：重啟遊戲 =====
@socketio.on('admin_reset_game')
def handle_admin_reset_game(data):
    player = data.get('player')
    if not player or not game_state["players"].get(player, {}).get("is_admin"):
        socketio.emit('error', {'message': '你不是管理員'}, room=request.sid); return

    # 停掉所有計時器與清空提示
    for pid, p in list(pending_prompts.items()):
        t = p.get("timer")
        if t:
            try: t.cancel()
            except Exception: pass
        pending_prompts.pop(pid, None)
    for name, t in list(force_choice_timers.items()):
        try: t.cancel()
        except Exception: pass
        force_choice_timers.pop(name, None)
    game_state["pending_prompt_id"] = None

    # 重新洗牌與發牌、重置分數與狀態
    deck = ROLE_POOL_30.copy()
    random.shuffle(deck)
    player_names = list(game_state["players"].keys())
    random.shuffle(player_names)
    cards_per_player = 5
    for i, name in enumerate(player_names):
        start = i * cards_per_player
        end = start + cards_per_player
        game_state["players"][name]["roles"] = deck[start:end]
        game_state["players"][name]["score"] = 100
        game_state["players"][name]["guardian_active"] = False
        game_state["players"][name]["mark_target"] = None
        game_state["players"][name]["mark_used_turn"] = None

    game_state["draw_pile"] = deck[len(player_names)*cards_per_player:]
    game_state["discard_pile"] = []
    game_state["current_turn"] = player_names[0] if player_names else None
    game_state["game_started"] = True
    game_state["turn_marker"] = str(uuid.uuid4())
    game_state["logs"] = []

    socketio.emit('game_started', {'message': '管理員已重啟遊戲'}, room=game_state['room_id'])
    broadcast_state()

if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)