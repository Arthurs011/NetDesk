import os
import threading
import time

import cv2
import mss
import numpy as np
import pyautogui
import tkinter as tk
from tkinter import filedialog, scrolledtext

from audio import HAVE_AUDIO, AudioLink
from common import (
    AUDIO_PORT,
    CONTROL_PORT,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    send_message,
    recv_message,
    server_handshake,
)

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.01


def changed_region(prev_gray, gray, min_ratio=0.0008, margin=6):
    diff = cv2.absdiff(prev_gray, gray)

    if float(np.count_nonzero(diff > 24)) / diff.size < min_ratio:
        return None

    ys, xs = np.where(diff > 24)
    x0 = max(0, int(xs.min()) - margin)
    y0 = max(0, int(ys.min()) - margin)
    x1 = min(gray.shape[1], int(xs.max()) + margin + 1)
    y1 = min(gray.shape[0], int(ys.max()) + margin + 1)

    return [x0, y0, x1 - x0, y1 - y0]


class HostSession:
    def __init__(self, conn, fernet, addr, password, app):
        self.conn = conn
        self.fernet = fernet
        self.addr = addr
        self.app = app
        self.password = password
        self.send_lock = threading.Lock()
        self.running = True
        self.audio_link = None
        self.screen = None
        self.monitor_index = 1
        self.closed = False
        self.download_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(self.download_dir, exist_ok=True)
        self.file_handle = None
        self.file_received = 0
        self.app.log("Session authenticated: %s" % (addr[0],))
        self.app.set_session(self)

    def send(self, header, payload=b""):
        with self.send_lock:
            send_message(self.conn, self.fernet, header, payload)

    def close(self):
        if self.closed:
            return

        self.closed = True
        self.running = False
        self.stop_audio()
        self.conn.close()
        self.app.set_session(None)
        self.app.log("Session closed.")

    def start(self):
        video_thread = None

        try:
            self.screen = mss.mss()

            monitors = []

            for idx, mon in enumerate(self.screen.monitors):
                if idx == 0:
                    continue

                monitors.append({
                    "index": idx,
                    "width": mon["width"],
                    "height": mon["height"],
                    "left": mon["left"],
                    "top": mon["top"],
                })

            self.send({"type": "monitors", "list": monitors})

            video_thread = threading.Thread(
                target=self.video_loop, daemon=True
            )
            video_thread.start()

            self.input_loop()

        except (OSError, ConnectionError):
            pass

        finally:
            self.close()

            if video_thread is not None and video_thread.is_alive():
                video_thread.join(timeout=2.0)

    def input_loop(self):
        while self.running:
            header, payload = recv_message(self.conn, self.fernet)

            if header is None:
                break

            msg_type = header.get("type")

            if msg_type == "mouse":
                self.handle_mouse(header)

            elif msg_type == "key":
                self.handle_key(header)

            elif msg_type == "monitor":
                new_index = header.get("index")

                if isinstance(new_index, int) and new_index >= 1:
                    self.monitor_index = new_index

            elif msg_type == "clipboard":
                self.app.apply_clipboard(header.get("text", ""))

            elif msg_type == "file_meta":
                self._start_receive_file(header)

            elif msg_type == "file_chunk":
                if self.file_handle is not None:
                    self.file_handle.write(payload)
                    self.file_received += len(payload)

            elif msg_type == "file_end":
                self._finish_receive_file(header.get("name", ""))

    def _start_receive_file(self, header):
        name = os.path.basename(header.get("name", "file.bin"))

        self.file_handle = open(
            os.path.join(self.download_dir, name), "wb"
        )

        self.file_received = 0
        self.app.log("Receiving %s..." % name)

    def _finish_receive_file(self, name):
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None

        self.app.log(
            "Received %s (%d bytes)" % (name, self.file_received)
        )

    def handle_mouse(self, command):
        if not self.app.allow_control.get():
            return

        monitor = self.screen.monitors[self.monitor_index]
        action = command.get("action")
        m_width = monitor["width"]
        m_height = monitor["height"]

        if action == "move":
            x = int(command["x"] * m_width / FRAME_WIDTH)
            y = int(command["y"] * m_height / FRAME_HEIGHT)
            pyautogui.moveTo(monitor["left"] + x, monitor["top"] + y)

        elif action == "scroll":
            pyautogui.scroll(int(command.get("amount", 0)))

        elif action in (
            "left_down", "left_up", "right_down",
            "right_up", "middle_down", "middle_up",
        ):
            button, state = action.split("_")

            if state == "down":
                pyautogui.mouseDown(button=button)
            else:
                pyautogui.mouseUp(button=button)

    def handle_key(self, command):
        names = command.get("names", [])
        action = command.get("action")

        if action == "down":
            for name in names:
                pyautogui.keyDown(name)

        elif action == "up":
            for name in reversed(names):
                pyautogui.keyUp(name)

    def video_loop(self):
        prev_gray = None
        prev_time = time.monotonic()
        fps = 0.0

        try:
            while self.running:
                monitor = self.screen.monitors[self.monitor_index]
                shot = self.screen.grab(monitor)

                frame = np.array(shot)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                if prev_gray is None:
                    bbox = [0, 0, FRAME_WIDTH, FRAME_HEIGHT]
                else:
                    bbox = changed_region(prev_gray, gray)

                prev_gray = gray
                now = time.monotonic()
                delta = now - prev_time

                if delta > 0:
                    fps = fps * 0.9 + (1.0 / delta) * 0.1

                prev_time = now

                if bbox is None:
                    self.send(
                        {"type": "video", "bbox": None, "fps": round(fps, 1)}
                    )
                else:
                    x0, y0, w, h = bbox
                    crop = frame[y0:y0 + h, x0:x0 + w]

                    success, encoded = cv2.imencode(
                        ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 60]
                    )

                    if success:
                        self.send(
                            {
                                "type": "video",
                                "bbox": bbox,
                                "fps": round(fps, 1),
                            },
                            encoded.tobytes(),
                        )

                time.sleep(0.02)

        except (OSError, ConnectionError):
            self.close()

    def push_file(self, path):
        filename = os.path.basename(path)
        file_size = os.path.getsize(path)

        try:
            self.send({
                "type": "file_meta",
                "name": filename,
                "size": file_size,
            })

            sent = 0

            with open(path, "rb") as file:
                while sent < file_size:
                    chunk = file.read(65536)

                    if not chunk:
                        break

                    self.send({"type": "file_chunk"}, chunk)
                    sent += len(chunk)

            self.send({"type": "file_end", "name": filename})
            self.app.log("File sent: %s" % filename)

        except (OSError, ConnectionError):
            self.app.log("File transfer failed.")

    def enable_audio(self):
        if not HAVE_AUDIO:
            return False

        self.audio_link = AudioLink(self.password, self.addr[0], AUDIO_PORT)
        return self.audio_link.start()

    def stop_audio(self):
        if self.audio_link is not None:
            self.audio_link.stop()
            self.audio_link = None


class HostApp:
    def __init__(self, root):
        self.root = root
        root.title("NetDesk - Host Console")
        root.geometry("560x520")

        self.running = False
        self.server_socket = None
        self.session = None
        self.last_clipboard = None

        self.build_ui()

    def build_ui(self):
        frame = tk.Frame(self.root, padx=16, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="NetDesk Host", font=("", 16, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Start listening so a controller can connect.",
            fg="#666",
        ).pack(anchor="w", pady=(0, 10))

        row_status = tk.Frame(frame)
        row_status.pack(fill="x", pady=2)

        tk.Label(row_status, text="Status:").pack(side="left")

        self.status_label = tk.Label(row_status, text="Not listening", fg="#c33")
        self.status_label.pack(side="left", padx=6)

        options = tk.Frame(frame)
        options.pack(fill="x", pady=6)

        tk.Label(options, text="Port:").grid(row=0, column=0, sticky="e")
        self.port_var = tk.StringVar(value=str(CONTROL_PORT))
        tk.Entry(options, textvariable=self.port_var, width=8).grid(
            row=0, column=1, sticky="w", padx=6
        )

        tk.Label(options, text="Password:").grid(row=1, column=0, sticky="e", pady=(4, 0))
        self.password_var = tk.StringVar()
        tk.Entry(options, textvariable=self.password_var, show="*").grid(
            row=1, column=1, sticky="w", padx=6, pady=(4, 0)
        )

        tk.Label(options, text="Share monitor:").grid(row=2, column=0, sticky="e", pady=(4, 0))
        self.monitor_var = tk.IntVar(value=0)
        self.monitor_list = tk.OptionMenu(options, self.monitor_var, 0)
        self.monitor_list.grid(row=2, column=1, sticky="w", padx=6, pady=(4, 0))

        self.allow_control = tk.BooleanVar(value=True)
        tk.Checkbutton(
            frame,
            text="Allow remote mouse & keyboard control",
            variable=self.allow_control,
        ).pack(anchor="w", pady=(10, 4))

        self.audio_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame, text="Enable audio (microphone relay)", variable=self.audio_var
        ).pack(anchor="w")

        buttons = tk.Frame(frame)
        buttons.pack(fill="x", pady=12)

        self.start_btn = tk.Button(
            buttons, text="Start Listener", command=self.start_listener, width=14
        )
        self.start_btn.pack(side="left")

        self.stop_btn = tk.Button(
            buttons, text="Stop", command=self.stop_listener, width=10, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=6)

        self.send_btn = tk.Button(
            buttons, text="Send File...", command=self.send_file, width=12, state="disabled"
        )
        self.send_btn.pack(side="left", padx=6)

        tk.Label(frame, text="Log", anchor="w").pack(anchor="w")

        self.log_text = scrolledtext.ScrolledText(frame, height=12, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

        self.root.after(400, self._poll_clipboard)

    def log(self, message):
        def _write():
            self.log_text.configure(state="normal")
            self.log_text.insert(
                "end",
                "%s> %s\n" % (time.strftime("%H:%M:%S"), message),
            )
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _write)

    def set_session(self, session):
        def _update():
            self.session = session
            self.send_btn.configure(
                state="normal" if session is not None else "disabled"
            )

            if session is not None:
                self.status_label.configure(
                    text="Connected: %s" % (session.addr[0],), fg="#2b7"
                )
                self._sync_audio()
            else:
                self.status_label.configure(text="Waiting for connection", fg="#fa0")

        self.root.after(0, _update)

    def _sync_audio(self):
        should_use = self.audio_var.get() and self.session is not None

        if should_use and self.session.audio_link is None:
            enabled = self.session.enable_audio()

            if not enabled:
                self.log("Audio unavailable (install pyaudio).")
        elif not should_use:
            self.session.stop_audio()

    def start_listener(self):
        if self.running:
            return

        try:
            port = int(self.port_var.get())
        except ValueError:
            self.log("Invalid port.")
            return

        import socket as _socket

        try:
            self.server_socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            self.server_socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            self.server_socket.bind(("0.0.0.0", port))
            self.server_socket.listen(2)
        except OSError as error:
            self.log("Could not bind port: %s" % error)
            return

        self.running = True
        self._load_monitors()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Listening on port %d" % port, fg="#2b7")
        self.log("Listening on 0.0.0.0:%d" % port)

        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break

            self.log("Incoming connection from %s" % (addr[0],))

            if not self.password_var.get():
                self.log("Set a password first.")
                conn.close()
                continue

            fernet = server_handshake(conn, self.password_var.get())

            if fernet is None:
                conn.close()
                self.log("Handshake failed (bad password).")
                continue

            session = HostSession(
                conn, fernet, addr, self.password_var.get(), self
            )
            session.start()

    def _load_monitors(self):
        try:
            screen = mss.mss()
            monitors = screen.monitors

            menu = self.monitor_list["menu"]
            menu.delete(0, "end")

            for idx in range(1, len(monitors)):
                mon = monitors[idx]
                menu.add_command(
                    label="Monitor %d (%dx%d)" % (idx, mon["width"], mon["height"]),
                    command=lambda i=idx: self.monitor_var.set(i),
                )

            self.monitor_var.set(1 if len(monitors) > 1 else 0)
        except Exception:
            self.monitor_var.set(0)

    def stop_listener(self):
        self.running = False

        if self.session is not None:
            self.session.close()

        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass

        self.server_socket = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="Not listening", fg="#c33")
        self.log("Listener stopped.")

    def send_file(self):
        if self.session is None:
            return

        path = filedialog.askopenfilename(title="Choose a file to send")

        if not path:
            return

        self.log("Sending file: %s" % os.path.basename(path))
        threading.Thread(
            target=self.session.push_file, args=(path,), daemon=True
        ).start()

    def apply_clipboard(self, text):
        if not text:
            return

        def _set():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.last_clipboard = text

        self.root.after(0, _set)
        self.log("Clipboard received from controller.")

    def _poll_clipboard(self):
        if self.session is not None:
            try:
                text = self.root.clipboard_get()

                if text and text != self.last_clipboard:
                    self.last_clipboard = text
                    self.session.send({"type": "clipboard", "text": text})
            except tk.TclError:
                pass

        self.root.after(400, self._poll_clipboard)


def main():
    root = tk.Tk()
    HostApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()