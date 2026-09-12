# NetDesk

A LAN-based remote desktop control application built with Python.
Stream your screen, control it with mouse/keyboard, sync the clipboard,
relay audio, and transfer files — all through an encrypted, password-authenticated connection.

## Features

- **Live screen streaming** — only changed screen regions are transmitted (delta video encoding, JPEG), drastically cutting bandwidth
- **Remote control** — full mouse (move / click / scroll) and keyboard (shift-aware key up/down) control
- **Multi-monitor** — controller can switch which monitor the host shares, live
- **Security** — password handshake with challenge-response (HMAC-SHA256) and AES-128 encrypted messages (Fernet/PBKDF2)
- **Clipboard sync** — bidirectional, echo-guarded
- **File transfer** — send files from either side (stored in `downloads/`)
- **Voice relay** — optional encrypted low-latency microphone audio over UDP
- **Clean GUI** — tkinter consoles on both ends, no CLI prompts

## Architecture

```
Protocol:  TCP control channel (framed, encrypted) + UDP audio relay
           Host = sender.py (the machine being viewed/controlled)
           Controller = reciever.py (the machine doing the controlling)
```

| Module      | Role                                              |
|-------------|---------------------------------------------------|
| `sender.py`   | Host console: listener, capture, input executor, file sender |
| `reciever.py` | Controller GUI: live canvas, input capture, monitor picker |
| `common.py`   | Shared protocol: auth, encryption, framing, key maps |
| `audio.py`    | Optional UDP voice relay (needs `pyaudio`)         |

## Installation

```bash
pip install -r requirements.txt
```

`pyaudio` is optional — everything except audio works without it.

## Running

On the host machine (the one being shared):

```bash
python3 sender.py
```

1. Set a password and press **Start Listener**.
2. Grant macOS `Screen Recording` permission (and `Accessibility` for mouse control).

On the controller machine:

```bash
python3 reciever.py
```

1. Enter the host's IP, port (default `5001`) and the password.
2. Press **Connect**, pick a monitor, and you're in control.

You can also test both locally on one machine with `127.0.0.1`.

## Ports

| Port | Use                  |
|------|----------------------|
| 5001 | TCP control channel (screen, input, files, clipboard) |
| 5002 | UDP audio relay      |

## Notes

- `pyautogui.FAILSAFE` is disabled so remote sessions are not interrupted.
- File pushes are channelled over the control connection; a large file will temporarily compete with video updates.