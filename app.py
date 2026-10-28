from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, emit
import random
import os
import eventlet

app = Flask(__name__)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

rooms = {}
clients = {}

PHASE_DESCRIPTIONS = {
    1: "2 Sets of 3",
    2: "1 Set of 3 + 1 Run of 4",
    3: "1 Set of 4 + 1 Run of 4",
    4: "1 Run of 7",
    5: "1 Run of 8",
    6: "1 Run of 9",
    7: "2 Sets of 4",
    8: "7 Cards of 1 Color",
    9: "1 Set of 5 + 1 Set of 2",
    10: "1 Set of 5 + 1 Set of 3"
}

PHASE_SLOTS = {
    1: [{'label': 'Set of 3', 'size': 3}, {'label': 'Set of 3', 'size': 3}],
    2: [{'label': 'Set of 3', 'size': 3}, {'label': 'Run of 4', 'size': 4}],
    3: [{'label': 'Set of 4', 'size': 4}, {'label': 'Run of 4', 'size': 4}],
    4: [{'label': 'Run of 7', 'size': 7}],
    5: [{'label': 'Run of 8', 'size': 8}],
    6: [{'label': 'Run of 9', 'size': 9}],
    7: [{'label': 'Set of 4', 'size': 4}, {'label': 'Set of 4', 'size': 4}],
    8: [{'label': '7 of 1 Color', 'size': 7}],
    9: [{'label': 'Set of 5', 'size': 5}, {'label': 'Set of 2', 'size': 2}],
    10: [{'label': 'Set of 5', 'size': 5}, {'label': 'Set of 3', 'size': 3}]
}

def card_penalty(card):
    """Standard Phase 10 points for cards remaining in a player's hand."""
    if card.get('type') == 'wild':
        return 25
    if card.get('type') == 'skip':
        return 15
    value = int(card.get('value', 0))
    return 5 if value <= 9 else 10


def finish_round(room, winner):
    """Score remaining hands, advance only completed players, and deal a new round."""
    game = rooms[room]
    if game.get('finished') or not game['completed_phases'].get(winner):
        return

    round_points = {
        player: sum(card_penalty(card) for card in game['hands'][player])
        for player in game['players']
    }
    for player, points in round_points.items():
        game['scores'][player] += points

    completed = dict(game['completed_phases'])
    for player in game['players']:
        if completed.get(player):
            game['phase_progress'][player] += 1

    sequence = game['phase_sequence']
    finished_players = [
        player for player in game['players']
        if game['phase_progress'][player] >= len(sequence)
    ]
    if finished_players:
        # Phase completion takes precedence; lowest accumulated score breaks ties.
        champion = min(finished_players, key=lambda player: game['scores'][player])
        game['finished'] = True
        socketio.emit('game_won', {
            'winner': champion,
            'scores': dict(game['scores']),
            'round_points': round_points,
            'phase_sequence': sequence,
        }, room=room)
        return

    game['last_round_points'] = round_points
    game['last_round_winner'] = winner
    game['round_number'] += 1
    game['deck'] = create_deck()
    game['discard_pile'] = []
    for player in game['players']:
        game['phases'][player] = sequence[game['phase_progress'][player]]
        game['completed_phases'][player] = False
        game['player_boxes'][player] = []
        game['hands'][player] = sort_hand([game['deck'].pop() for _ in range(10)])
    game['discard_pile'] = [game['deck'].pop()]
    game['has_drawn'] = False
    game['turn_index'] = game['players'].index(winner)
    broadcast_game_state(room)
    if game['players'][game['turn_index']].startswith('Bot'):
        socketio.start_background_task(run_bot_turn, room)


def create_deck():
    colors = ['Red', 'Blue', 'Green', 'Yellow']
    deck = []
    for color in colors:
        for num in range(1, 13):
            deck.append({'type': 'number', 'color': color, 'value': num})
            deck.append({'type': 'number', 'color': color, 'value': num})
    for _ in range(8):
        deck.append({'type': 'wild', 'color': 'Any', 'value': 'Wild'})
    for _ in range(4):
        deck.append({'type': 'skip', 'color': 'Any', 'value': 'Skip'})
    random.shuffle(deck)
    return deck

def refill_deck_if_needed(room_data):
    if not room_data['deck']:
        if len(room_data['discard_pile']) > 1:
            top_card = room_data['discard_pile'].pop()
            cards_to_shuffle = room_data['discard_pile']
            room_data['discard_pile'] = [top_card]
            random.shuffle(cards_to_shuffle)
            room_data['deck'] = cards_to_shuffle
        else:
            room_data['deck'] = create_deck()

def sort_hand(hand):
    color_order = {'Red': 1, 'Blue': 2, 'Green': 3, 'Yellow': 4, 'Any': 5}
    def card_key(card):
        color = card.get('color', 'Any')
        val = card.get('value', 0)
        c_rank = color_order.get(color, 6)
        v_rank = val if isinstance(val, int) else 100
        return (c_rank, v_rank)
    return sorted(hand, key=card_key)

def is_wild(card):
    return card.get('type') == 'wild'

def validate_set(cards, target_size):
    non_wilds = [c['value'] for c in cards if not is_wild(c)]
    if not non_wilds:
        return True
    first_val = non_wilds[0]
    for v in non_wilds:
        if v != first_val:
            return False
    return True

def run_start(cards):
    """Return the start of an ordered run, or None if it cannot fit 1..12."""
    if not cards or len(cards) > 12:
        return None
    starts = set(range(1, 14 - len(cards)))
    for index, card in enumerate(cards):
        if not is_wild(card):
            try:
                value = int(card['value'])
            except (ValueError, TypeError, KeyError):
                return None
            starts &= {value - index}
    return min(starts) if starts else None


def validate_run(cards, target_size):
    return len(cards) >= target_size and run_start(cards) is not None


def validate_color(cards, target_size):
    colors = [c['color'] for c in cards if not is_wild(c) and c['color'] != 'Any']
    if not colors:
        return True
    first_color = colors[0]
    for col in colors:
        if col != first_color:
            return False
    return True

def validate_phase_submission(phase_num, groups):
    slots = PHASE_SLOTS.get(phase_num, [])
    if len(groups) != len(slots):
        return False, f"Phase {phase_num} requires {len(slots)} groups."
        
    for i, slot in enumerate(slots):
        cards = groups[i]
        label = slot['label'].lower()
        size = slot['size']
        
        if len(cards) < size:
            return False, f"Box {i+1} ({slot['label']}) needs at least {size} cards."
            
        if 'set' in label:
            if not validate_set(cards, size):
                return False, f"Box {i+1} ({slot['label']}) is not a valid Set."
        elif 'run' in label:
            if not validate_run(cards, size):
                return False, f"Box {i+1} ({slot['label']}) is not a valid Run."
        elif 'color' in label:
            if not validate_color(cards, size):
                return False, f"Box {i+1} ({slot['label']}) is not a valid Color group."
    return True, "Valid Phase!"

def validate_hit(slot_label, existing_cards, card_to_add, side='end'):
    label = slot_label.lower()
    if 'run' in label:
        if side not in ('start', 'end'):
            return False
        candidate = ([card_to_add] + existing_cards if side == 'start'
                     else existing_cards + [card_to_add])
        return run_start(candidate) is not None

    if is_wild(card_to_add):
        return True
    non_wilds = [c for c in existing_cards if not is_wild(c)]
    if 'set' in label:
        return not non_wilds or card_to_add.get('value') == non_wilds[0].get('value')
    if 'color' in label:
        return not non_wilds or card_to_add.get('color') == non_wilds[0].get('color')
    return False

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join(data):
    room = data['room']
    username = data['username']
    join_room(room)
    
    clients[request.sid] = {'username': username, 'room': room}
    
    if room not in rooms:
        rooms[room] = {
            'players': [], 
            'hands': {}, 
            'phases': {}, 
            'completed_phases': {},
            'player_boxes': {},
            'max_phases': 10,
            'phase_sequence': list(range(1, 11)),
            'phase_progress': {},
            'scores': {},
            'round_number': 1,
            'last_round_points': {},
            'last_round_winner': None,
            'finished': False,
            'deck': [], 
            'discard_pile': [], 
            'turn_index': 0, 
            'has_drawn': False,
            'started': False
        }
    
    if username not in rooms[room]['players'] and not rooms[room]['started']:
        rooms[room]['players'].append(username)
        rooms[room]['phases'][username] = 1
        rooms[room]['completed_phases'][username] = False
        rooms[room]['player_boxes'][username] = []
        
    emit('update_lobby', {'players': rooms[room]['players'], 'started': rooms[room]['started']}, room=room)

@socketio.on('start_game')
def handle_start_game(data):
    room = data['room']
    if room in rooms and not rooms[room]['started']:
        players = rooms[room]['players']
        if not players:
            return
        
        requested_max = max(1, min(10, int(data.get('max_phases', 10))))

        if len(players) < 2:
            if "Bot 1" not in players:
                players.append("Bot 1")

        # All players share one random, non-repeating phase sequence.
        # A full 10-phase game always plays phases 1 through 10 in order.
        sequence = (list(range(1, 11)) if requested_max == 10
                    else random.sample(range(1, 11), requested_max))
        game = rooms[room]
        game['phase_sequence'] = sequence
        game['max_phases'] = requested_max
        game['round_number'] = 1
        game['last_round_points'] = {}
        game['last_round_winner'] = None
        game['finished'] = False

        for player in players:
            game['phases'][player] = sequence[0]
            game['phase_progress'][player] = 0
            game['scores'][player] = 0
            game['completed_phases'][player] = False
            game['player_boxes'][player] = []

        deck = create_deck()
        hands = {}
        for player in players:
            raw_hand = [deck.pop() for _ in range(10)]
            hands[player] = sort_hand(raw_hand)
            
        discard_pile = [deck.pop()]
            
        rooms[room]['deck'] = deck
        rooms[room]['hands'] = hands
        rooms[room]['discard_pile'] = discard_pile
        rooms[room]['turn_index'] = 0
        rooms[room]['has_drawn'] = False
        rooms[room]['started'] = True
        
        broadcast_game_state(room)

@socketio.on('reorder_hand')
def handle_reorder_hand(data):
    room = data['room']
    new_hand = data['new_hand']
    sid = request.sid
    if sid not in clients:
        return
    username = clients[sid]['username']
    room_data = rooms.get(room)
    if room_data and username in room_data['hands']:
        room_data['hands'][username] = new_hand

@socketio.on('draw_card')
def handle_draw(data):
    room = data['room']
    source = data['source'] 
    sid = request.sid
    
    if sid not in clients:
        return
    
    username = clients[sid]['username']
    room_data = rooms.get(room)
    
    if not room_data or not room_data['started'] or room_data['has_drawn']:
        return
        
    current_player = room_data['players'][room_data['turn_index']]
    if current_player != username:
        return 
        
    deck = room_data['deck']
    discard_pile = room_data['discard_pile']
    hand = room_data['hands'][username]
    
    if source == 'deck':
        refill_deck_if_needed(room_data)
        if room_data['deck']:
            card = room_data['deck'].pop()
            hand.append(card)
            room_data['has_drawn'] = True
    elif source == 'discard' and discard_pile:
        if discard_pile[-1].get('type') == 'skip':
            emit('phase_rejected', {'message': 'Skip cards cannot be drawn from the discard pile!'})
            return
        card = discard_pile.pop()
        hand.append(card)
        room_data['has_drawn'] = True
        
    broadcast_game_state(room)

@socketio.on('discard_card')
def handle_discard(data):
    room = data['room']
    card_index = data['card_index']
    sid = request.sid
    
    if sid not in clients:
        return
    
    username = clients[sid]['username']
    room_data = rooms.get(room)
    
    if not room_data or not room_data['started'] or not room_data['has_drawn']:
        return
        
    current_player = room_data['players'][room_data['turn_index']]
    if current_player != username:
        return
        
    hand = room_data['hands'][username]
    if 0 <= card_index < len(hand):
        if len(hand) == 1 and not room_data['completed_phases'].get(username):
            emit('phase_rejected', {'message': 'Complete your objective before playing your last card.'}, room=sid)
            return
        discarded = hand.pop(card_index)
        room_data['discard_pile'].append(discarded)
        room_data['has_drawn'] = False
        
        if len(hand) == 0:
            finish_round(room, username)
            return

        next_turn_idx = (room_data['turn_index'] + 1) % len(room_data['players'])
        if discarded.get('type') == 'skip':
            next_turn_idx = (next_turn_idx + 1) % len(room_data['players'])

        room_data['turn_index'] = next_turn_idx
        broadcast_game_state(room)
        
        next_player = room_data['players'][room_data['turn_index']]
        if next_player.startswith("Bot"):
            socketio.start_background_task(run_bot_turn, room)

@socketio.on('submit_phase')
def handle_submit_phase(data):
    room = data['room']
    groups = data['groups'] 
    sid = request.sid
    
    if sid not in clients:
        return
        
    username = clients[sid]['username']
    room_data = rooms.get(room)
    
    if not room_data or not room_data['started'] or room_data['completed_phases'].get(username):
        return
        
    current_player = room_data['players'][room_data['turn_index']]
    if current_player != username:
        return
        
    hand = room_data['hands'][username]
    current_phase = room_data['phases'][username]
    
    resolved_groups = []
    all_indices = []
    for group_indices in groups:
        resolved_group = []
        for idx in group_indices:
            if 0 <= idx < len(hand):
                resolved_group.append(hand[idx])
                all_indices.append(idx)
            else:
                emit('phase_rejected', {'message': 'Invalid card selection!'}, room=sid)
                return
        resolved_groups.append(resolved_group)
        
    if len(all_indices) != len(set(all_indices)):
        emit('phase_rejected', {'message': 'Cannot use the same card in multiple boxes!'}, room=sid)
        return
        
    is_valid, msg = validate_phase_submission(current_phase, resolved_groups)
    if not is_valid:
        emit('phase_rejected', {'message': msg}, room=sid)
        return
        
    for i in sorted(all_indices, reverse=True):
        hand.pop(i)
        
    room_data['completed_phases'][username] = True
    room_data['player_boxes'][username] = resolved_groups
    broadcast_game_state(room)

@socketio.on('hit_phase_box')
def handle_hit_box(data):
    room = data['room']
    target_player = data.get('target_player')
    slot_index = data['slot_index']
    card_index = data['card_index']
    side = data.get('side', 'end')
    sid = request.sid
    
    if sid not in clients:
        return
        
    username = clients[sid]['username']
    room_data = rooms.get(room)
    
    if not room_data or not room_data['started'] or room_data.get('finished'):
        return
        
    current_player = room_data['players'][room_data['turn_index']]
    if current_player != username:
        return
        
    if not room_data['completed_phases'].get(username):
        emit('phase_rejected', {'message': 'You must complete your own phase before you can hit boxes!'}, room=sid)
        return
        
    if not room_data['completed_phases'].get(target_player):
        emit('phase_rejected', {'message': f"{target_player} has not completed their phase yet!"}, room=sid)
        return
        
    target_phase = room_data['phases'][target_player]
    slots = PHASE_SLOTS.get(target_phase, [])
    
    if not (0 <= slot_index < len(slots)):
        return
        
    hand = room_data['hands'][username]
    if not (0 <= card_index < len(hand)):
        return
        
    target_slot = slots[slot_index]
    existing_box_cards = room_data['player_boxes'][target_player][slot_index]
    card_to_hit = hand[card_index]
    
    if not validate_hit(target_slot['label'], existing_box_cards, card_to_hit, side):
        emit('phase_rejected', {'message': f"This card does not legally fit into {target_player}'s Box {slot_index + 1} ({target_slot['label']})!"}, room=sid)
        return
        
    card = hand.pop(card_index)
    if 'run' in target_slot['label'].lower() and side == 'start':
        room_data['player_boxes'][target_player][slot_index].insert(0, card)
    else:
        room_data['player_boxes'][target_player][slot_index].append(card)
    
    if len(hand) == 0:
        finish_round(room, username)
        return

    broadcast_game_state(room)

def run_bot_turn(room):
    eventlet.sleep(1.5)
    room_data = rooms.get(room)
    if not room_data or not room_data['started']:
        return
        
    current_player = room_data['players'][room_data['turn_index']]
    if not current_player.startswith("Bot"):
        return
        
    if not room_data['completed_phases'].get(current_player) and len(room_data['hands'][current_player]) >= 6:
        slots = PHASE_SLOTS.get(room_data['phases'][current_player], [])
        bot_hand = room_data['hands'][current_player]
        simulated_groups = [bot_hand[:slots[0]['size']]]
        if len(slots) > 1:
            simulated_groups.append(bot_hand[slots[0]['size']:slots[0]['size'] + slots[1]['size']])
        
        is_val, _ = validate_phase_submission(room_data['phases'][current_player], simulated_groups)
        if is_val:
            flattened = [c for g in simulated_groups for c in g]
            for c in flattened:
                if c in bot_hand:
                    bot_hand.remove(c)
            room_data['completed_phases'][current_player] = True
            room_data['player_boxes'][current_player] = simulated_groups

    refill_deck_if_needed(room_data)
    if room_data['deck']:
        card = room_data['deck'].pop()
        room_data['hands'][current_player].append(card)
        
    broadcast_game_state(room)
    eventlet.sleep(1.5)
    
    if room_data.get('finished') or room_data['players'][room_data['turn_index']] != current_player:
        return
    bot_hand = room_data['hands'][current_player]
    if bot_hand:
        # A bot cannot go out before laying down its current objective.
        discarded = bot_hand.pop(random.randint(0, len(bot_hand) - 1))
        room_data['discard_pile'].append(discarded)
        
    if not bot_hand and room_data['completed_phases'].get(current_player):
        finish_round(room, current_player)
        return

    next_turn_idx = (room_data['turn_index'] + 1) % len(room_data['players'])
    if discarded.get('type') == 'skip':
        next_turn_idx = (next_turn_idx + 1) % len(room_data['players'])

    room_data['turn_index'] = next_turn_idx
    broadcast_game_state(room)
    
    next_player = room_data['players'][room_data['turn_index']]
    if next_player.startswith("Bot"):
        socketio.start_background_task(run_bot_turn, room)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in clients:
        username = clients[sid]['username']
        room = clients[sid]['room']
        
        if room in rooms and username in rooms[room]['players']:
            rooms[room]['players'].remove(username)
            if username in rooms[room]['phases']:
                del rooms[room]['phases'][username]
            if not rooms[room]['players']:
                del rooms[room]
            else:
                emit('update_lobby', {'players': rooms[room]['players'], 'started': rooms[room]['started']}, room=room)
                
        del clients[sid]

def broadcast_game_state(room):
    room_data = rooms[room]
    players = room_data['players']
    current_turn_player = players[room_data['turn_index']]
    top_discard = room_data['discard_pile'][-1] if room_data['discard_pile'] else None
    
    for sid, info in clients.items():
        if info['room'] == room:
            p_name = info['username']
            opponents = {
                p: {
                    'cards_count': len(room_data['hands'][p]), 
                    'phase': room_data['phases'][p],
                    'phase_step': room_data['phase_progress'].get(p, 0) + 1,
                    'score': room_data['scores'].get(p, 0),
                    'completed': room_data['completed_phases'].get(p, False),
                    'boxes': room_data['player_boxes'].get(p, []),
                    'slots': PHASE_SLOTS.get(room_data['phases'][p], [])
                } 
                for p in players if p != p_name
            }
            
            socketio.emit('game_update', {
                'hand': room_data['hands'].get(p_name, []),
                'opponents': opponents,
                'deck_count': len(room_data['deck']),
                'top_discard': top_discard,
                'current_turn': current_turn_player,
                'has_drawn': room_data['has_drawn'],
                'current_phase': room_data['phases'].get(p_name, 1),
                'max_phases': room_data['max_phases'],
                'phase_sequence': room_data['phase_sequence'],
                'phase_step': room_data['phase_progress'].get(p_name, 0) + 1,
                'scores': dict(room_data['scores']),
                'my_score': room_data['scores'].get(p_name, 0),
                'round_number': room_data['round_number'],
                'last_round_points': room_data['last_round_points'],
                'last_round_winner': room_data['last_round_winner'],
                'phase_desc': PHASE_DESCRIPTIONS.get(room_data['phases'].get(p_name, 1), ""),
                'phase_slots': PHASE_SLOTS.get(room_data['phases'].get(p_name, 1), []),
                'phase_completed': room_data['completed_phases'].get(p_name, False),
                'my_boxes': room_data['player_boxes'].get(p_name, []),
                'is_my_turn': (current_turn_player == p_name)
            }, room=sid)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Phase 10 server starting at http://127.0.0.1:{port}", flush=True)
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
