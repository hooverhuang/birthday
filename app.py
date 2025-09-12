from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

# 遊戲狀態
game_state = {
    "players": {},
    "logs": [],
    "current_turn": None,
    "room_id": "game_room_1",
    "game_started": False,
    "max_players": 4
}

# 連線的玩家
connected_players = {}

# 角色池 - 只保留狙擊手和贈禮者
ROLE_POOL = [
    " 狙擊手", " 贈禮者"
]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/state")
def state():
    return jsonify(game_state)

@socketio.on('connect')
def handle_connect():
    print(f'玩家連線: {request.sid}')
    emit('connected', {'message': '連線成功'})

@socketio.on('disconnect')
def handle_disconnect():
    print(f'玩家斷線: {request.sid}')
    if request.sid in connected_players:
        player_name = connected_players[request.sid]
        if player_name in game_state["players"]:
            del game_state["players"][player_name]
        del connected_players[request.sid]
        emit('player_left', {'player': player_name}, room=game_state['room_id'])
        emit('game_state', game_state, room=game_state['room_id'])

@socketio.on('join_game')
def handle_join_game(data):
    player_name = data.get('player_name', '').strip()
    
    if not player_name:
        emit('error', {'message': '請輸入玩家名稱'})
        return
    
    if len(game_state["players"]) >= game_state["max_players"]:
        emit('error', {'message': '遊戲已滿，無法加入'})
        return
    
    if player_name in game_state["players"]:
        emit('error', {'message': '玩家名稱已存在'})
        return
    
    # 加入玩家
    connected_players[request.sid] = player_name
    game_state["players"][player_name] = {
        "score": 100,
        "roles": [],
        "socket_id": request.sid
    }
    
    join_room(game_state['room_id'])
    emit('player_joined', {'player': player_name, 'total_players': len(game_state["players"])}, room=game_state['room_id'])
    emit('game_state', game_state, room=game_state['room_id'])

@socketio.on('start_game')
def handle_start_game():
    if len(game_state["players"]) < 2:
        emit('error', {'message': '至少需要2個玩家才能開始遊戲'})
        return
    
    # 分配角色 - 每個玩家隨機分配2張不同的卡牌
    player_names = list(game_state["players"].keys())
    random.shuffle(player_names)
    
    for i, player_name in enumerate(player_names):
        # 從2張卡牌中隨機選擇2張（可能重複，但每個玩家都有2張）
        player_roles = random.choices(ROLE_POOL, k=2)
        game_state["players"][player_name]["roles"] = player_roles
    
    game_state["current_turn"] = player_names[0]
    game_state["game_started"] = True
    
    emit('game_started', {'message': '遊戲開始！'}, room=game_state['room_id'])
    emit('game_state', game_state, room=game_state['room_id'])

@socketio.on('play_card')
def handle_play_card(data):
    player = data.get('player')
    role = data.get('role')
    target = data.get('target')
    
    if not game_state["game_started"]:
        emit('error', {'message': '遊戲尚未開始'})
        return
    
    if player not in game_state["players"]:
        emit('error', {'message': '未知玩家'})
        return

    # 檢查是否輪到該玩家
    if game_state['current_turn'] != player:
        emit('error', {'message': '還沒輪到你！'})
        return

    # 檢查玩家是否有這個角色
    if role not in game_state["players"][player]["roles"]:
        emit('error', {'message': '你沒有這個角色！'})
        return

    # 執行遊戲邏輯
    if role == " 狙擊手" and target:
        if target in game_state["players"]:
            # 狙擊手：目標玩家-3分，自己-1分
            game_state["players"][target]["score"] -= 3
            game_state["players"][player]["score"] -= 1
            game_state["logs"].append(f"{player} 使用狙擊手 → {target} -3 分，自己 -1 分")
    elif role == " 贈禮者" and target:
        if target in game_state["players"]:
            # 贈禮者：自己+1分，目標玩家-1分
            game_state["players"][player]["score"] += 1
            game_state["players"][target]["score"] -= 1
            game_state["logs"].append(f"{player} 使用贈禮者 → 自己 +1 分，{target} -1 分")

    # 移除已使用的角色
    game_state["players"][player]["roles"].remove(role)

    # 切換到下一個玩家
    players = list(game_state["players"].keys())
    current_index = players.index(player)
    next_index = (current_index + 1) % len(players)
    game_state['current_turn'] = players[next_index]

    # 廣播更新給所有玩家
    emit('game_state', game_state, room=game_state['room_id'])

@socketio.on('get_my_cards')
def handle_get_my_cards(data):
    player = data.get('player')
    if player in game_state["players"]:
        my_cards = game_state["players"][player]["roles"]
        emit('my_cards', {'cards': my_cards})

if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
