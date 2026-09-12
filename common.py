import base64
import hashlib
import hmac
import json
import os
import socket
import struct

from cryptography.fernet import Fernet

MAGIC = b"NETDESK\x01"
CONTROL_PORT = 5001
AUDIO_PORT = 5002
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
PBKDF2_ITERATIONS = 200000
CHALLENGE_LEN = 16
AUDIO_CHUNK_BYTES = 1600
AUDIO_SALT = b"laydesk-audio-session"
AUDIO_FORMAT = "int16"
AUDIO_RATE = 16000
AUDIO_CHANNELS = 1

SIZE_PACK = struct.Struct("!I")
AUTH_BLOCK = struct.Struct("!16s16s")
DIGEST_LEN = 32


def derive_password_key(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, 32
    )


def make_fernet(password, salt):
    raw = base64.urlsafe_b64encode(derive_password_key(password, salt))
    return Fernet(raw)


def auth_response(password, salt, challenge):
    key = derive_password_key(password, salt)
    return hmac.new(key, challenge, hashlib.sha256).digest()


def build_auth_challenge():
    salt = os.urandom(16)
    challenge = os.urandom(CHALLENGE_LEN)
    return salt, challenge


def recv_exact(conn, size):
    parts = []
    remaining = size

    while remaining > 0:
        chunk = conn.recv(min(4096, remaining))

        if not chunk:
            return None

        parts.append(chunk)
        remaining -= len(chunk)

    return b"".join(parts)


def server_handshake(conn, password):
    hello = recv_exact(conn, len(MAGIC))

    if hello != MAGIC:
        return None

    salt, challenge = build_auth_challenge()
    conn.sendall(AUTH_BLOCK.pack(salt, challenge))

    response = recv_exact(conn, DIGEST_LEN)

    if response is None:
        return None

    expected = auth_response(password, salt, challenge)

    if not hmac.compare_digest(response, expected):
        conn.sendall(b"\x00")
        return None

    conn.sendall(b"\x01")
    return make_fernet(password, salt)


def client_handshake(conn, password):
    conn.sendall(MAGIC)

    block = recv_exact(conn, AUTH_BLOCK.size)

    if block is None:
        return None, "No response from server"

    salt, challenge = AUTH_BLOCK.unpack(block)
    conn.sendall(auth_response(password, salt, challenge))

    status = recv_exact(conn, 1)

    if status != b"\x01":
        return None, "Authentication failed (wrong password?)"

    return make_fernet(password, salt), None


def send_message(conn, fernet, header, payload=b""):
    body = json.dumps(header).encode("utf-8") + b"\n" + payload
    encrypted = fernet.encrypt(body)
    conn.sendall(SIZE_PACK.pack(len(encrypted)) + encrypted)


def recv_message(conn, fernet):
    size_data = recv_exact(conn, SIZE_PACK.size)

    if size_data is None:
        return None, None

    size = SIZE_PACK.unpack(size_data)[0]

    if size <= 0 or size > 512 * 1024 * 1024:
        return None, None

    encrypted = recv_exact(conn, size)

    if encrypted is None:
        return None, None

    body = fernet.decrypt(encrypted)
    head, sep, payload = body.partition(b"\n")

    if not sep:
        return None, None

    return json.loads(head.decode("utf-8")), payload


SHIFT_SYMBOLS = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}

TK_SPECIAL_KEYS = {
    "return": "enter",
    "escape": "esc",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "prior": "pageup",
    "next": "pagedown",
    "insert": "insert",
    "kp_enter": "enter",
    "kp_add": "add",
    "kp_subtract": "subtract",
    "kp_multiply": "multiply",
    "kp_divide": "divide",
    "kp_decimal": "decimal",
    "shift_l": "shift",
    "shift_r": "shift",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "option_l": "alt",
    "option_r": "alt",
    "command_l": "command",
    "command_r": "command",
    "meta_l": "command",
    "meta_r": "command",
    "caps_lock": "capslock",
    "num_lock": "numlock",
    "scroll_lock": "scrolllock",
    "pause": "pause",
    "print": "printscreen",
    "printscreen": "printscreen",
}

for _index in range(1, 36):
    TK_SPECIAL_KEYS["f%d" % _index] = "f%d" % _index

DIRECT_KEYS = set(
    "abcdefghijklmnopqrstuvwxyz0123456789 .-/~;'[]`\\,=`"
)


def resolve_key_event(char, keysym):
    names = []

    if char and len(char) == 1 and char.isprintable():
        if "A" <= char <= "Z":
            names.append("shift")
            names.append(char.lower())
        elif char in SHIFT_SYMBOLS:
            names.append("shift")
            names.append(SHIFT_SYMBOLS[char])
        elif char.lower() in DIRECT_KEYS:
            names.append(char.lower())
    else:
        key = TK_SPECIAL_KEYS.get(keysym.lower())

        if key:
            names.append(key)

    return names