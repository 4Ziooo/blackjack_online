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
    # threading modunda sorun çıkmaması için:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
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
