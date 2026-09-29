import json
import math
import os
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_FILE = os.path.join(BASE_DIR, "highscore.json")

# Mencegah race condition jika beberapa request datang bersamaan.
SAVE_LOCK = threading.Lock()
PLAYER_LOCK = threading.Lock()
ACTIVE_PLAYERS = {}
MATCHMAKING_LOCK = threading.Lock()
MATCH_QUEUE = {}
MATCHES = {}
PLAYER_MATCHES = {}
PLAYER_TIMEOUT = 12.0
PLAYER_NEARBY_RADIUS = 1800.0
MATCH_QUEUE_TIMEOUT = 45.0
MATCH_DURATION = 900.0
MAX_ACTIVE_PLAYERS = 256
PLAYER_SKINS = {"cyan", "verdant", "ember", "violet"}
PLAYER_SKILLS = {
    "cyan": {"name": "ARC BURST", "damage": 34, "range": 380, "cooldown": 7.5},
    "verdant": {"name": "VITAL PULSE", "damage": 22, "range": 300, "cooldown": 9.0},
    "ember": {"name": "NOVA STRIKE", "damage": 44, "range": 270, "cooldown": 10.0},
    "violet": {"name": "PHASE LANCE", "damage": 30, "range": 500, "cooldown": 8.5},
}
PLAYER_WEAPONS = {
    "pulse": {"name": "PULSE", "damage": 10, "range": 260, "cooldown": 0.65},
    "scatter": {"name": "SCATTER", "damage": 8, "range": 235, "cooldown": 0.9},
    "lance": {"name": "LANCE", "damage": 16, "range": 340, "cooldown": 0.95},
}


DEFAULT_SCORE = {
    "wave": 0,
    "kills": 0,
    "level": 0,
}

MAX_SCORE = {
    "wave": 1_000_000,
    "kills": 10_000_000,
    "level": 1_000_000,
}


def default_score():
    return DEFAULT_SCORE.copy()


def sanitize_score(data):
    """
    Membersihkan data score dari file maupun request.
    Semua nilai:
      - harus integer
      - minimal 0
      - tidak boleh melebihi batas
    """
    if not isinstance(data, dict):
        return default_score()

    result = {}

    for key in DEFAULT_SCORE:
        try:
            value = int(data.get(key, 0))
        except (ValueError, TypeError, OverflowError):
            value = 0

        result[key] = max(
            0,
            min(value, MAX_SCORE[key])
        )

    return result


def load_score():
    if not os.path.exists(SAVE_FILE):
        return default_score()

    try:
        with open(
            SAVE_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return sanitize_score(data)

    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return default_score()


def save_score(data):
    """
    Atomic write:
    tulis ke temporary file lalu replace file utama.
    """
    clean = sanitize_score(data)

    directory = os.path.dirname(SAVE_FILE) or "."

    fd = None
    temp_path = None

    try:
        fd, temp_path = tempfile.mkstemp(
            prefix="highscore_",
            suffix=".tmp",
            dir=directory,
            text=True,
        )

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as file:
            fd = None

            json.dump(
                clean,
                file,
                indent=2,
                ensure_ascii=False,
            )

            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temp_path,
            SAVE_FILE,
        )

        temp_path = None

    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


@app.get("/")
def index():
    return send_from_directory(
        BASE_DIR,
        "index.html",
    )


@app.get("/api/highscore")
def get_highscore():
    with SAVE_LOCK:
        score = load_score()

    response = jsonify(score)
    response.headers["Cache-Control"] = "no-store"

    return response


@app.post("/api/highscore")
def post_highscore():
    if not request.is_json:
        return jsonify({
            "error": "JSON required"
        }), 400

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        return jsonify({
            "error": "Invalid JSON"
        }), 400

    submitted = sanitize_score(data)

    with SAVE_LOCK:
        best = load_score()

        best["wave"] = max(
            best["wave"],
            submitted["wave"],
        )

        best["kills"] = max(
            best["kills"],
            submitted["kills"],
        )

        best["level"] = max(
            best["level"],
            submitted["level"],
        )

        try:
            save_score(best)
        except OSError:
            return jsonify({
                "error": "Could not save score"
            }), 500

    response = jsonify(best)
    response.headers["Cache-Control"] = "no-store"

    return response


@app.post("/api/players")
def update_players():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        player_id = str(uuid.UUID(data.get("id", "")))
    except (AttributeError, TypeError, ValueError):
        return jsonify({"error": "Invalid player ID"}), 400

    def valid_position(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None

        value = float(value)
        if not math.isfinite(value) or abs(value) > 10_000_000:
            return None

        return value

    x = valid_position(data.get("x"))
    y = valid_position(data.get("y"))
    if x is None or y is None:
        return jsonify({"error": "Invalid position"}), 400

    raw_name = data.get("name")
    name = " ".join(raw_name.split()) if isinstance(raw_name, str) else ""
    name = "".join(character for character in name if character.isprintable())[:16]
    if not name:
        name = f"PILOT-{player_id[:4].upper()}"

    raw_skin = data.get("skin")
    skin = raw_skin if isinstance(raw_skin, str) and raw_skin in PLAYER_SKINS else "cyan"
    raw_weapon = data.get("weapon")
    weapon = raw_weapon if isinstance(raw_weapon, str) and raw_weapon in PLAYER_WEAPONS else "pulse"

    score = sanitize_score(data)
    now = time.monotonic()

    with MATCHMAKING_LOCK:
        match_id = PLAYER_MATCHES.get(player_id)

    with PLAYER_LOCK:
        expired = [
            other_id
            for other_id, player in ACTIVE_PLAYERS.items()
            if now - player["updated"] > PLAYER_TIMEOUT
        ]
        for other_id in expired:
            ACTIVE_PLAYERS.pop(other_id, None)

        if player_id not in ACTIVE_PLAYERS and len(ACTIVE_PLAYERS) >= MAX_ACTIVE_PLAYERS:
            return jsonify({"error": "Player capacity reached"}), 503

        player_state = ACTIVE_PLAYERS.get(player_id, {})
        player_state.update({
            "x": x,
            "y": y,
            "name": name,
            "skin": skin,
            "weapon": weapon,
            "match_id": match_id,
            "wave": score["wave"],
            "kills": score["kills"],
            "level": score["level"],
            "updated": now,
        })
        player_state.setdefault("last_attack", 0.0)
        player_state.setdefault("last_skill", 0.0)
        player_state.setdefault("events", [])
        ACTIVE_PLAYERS[player_id] = player_state

        nearby = []
        for other_id, player in ACTIVE_PLAYERS.items():
            if other_id == player_id:
                continue
            if player.get("match_id") != match_id:
                continue

            distance = math.hypot(
                player["x"] - x,
                player["y"] - y,
            )
            if distance <= PLAYER_NEARBY_RADIUS:
                nearby.append({
                    "id": other_id,
                    "name": player["name"],
                    "skin": player["skin"],
                    "weapon": player["weapon"],
                    "x": player["x"],
                    "y": player["y"],
                    "wave": player["wave"],
                    "kills": player["kills"],
                    "level": player["level"],
                    "distance": distance,
                })

        nearby.sort(key=lambda player: player["distance"])
        leaderboard = [
            {
                "id": other_id,
                "name": player["name"],
                "skin": player["skin"],
                "wave": player["wave"],
                "kills": player["kills"],
                "level": player["level"],
            }
            for other_id, player in sorted(
                ACTIVE_PLAYERS.items(),
                key=lambda item: (
                    item[1]["wave"],
                    item[1]["kills"],
                    item[1]["level"],
                ),
                reverse=True,
            )[:10]
        ]

        response = jsonify({
            "players": nearby[:8],
            "leaderboard": leaderboard,
            "playersOnline": len(ACTIVE_PLAYERS),
            "pvpEvents": ACTIVE_PLAYERS[player_id]["events"],
            "matchId": match_id,
        })
        ACTIVE_PLAYERS[player_id]["events"] = []
        response.headers["Cache-Control"] = "no-store"
        return response


@app.post("/api/matchmaking")
def matchmaking():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        player_id = str(uuid.UUID(data.get("id", "")))
    except (AttributeError, TypeError, ValueError):
        return jsonify({"error": "Invalid player ID"}), 400

    action = data.get("action", "join")
    if action not in {"join", "cancel"}:
        return jsonify({"error": "Invalid matchmaking action"}), 400

    now = time.monotonic()
    name_value = data.get("name")
    name = " ".join(name_value.split()) if isinstance(name_value, str) else ""
    name = "".join(character for character in name if character.isprintable())[:16]
    if not name:
        name = f"PILOT-{player_id[:4].upper()}"
    skin_value = data.get("skin")
    skin = skin_value if isinstance(skin_value, str) and skin_value in PLAYER_SKINS else "cyan"
    wave = sanitize_score(data).get("wave", 0)

    with MATCHMAKING_LOCK:
        expired_queue = [
            queued_id
            for queued_id, ticket in MATCH_QUEUE.items()
            if now - ticket["joined_at"] > MATCH_QUEUE_TIMEOUT
        ]
        for queued_id in expired_queue:
            MATCH_QUEUE.pop(queued_id, None)

        expired_matches = [
            match_id
            for match_id, match in MATCHES.items()
            if now - match["created_at"] > MATCH_DURATION
        ]
        for expired_id in expired_matches:
            match = MATCHES.pop(expired_id)
            for participant in match["players"]:
                PLAYER_MATCHES.pop(participant, None)

        if action == "cancel":
            MATCH_QUEUE.pop(player_id, None)
            active_match_id = PLAYER_MATCHES.pop(player_id, None)
            if active_match_id:
                active_match = MATCHES.pop(active_match_id, None)
                if active_match:
                    for participant in active_match["players"]:
                        if participant != player_id:
                            PLAYER_MATCHES.pop(participant, None)
            return jsonify({"status": "cancelled"})

        active_match_id = PLAYER_MATCHES.get(player_id)
        active_match = MATCHES.get(active_match_id)
        if active_match:
            opponent_id = next(
                participant
                for participant in active_match["players"]
                if participant != player_id
            )
            player_match = active_match["players"][player_id]
            opponent = active_match["players"][opponent_id]
            return jsonify({
                "status": "matched",
                "matchId": active_match_id,
                "spawn": player_match["spawn"],
                "opponent": {
                    "id": opponent_id,
                    "name": opponent["name"],
                    "skin": opponent["skin"],
                },
            })

        ticket = {
            "id": player_id,
            "name": name,
            "skin": skin,
            "wave": wave,
            "joined_at": now,
        }
        MATCH_QUEUE[player_id] = ticket

        opponent_id = next((
            queued_id
            for queued_id, queued in MATCH_QUEUE.items()
            if queued_id != player_id and abs(queued["wave"] - wave) <= 3
        ), None)

        if opponent_id:
            opponent = MATCH_QUEUE.pop(opponent_id)
            MATCH_QUEUE.pop(player_id, None)
            match_id = str(uuid.uuid4())
            players = {
                player_id: {"name": name, "skin": skin, "spawn": {"x": 80, "y": 0}},
                opponent_id: {"name": opponent["name"], "skin": opponent["skin"], "spawn": {"x": -80, "y": 0}},
            }
            MATCHES[match_id] = {"created_at": now, "players": players}
            PLAYER_MATCHES[player_id] = match_id
            PLAYER_MATCHES[opponent_id] = match_id
            return jsonify({
                "status": "matched",
                "matchId": match_id,
                "spawn": players[player_id]["spawn"],
                "opponent": {
                    "id": opponent_id,
                    "name": opponent["name"],
                    "skin": opponent["skin"],
                },
            })

        return jsonify({
            "status": "queued",
            "position": sum(1 for queued in MATCH_QUEUE.values() if queued["wave"] <= wave),
            "queueSize": len(MATCH_QUEUE),
        })


@app.post("/api/battle")
def battle():
    if not request.is_json:
        return jsonify({"error": "JSON required"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        attacker_id = str(uuid.UUID(data.get("id", "")))
        target_id = str(uuid.UUID(data.get("target", "")))
    except (AttributeError, TypeError, ValueError):
        return jsonify({"error": "Invalid player ID"}), 400

    action = data.get("action")
    if action not in {"attack", "skill"}:
        return jsonify({"error": "Invalid action"}), 400
    if attacker_id == target_id:
        return jsonify({"error": "Cannot target yourself"}), 400

    now = time.monotonic()
    with PLAYER_LOCK:
        attacker = ACTIVE_PLAYERS.get(attacker_id)
        target = ACTIVE_PLAYERS.get(target_id)
        if (
            not attacker or not target or
            now - attacker["updated"] > PLAYER_TIMEOUT or
            now - target["updated"] > PLAYER_TIMEOUT
        ):
            return jsonify({"error": "Player unavailable"}), 404

        if attacker.get("match_id") != target.get("match_id"):
            return jsonify({"error": "Players are in different matches"}), 403

        skill = PLAYER_SKILLS[attacker["skin"]]
        if action == "skill":
            cooldown = skill["cooldown"]
            if now - attacker["last_skill"] < cooldown:
                return jsonify({"error": "Skill cooling down"}), 429
            damage = skill["damage"]
            attack_range = skill["range"]
        else:
            weapon = PLAYER_WEAPONS.get(
                attacker.get("weapon"),
                PLAYER_WEAPONS["pulse"],
            )
            cooldown = weapon["cooldown"]
            if now - attacker["last_attack"] < cooldown:
                return jsonify({"error": "Attack cooling down"}), 429
            damage = weapon["damage"]
            attack_range = weapon["range"]

        distance = math.hypot(
            target["x"] - attacker["x"],
            target["y"] - attacker["y"],
        )
        if distance > attack_range:
            return jsonify({"error": "Target out of range"}), 403

        attacker["last_skill" if action == "skill" else "last_attack"] = now

        heal = (
            round(damage * 0.75)
            if action == "skill" and attacker["skin"] == "verdant"
            else 0
        )
        target["events"].append({
            "damage": damage,
            "attacker": attacker["name"],
            "skin": attacker["skin"],
            "skill": action == "skill",
        })
        target["events"] = target["events"][-32:]

        return jsonify({
            "ok": True,
            "damage": damage,
            "heal": heal,
            "skillName": skill["name"] if action == "skill" else "ATTACK",
            "cooldown": cooldown,
            "target": target_id,
        })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok"
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
    )
