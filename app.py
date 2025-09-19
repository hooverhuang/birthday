import random
import time
import uuid
import threading
from copy import deepcopy
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# ====== 常數與卡池 ======
MAX_PLAYERS = 6
CARDS_PER_PLAYER = 5
MAX_ROUNDS = 3
GAME_TIME_LIMIT_SEC = 30 * 60
BLUFF_TIMEOUT_MS = 5000

# 注意：鍵名需與前端一致（包含空白/特殊字元）
CARD_TYPES = [" 壽星", " 小丑", " 贈禮者", "️ 偵探", " 守護者", " 狙擊手"]
ROLE_POOL_30 = []
for c in CARD_TYPES:
    ROLE_POOL_30 += [c] * 5  # 6 種 x 5 = 30

ROLE_DISPLAY_NAMES = {
    " 壽星": "斬魂米娜",
    " 小丑": "海賊王",
    " 贈禮者": "假純愛戰士",
    "️ 偵探": "小菊獸",
    " 守護者": "烈焰雯の魂",
    " 狙擊手": "祖濕爺",
}

# ====== 全域狀態與輔助容器（Timer 不可放進可序列化狀態）======
pending_prompts = {}       # prompt_id -> {player, target, role, had_card, extra, timer}
force_choice_timers = {}   # player_name -> Timer

def new_game_state():
    return {
        "room_id": "main",
        "players": {},          # name -> {roles, score, guardian_active, mark_target, mark_used_turn, is_admin, stats}
        "player_order": [],     # 出手順序
        "current_turn": None,
        "draw_pile": [],
        "discard_pile": [],
        "logs": [],
        "game_started": False,
        "max_players": MAX_PLAYERS,
        "turn_index": 0,        # 累計已進行的出手次數
        "max_rounds": MAX_ROUNDS,
        "start_ts": None,
        "deadline_ts": None,
        "pending_prompt_id": None,
        "turn_marker": None,
    }

game_state = new_game_state()

# ====== 工具函數 ======
def now_ts():
    return int(time.time())

def game_time_remaining_ms():
    if not game_state["game_started"] or not game_state["deadline_ts"]:
        return None
    remain = max(0, game_state["deadline_ts"] - now_ts())
    return remain * 1000

def num_players():
    return len(game_state["player_order"])

def round_number():
    if not game_state["game_started"] or num_players() == 0:
        return 0
    # 以「每人一次出手」為 1 輪
    return (game_state["turn_index"] // max(1, num_players())) + 1

def add_log(text):
    game_state["logs"].append(text)
    if len(game_state["logs"]) > 200:
        game_state["logs"] = game_state["logs"][-200:]

def next_player_name(curr):
    order = game_state["player_order"]
    if not order:
        return None
    if curr not in order:
        return order[0]
    i = (order.index(curr) + 1) % len(order)
    return order[i]

def sanitize_players_for_emit(players_raw):
    players_clean = {}
    for name, p in players_raw.items():
        q = dict(p)
        # 確保沒有 Timer 類型進入
        q.pop("force_choice_timer", None)
        players_clean[name] = q
    return players_clean

def broadcast_state():
    state = {
        "room_id": game_state["room_id"],
        "players": sanitize_players_for_emit(game_state["players"]),
        "player_order": list(game_state["player_order"]),
        "current_turn": game_state["current_turn"],
        "draw_pile_count": len(game_state["draw_pile"]),
        "discard_pile_count": len(game_state["discard_pile"]),
        "logs": list(game_state["logs"]),
        "game_started": game_state["game_started"],
        "max_players": game_state["max_players"],
        "turn_index": game_state["turn_index"],
        "max_rounds": game_state["max_rounds"],
        "round_number": round_number(),
        "time_remaining_ms": game_time_remaining_ms(),
        "turn_marker": game_state["turn_marker"],
    }
    socketio.emit("game_state", state, room=game_state["room_id"])

def finish_all_timers():
    for pid, p in list(pending_prompts.items()):
        t = p.get("timer")
        if t:
            try:
                t.cancel()
            except Exception:
                pass
        pending_prompts.pop(pid, None)
    for name, t in list(force_choice_timers.items()):
        try:
            t.cancel()
        except Exception:
            pass
        force_choice_timers.pop(name, None)
    game_state["pending_prompt_id"] = None

def end_game(reason=""):
    # 排名：分數降序；同分 → 成功拆穿 > 成功虛張 > 剩餘手牌多
    results = []
    for name, p in game_state["players"].items():
        results.append({
            "player": name,
            "score": p.get("score", 0),
            "call_bluff_success": p.get("stats", {}).get("call_bluff_success", 0),
            "bluff_success": p.get("stats", {}).get("bluff_success", 0),
            "hand_count": len(p.get("roles", [])),
        })
    results.sort(key=lambda r: (-r["score"], -r["call_bluff_success"], -r["bluff_success"], -r["hand_count"]))

    game_state["game_started"] = False
    add_log(f"【遊戲結束】{reason or '條件達成'}")
    broadcast_state()
    socketio.emit("game_over", {
        "reason": reason or "條件達成",
        "results": results,
    }, room=game_state["room_id"])

def check_end_conditions(trigger=""):
    # 提前結束：有人 <= 0
    for name, p in game_state["players"].items():
        if p.get("score", 0) <= 0:
            end_game(f"{name} 分數歸零")
            return True
    # 輪數上限
    if round_number() > game_state["max_rounds"]:
        end_game("超過最大輪數")
        return True
    # 時限
    if game_state["deadline_ts"] and now_ts() >= game_state["deadline_ts"]:
        end_game("超過 30 分鐘時限")
        return True
    return False

def advance_turn(advance_from=None):
    # 計入當前玩家已出手次數
    if advance_from and advance_from in game_state["players"]:
        st = game_state["players"][advance_from].setdefault("stats", {})
        st["turns_taken"] = st.get("turns_taken", 0) + 1

    nxt = next_player_name(game_state["current_turn"])
    game_state["current_turn"] = nxt
    game_state["turn_index"] += 1
    game_state["turn_marker"] = str(uuid.uuid4())
    broadcast_state()
    check_end_conditions("advance_turn")

# ====== 牌效與行為（精簡版，保留你已有的效果邏輯再置換也可）======
def apply_guardian_counter(attacker_name, target_name, role_used):
    # 擋下攻擊，反擊；若擋祖濕爺，反擊 -2，其他 -1
    atk = game_state["players"].get(attacker_name)
    tgt = game_state["players"].get(target_name)
    if not atk or not tgt:
        return
    dmg = 2 if role_used == " 狙擊手" else 1
    atk["score"] -= dmg
    tgt["guardian_active"] = False
    add_log(f"{target_name} 的『{ROLE_DISPLAY_NAMES[' 守護者']}』觸發，{attacker_name} 受到 -{dmg}")

def resolve_effect(attacker, role, target, extra=None, consume_if_has=True):
    # 管理員：不消耗手牌
    if consume_if_has and not game_state["players"][attacker].get("is_admin"):
        roles = game_state["players"][attacker]["roles"]
        try:
            roles.remove(role)
            game_state["discard_pile"].append(role)
        except ValueError:
            pass

    A = game_state["players"].get(attacker)
    T = game_state["players"].get(target) if target else None
    if not A:
        return

    # 守護者：啟動（隱形啟動，不寫入公開日誌）
    if role == " 守護者":
        A["guardian_active"] = True
        return

    # 需要目標的卡，先判斷守護者是否擋下（流程：先處理拆穿 → 若行動生效才判定守護者）
    if T and T.get("guardian_active"):
        apply_guardian_counter(attacker, target, role)
        return

    # 斬魂米娜：標記
    if role == " 壽星" and T:
        A["mark_target"] = target
        A["mark_used_turn"] = round_number()
        add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 壽星']}』標記了 {target}")

    # 偵探：強制選擇（公開/棄1或-1）
    elif role == "️ 偵探" and T:
        add_log(f"{attacker} 對 {target} 使用『{ROLE_DISPLAY_NAMES['️ 偵探']}』")
        # 強制選擇的 UI 已在前端，用事件通知目標
        socketio.emit("force_choice", {"timeout_ms": 5000, "target": target}, room=game_state["room_id"])

    # 小丑：-2，自身 -1；若被標記者為目標，總傷害不超過 -3（此處保守處理）
    elif role == " 小丑" and T:
        dmg = 2
        if A.get("mark_target") == target and A.get("mark_used_turn") == round_number():
            dmg = min(3, dmg + 1)  # 額外 -1，但上限 -3
        T["score"] -= dmg
        A["score"] -= 1
        add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 小丑']}』→ {target} -{dmg}，{attacker} -1")

    # 贈禮者：A 自+1目標-1；B 兩個不同目標各-1
    elif role == " 贈禮者" and T:
        mode = (extra or {}).get("mode", "A")
        if mode == "B":
            second_target = (extra or {}).get("second_target")
            if second_target and second_target != target and second_target in game_state["players"]:
                game_state["players"][target]["score"] -= 1
                game_state["players"][second_target]["score"] -= 1
                add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 贈禮者']}』(B) → {target} -1、{second_target} -1")
            else:
                # 無效參數時，降級為 A
                game_state["players"][attacker]["score"] += 1
                game_state["players"][target]["score"] -= 1
                add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 贈禮者']}』(A) → {attacker} +1、{target} -1")
        else:
            game_state["players"][attacker]["score"] += 1
            game_state["players"][target]["score"] -= 1
            add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 贈禮者']}』(A) → {attacker} +1、{target} -1")

    # 祖濕爺：目標 -3，自損 -1；若目標分數>80 則改為 -2
    elif role == " 狙擊手" and T:
        dmg = 3
        if T["score"] > 80:
            dmg = 2
        T["score"] -= dmg
        A["score"] -= 1
        add_log(f"{attacker} 使用『{ROLE_DISPLAY_NAMES[' 狙擊手']}』→ {target} -{dmg}，{attacker} -1")

# ====== Bluff（虛張）處理 ======
def create_challenge_prompt(attacker, role, target, extra=None, had_card=None):
    # had_card = None → 由手牌判斷；管理員必定 True
    if had_card is None:
        if game_state["players"].get(attacker, {}).get("is_admin"):
            had_card = True
        else:
            had_card = role in game_state["players"].get(attacker, {}).get("roles", [])

    prompt_id = str(uuid.uuid4())
    timer = threading.Timer(BLUFF_TIMEOUT_MS / 1000.0, on_prompt_timeout, args=(prompt_id, attacker))
    pending_prompts[prompt_id] = {
        "player": attacker,
        "target": target,
        "role": role,
        "extra": extra,
        "had_card": had_card,
        "timer": timer,
    }
    game_state["pending_prompt_id"] = prompt_id
    timer.start()

    socketio.emit("bluff_challenge", {
        "prompt_id": prompt_id,
        "player": attacker,
        "target": target,
        "role": role,
        "timeout_ms": BLUFF_TIMEOUT_MS
    }, room=game_state["room_id"])

def on_prompt_timeout(prompt_id, attacker):
    # 超時視為不揭穿
    on_not_call_internal(prompt_id, silent=True, advance_from_player=attacker)

def finish_prompt(prompt_id, advance_from_player=None):
    info = pending_prompts.pop(prompt_id, None)
    if info and info.get("timer"):
        try:
            info["timer"].cancel()
        except Exception:
            pass
    game_state["pending_prompt_id"] = None
    broadcast_state()
    if advance_from_player:
        advance_turn(advance_from_player)

def on_not_call_internal(prompt_id, silent=False, advance_from_player=None):
    info = pending_prompts.get(prompt_id)
    if not info:
        return
    attacker = info["player"]
    role = info["role"]
    target = info["target"]
    extra = info.get("extra")
    had_card = info["had_card"]

    # 不揭穿：若有牌 → 效果生效；若無牌 → 虛張成功，依規則 -2/-?（你先前定義過 -2）
    if had_card:
        resolve_effect(attacker, role, target, extra, consume_if_has=not game_state["players"][attacker].get("is_admin"))
        msg = f"{attacker} 的行動生效：{ROLE_DISPLAY_NAMES.get(role, role)}"
        succ = True
    else:
        # 虛張成功：目標受影響（此遊戲規則：不揭穿則視同真的；此處僅記錄成功次數）
        st = game_state["players"][attacker].setdefault("stats", {})
        st["bluff_success"] = st.get("bluff_success", 0) + 1
        resolve_effect(attacker, role, target, extra, consume_if_has=False)
        msg = f"{attacker} 的虛張成功，行動生效：{ROLE_DISPLAY_NAMES.get(role, role)}"
        succ = True

    socketio.emit("bluff_result", {"success": succ, "message": msg}, room=game_state["room_id"])
    finish_prompt(prompt_id, advance_from_player=attacker)

# ====== Flask 與 Socket 事件 ======
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/state")
def http_state():
    state = {
        "players": sanitize_players_for_emit(game_state["players"]),
        "player_order": list(game_state["player_order"]),
        "current_turn": game_state["current_turn"],
        "logs": list(game_state["logs"]),
        "game_started": game_state["game_started"],
        "max_rounds": game_state["max_rounds"],
        "round_number": round_number(),
        "time_remaining_ms": game_time_remaining_ms(),
    }
    return jsonify(state)

@socketio.on("connect")
def on_connect():
    join_room(game_state["room_id"])
    broadcast_state()

@socketio.on("disconnect")
def on_disconnect():
    pass

@socketio.on("join_game")
def handle_join(data):
    name = (data or {}).get("player_name", "").strip()
    if not name:
        emit("error", {"message": "請提供玩家名稱"}, room=request.sid); return
    if game_state["game_started"]:
        emit("error", {"message": "遊戲已開始，暫不接受新玩家"}, room=request.sid); return
    if len(game_state["players"]) >= game_state["max_players"]:
        emit("error", {"message": "玩家已滿"}, room=request.sid); return
    if name in game_state["players"]:
        emit("error", {"message": "名稱已存在"}, room=request.sid); return

    game_state["players"][name] = {
        "roles": [],
        "score": 100,
        "guardian_active": False,
        "mark_target": None,
        "mark_used_turn": None,
        "is_admin": name.lower() == "admin",
        "stats": {"call_bluff_success": 0, "bluff_success": 0, "turns_taken": 0},
    }
    game_state["player_order"].append(name)
    add_log(f"{name} 加入了遊戲")
    socketio.emit("player_joined", {"player": name, "total_players": len(game_state["players"])}, room=game_state["room_id"])
    broadcast_state()

@socketio.on("start_game")
def handle_start():
    if game_state["game_started"]:
        return
    if len(game_state["players"]) < 2:
        emit("error", {"message": "至少需要 2 位玩家"}, room=request.sid); return

    # 重置
    finish_all_timers()
    deck = ROLE_POOL_30.copy()
    random.shuffle(deck)

    names = list(game_state["players"].keys())
    random.shuffle(names)
    game_state["player_order"] = names

    for i, name in enumerate(names):
        start = i * CARDS_PER_PLAYER
        end = start + CARDS_PER_PLAYER
        game_state["players"][name]["roles"] = deck[start:end]
        game_state["players"][name]["score"] = 100
        game_state["players"][name]["guardian_active"] = False
        game_state["players"][name]["mark_target"] = None
        game_state["players"][name]["mark_used_turn"] = None
        game_state["players"][name]["stats"] = {"call_bluff_success": 0, "bluff_success": 0, "turns_taken": 0}

    game_state["draw_pile"] = deck[len(names) * CARDS_PER_PLAYER:]
    game_state["discard_pile"] = []
    game_state["current_turn"] = names[0] if names else None
    game_state["turn_index"] = 0
    game_state["game_started"] = True
    game_state["start_ts"] = now_ts()
    game_state["deadline_ts"] = game_state["start_ts"] + GAME_TIME_LIMIT_SEC
    game_state["turn_marker"] = str(uuid.uuid4())
    game_state["logs"] = []
    add_log("遊戲開始！")
    socketio.emit("game_started", {"message": "遊戲已開始"}, room=game_state["room_id"])
    broadcast_state()

@socketio.on("get_my_cards")
def handle_get_my_cards(data):
    player = (data or {}).get("player")
    if not player or player not in game_state["players"]:
        return
    if game_state["players"][player].get("is_admin"):
        # 管理員顯示所有卡
        cards = CARD_TYPES.copy()
    else:
        cards = list(game_state["players"][player]["roles"])
    emit("my_cards", {"cards": cards}, room=request.sid)

@socketio.on("play_card")
def handle_play_card(data):
    if not game_state["game_started"]:
        emit("error", {"message": "遊戲未開始"}, room=request.sid); return
    attacker = (data or {}).get("player")
    role = (data or {}).get("role")
    target = (data or {}).get("target")
    is_bluff = bool((data or {}).get("is_bluff"))
    extra = (data or {}).get("extra") or {}

    if attacker != game_state["current_turn"]:
        emit("error", {"message": "不是你的回合"}, room=request.sid); return
    if attacker not in game_state["players"]:
        return

    # 管理員使用守護者可跳過手牌檢查
    has_card = True
    if not game_state["players"][attacker].get("is_admin") or role != " 守護者":
        has_card = role in game_state["players"][attacker]["roles"]

    # 先建立拆穿提示（所有有目標的卡都能被拆穿；守護者無目標，直接生效）
    if role == " 守護者":
        resolve_effect(attacker, role, None, extra, consume_if_has=not game_state["players"][attacker].get("is_admin"))
        broadcast_state()
        advance_turn(advance_from=attacker)
        return

    if not target or target not in game_state["players"]:
        emit("error", {"message": "請選擇有效目標"}, room=request.sid); return

    # 一律發出拆穿提示（守護者已排除），管理員視為 had_card=True
    create_challenge_prompt(attacker, role, target, extra, had_card=has_card if not is_bluff else False)
    broadcast_state()

@socketio.on("call_bluff")
def handle_call_bluff(data):
    pid = (data or {}).get("prompt_id")
    player = (data or {}).get("player")  # 誰按揭穿
    info = pending_prompts.get(pid)
    if not info:
        return
    attacker = info["player"]
    role = info["role"]
    target = info["target"]
    had_card = info["had_card"]

    # 揭穿：若對方無牌 → 揭穿成功，對方 -5；若對方有牌 → 揭穿失敗，自己 -2，效果生效
    if not had_card:
        game_state["players"][attacker]["score"] -= 5
        st = game_state["players"].setdefault(player, {}).setdefault("stats", {})
        st["call_bluff_success"] = st.get("call_bluff_success", 0) + 1
        msg = f"{player} 成功揭穿 {attacker}！{attacker} -5"
        succ = True
        socketio.emit("bluff_result", {"success": succ, "message": msg}, room=game_state["room_id"])
        finish_prompt(pid, advance_from_player=attacker)
    else:
        # 揭穿失敗：自己 -2，且行動生效
        game_state["players"][player]["score"] -= 2
        resolve_effect(attacker, role, target, info.get("extra"), consume_if_has=not game_state["players"][attacker].get("is_admin"))
        msg = f"{player} 揭穿失敗！{player} -2，{attacker} 的行動生效"
        succ = False
        socketio.emit("bluff_result", {"success": succ, "message": msg}, room=game_state["room_id"])
        finish_prompt(pid, advance_from_player=attacker)

@socketio.on("not_call_bluff")
def handle_not_call_bluff(data):
    pid = (data or {}).get("prompt_id")
    attacker = (data or {}).get("player")  # 這裡傳的是誰按的；實際上不需要
    on_not_call_internal(pid, silent=False, advance_from_player=pending_prompts.get(pid, {}).get("player"))

@socketio.on("force_choice_answer")
def handle_force_choice_answer(data):
    # 偵探的強制選擇回覆
    player = (data or {}).get("player")
    choice = (data or {}).get("choice")
    if not player or player not in game_state["players"]:
        return
    if choice == "discard_one":
        role = (data or {}).get("discard_role")
        if role and role in game_state["players"][player]["roles"]:
            game_state["players"][player]["roles"].remove(role)
            game_state["discard_pile"].append(role)
            add_log(f"{player} 丟棄了一張手牌")
        else:
            add_log(f"{player} 未選擇有效手牌，視為 -1")
            game_state["players"][player]["score"] -= 1
    elif choice == "lose_one":
        game_state["players"][player]["score"] -= 1
        add_log(f"{player} 選擇扣 1 分")
    broadcast_state()
    check_end_conditions("force_choice_answer")

@socketio.on("end_turn_discard_draw")
def handle_end_turn_discard_draw(data):
    player = (data or {}).get("player")
    discard_role = (data or {}).get("discard_role")
    if player != game_state["current_turn"]:
        emit("error", {"message": "不是你的回合"}, room=request.sid); return
    if discard_role:
        roles = game_state["players"][player]["roles"]
        if discard_role in roles:
            roles.remove(discard_role)
            game_state["discard_pile"].append(discard_role)
    # 抽一張
    if game_state["draw_pile"]:
        card = game_state["draw_pile"].pop(0)
        game_state["players"][player]["roles"].append(card)
    broadcast_state()
    advance_turn(advance_from=player)

@socketio.on("admin_reset_game")
def handle_admin_reset_game(data):
    player = (data or {}).get("player")
    if not player or not game_state["players"].get(player, {}).get("is_admin"):
        emit("error", {"message": "你不是管理員"}, room=request.sid); return

    finish_all_timers()
    deck = ROLE_POOL_30.copy()
    random.shuffle(deck)
    names = list(game_state["players"].keys())
    random.shuffle(names)
    cards_per_player = CARDS_PER_PLAYER

    for i, name in enumerate(names):
        start = i * cards_per_player
        end = start + cards_per_player
        st = game_state["players"][name]
        st["roles"] = deck[start:end]
        st["score"] = 100
        st["guardian_active"] = False
        st["mark_target"] = None
        st["mark_used_turn"] = None
        st["stats"] = {"call_bluff_success": 0, "bluff_success": 0, "turns_taken": 0}

    game_state["player_order"] = names
    game_state["draw_pile"] = deck[len(names) * cards_per_player:]
    game_state["discard_pile"] = []
    game_state["current_turn"] = names[0] if names else None
    game_state["game_started"] = True
    game_state["turn_index"] = 0
    game_state["turn_marker"] = str(uuid.uuid4())
    game_state["start_ts"] = now_ts()
    game_state["deadline_ts"] = game_state["start_ts"] + GAME_TIME_LIMIT_SEC
    game_state["logs"] = []
    socketio.emit("game_started", {"message": "管理員已重啟遊戲"}, room=game_state["room_id"])
    broadcast_state()

# ====== 啟動 ======
if __name__ == "__main__":
    # 服務遊戲頁面
    app.template_folder = "templates"
    app.static_folder = "static"
    socketio.run(app, host="0.0.0.0", port=5000)