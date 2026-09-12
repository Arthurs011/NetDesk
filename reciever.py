import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from audio import HAVE_AUDIO, AudioLink
from common import (
    AUDIO_PORT,
    CONTROL_PORT,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    resolve_key_event,
    send_message,
    recv_message,
    client_handshake,
)


class ClientApp:
    def __init__(self, root):
        self.root = root
        root.title("NetDesk - Controller")
        root.geometry("1320x880")

        self.sock = None
        self.fernet = None
        self.connected = False
        self.recv_thread = None
        self.send_lock = threading.Lock()
        self.download_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

        self.monitors = []
        self.frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
        self.latest_image = None
        self.frame_lock = threading.Lock()

        self.pressed_keys = {}
        self.last_move_time = 0.0
        self.last_clipboard = None
        self.audio_link = None
        self.file_handle = None
        self.file_size = 0
        self.file_received = 0

        self.build_ui()

    def build_ui(self):
        top = tk.Frame(self.root, padx=10, pady=8)
        top.pack(fill="x")

        tk.Label(top, text="Server IP:").pack(side="left")
        self.ip_var = tk.StringVar(value="127.0.0.1")
        tk.Entry(top, textvariable=self.ip_var, width=16).pack(side="left", padx=4)

        tk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(CONTROL_PORT))
        tk.Entry(top, textvariable=self.port_var, width=6).pack(side="left", padx=4)

        tk.Label(top, text="Password:").pack(side="left")
        self.password_var = tk.StringVar()
        tk.Entry(top, textvariable=self.password_var, show="*", width=12).pack(
            side="left", padx=4
        )

        self.connect_btn = tk.Button(
            top, text="Connect", command=self.connect
        )
        self.connect_btn.pack(side="left", padx=6)

        self.disconnect_btn = tk.Button(
            top, text="Disconnect", command=self.disconnect, state="disabled"
        )
        self.disconnect_btn.pack(side="left")

        tk.Label(top, text="Monitor:").pack(side="left", padx=(14, 2))
        self.monitor_var = tk.StringVar()
        self.monitor_menu = tk.OptionMenu(
            top, self.monitor_var, "", command=self.change_monitor
        )
        self.monitor_menu.config(state="disabled")
        self.monitor_menu.pack(side="left")

        self.audio_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            top, text="Audio", variable=self.audio_var, command=self.toggle_audio
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            top, text="Send File", command=self.send_file
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            top, text="Downloads", command=self.open_downloads
        ).pack(side="left", padx=(6, 0))

        self.fps_label = tk.Label(top, text="FPS: -")
        self.fps_label.pack(side="right", padx=6)

        self.status_label = tk.Label(top, text="Disconnected", fg="#c33")
        self.status_label.pack(side="right")

        self.canvas = tk.Canvas(
            self.root,
            width=FRAME_WIDTH,
            height=FRAME_HEIGHT,
            bg="#111",
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.on_mouse_button(e, "left", "down"))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.on_mouse_button(e, "left", "up"))
        self.canvas.bind("<ButtonPress-2>", lambda e: self.on_mouse_button(e, "middle", "down"))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self.on_mouse_button(e, "middle", "up"))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.on_mouse_button(e, "right", "down"))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self.on_mouse_button(e, "right", "up"))
        self.canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.send_input("mouse", {"action": "scroll", "amount": 1}))
        self.canvas.bind("<Button-5>", lambda e: self.send_input("mouse", {"action": "scroll", "amount": -1}))

        self.root.bind_all("<KeyPress>", self.on_key_press)
        self.root.bind_all("<KeyRelease>", self.on_key_release)

        self.root.after(33, self._render_tick)
        self.root.after(400, self._poll_clipboard)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def send_input(self, msg_type, payload):
        if not self.connected:
            return

        message = dict(payload)
        message["type"] = msg_type

        with self.send_lock:
            send_message(self.sock, self.fernet, message)

    def log_status(self, text, color="#666"):
        def _update():
            self.status_label.configure(text=text, fg=color)

        self.root.after(0, _update)

    def connect(self):
        if self.connected:
            return

        host = self.ip_var.get().strip()
        password = self.password_var.get()

        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("NetDesk", "Invalid port.")
            return

        self.log_status("Connecting...", "#fa0")
        self.connect_btn.configure(state="disabled")

        def do_connect():
            error = None

            try:
                sock = socket.create_connection((host, port), timeout=8)
                sock.settimeout(None)
            except OSError as exc:
                error = "Cannot reach %s:%d (%s)" % (host, port, exc)

            if error is None:
                fernet, error = client_handshake(sock, password)

            if error is not None:
                try:
                    sock.close()
                except OSError:
                    pass

                self.root.after(0, lambda err=error: self._connect_failed(err))
                return

            self.root.after(
                0, lambda s=sock, f=fernet, h=host: self._connected(s, f, h)
            )

        threading.Thread(target=do_connect, daemon=True).start()

    def _connect_failed(self, error):
        self.connect_btn.configure(state="normal")
        self.log_status("Failed: %s" % error, "#c33")

    def _connected(self, sock, fernet, host):
        self.sock = sock
        self.fernet = fernet
        self.connected = True
        self.log_status("Connected to %s" % host, "#2b7")
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.monitor_menu.config(state="normal")

        self.recv_thread = threading.Thread(target=self.recv_loop, daemon=True)
        self.recv_thread.start()

    def disconnect(self):
        if not self.connected:
            return

        self.connected = False
        self.stop_audio()

        try:
            self.sock.close()
        except OSError:
            pass

        self.log_status("Disconnected", "#c33")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.monitor_menu.config(state="disabled")
        self.monitor_var.set("")

    def on_close(self):
        self.disconnect()
        self.root.destroy()

    def recv_loop(self):
        while self.connected:
            try:
                header, payload = recv_message(self.sock, self.fernet)
            except Exception:
                break

            if header is None:
                break

            msg_type = header.get("type")

            if msg_type == "monitors":
                self.root.after(0, lambda h=header: self._apply_monitors(h))

            elif msg_type == "video":
                self.handle_video(header, payload)

            elif msg_type == "clipboard":
                text = header.get("text", "")

                if text:
                    self.last_clipboard = text
                    self.root.after(0, lambda t=text: self._set_clipboard(t))

            elif msg_type == "file_meta":
                self._start_receive_file(header)

            elif msg_type == "file_chunk":
                self._append_file(payload)

            elif msg_type == "file_end":
                self._finish_receive_file(header.get("name", ""))

        self.root.after(0, self.disconnect)

    def _apply_monitors(self, header):
        self.monitors = header.get("list", [])
        menu = self.monitor_menu["menu"]
        menu.delete(0, "end")

        for mon in self.monitors:
            label = "Monitor %d (%dx%d)" % (
                mon["index"], mon["width"], mon["height"]
            )
            menu.add_command(
                label=label,
                command=lambda i=mon["index"]: self._select_monitor(i),
            )

        if self.monitors:
            self.monitor_var.set("Monitor %d" % self.monitors[0]["index"])
            self._select_monitor(self.monitors[0]["index"])

    def _select_monitor(self, index):
        self.monitor_var.set("Monitor %d" % index)
        self.send_input("monitor", {"index": index})

    def change_monitor(self, _value):
        pass

    def handle_video(self, header, payload):
        bbox = header.get("bbox")

        if bbox is not None:
            x, y, w, h = bbox
            image = cv2.imdecode(
                np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR
            )

            if image is not None:
                if image.shape[1] != w or image.shape[0] != h:
                    image = cv2.resize(image, (w, h))

                x1 = min(x + w, FRAME_WIDTH)
                y1 = min(y + h, FRAME_HEIGHT)

                with self.frame_lock:
                    self.frame[y:y1, x:x1] = image

                rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
                self.latest_image = Image.fromarray(rgb)

        fps = header.get("fps")

        if fps:
            def _update_fps(value=fps):
                self.fps_label.configure(text="FPS: %s" % value)

            self.root.after(0, _update_fps)

    def _render_tick(self):
        if self.latest_image is not None:
            self.canvas_image = ImageTk.PhotoImage(self.latest_image)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self.canvas_image, anchor="nw")

        self.root.after(33, self._render_tick)

    def on_mouse_move(self, event):
        now = time.monotonic()

        if now - self.last_move_time < 0.03:
            return

        self.last_move_time = now
        self.send_input("mouse", {
            "action": "move",
            "x": event.x,
            "y": event.y,
        })

    def on_mouse_button(self, event, button, state):
        self.send_input("mouse", {
            "action": "%s_%s" % (button, state)
        })

    def on_mouse_wheel(self, event):
        amount = 1 if event.delta > 0 else -1
        self.send_input("mouse", {"action": "scroll", "amount": amount})

    def on_key_press(self, event):
        names = resolve_key_event(event.char, event.keysym)

        if not names:
            return

        if event.keycode in self.pressed_keys:
            return

        self.pressed_keys[event.keycode] = names
        self.send_input("key", {"action": "down", "names": names})

    def on_key_release(self, event):
        names = self.pressed_keys.pop(event.keycode, None)

        if not names:
            return

        self.send_input("key", {"action": "up", "names": list(reversed(names))})

    def _set_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _poll_clipboard(self):
        if self.connected:
            try:
                text = self.root.clipboard_get()

                if text and text != self.last_clipboard:
                    self.last_clipboard = text
                    self.send_input("clipboard", {"text": text})
            except tk.TclError:
                pass

        self.root.after(400, self._poll_clipboard)

    def _start_receive_file(self, header):
        name = os.path.basename(header.get("name", "file.bin"))
        self.file_size = header.get("size", 0)
        self.file_received = 0

        self.file_handle = open(
            os.path.join(self.download_dir, name), "wb"
        )

        self.log_status("Receiving %s..." % name, "#fa0")

    def _append_file(self, payload):
        if self.file_handle is not None:
            self.file_handle.write(payload)
            self.file_received += len(payload)

    def _finish_receive_file(self, name):
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None

        self.log_status(
            "Received %s (%d bytes)" % (name, self.file_received), "#2b7"
        )

    def toggle_audio(self):
        if not self.connected:
            return

        if self.audio_var.get() and self.audio_link is None:
            if not HAVE_AUDIO:
                messagebox.showinfo(
                    "NetDesk", "Audio requires 'pyaudio' to be installed."
                )
                self.audio_var.set(False)
                return

            self.audio_link = AudioLink(
                self.password_var.get(), self.ip_var.get().strip(), AUDIO_PORT
            )

            if not self.audio_link.start():
                messagebox.showinfo("NetDesk", "Could not start audio.")
                self.audio_link = None
                self.audio_var.set(False)
        elif not self.audio_var.get():
            self.stop_audio()

    def stop_audio(self):
        if self.audio_link is not None:
            self.audio_link.stop()
            self.audio_link = None

    def send_file(self):
        if not self.connected:
            messagebox.showinfo("NetDesk", "Connect first.")
            return

        path = filedialog.askopenfilename(title="Choose a file to send")

        if not path:
            return

        self.log_status("Sending %s..." % os.path.basename(path), "#fa0")

        def worker():
            name = os.path.basename(path)
            size = os.path.getsize(path)

            try:
                self.send_input("file_meta", {"name": name, "size": size})

                with open(path, "rb") as file:
                    while True:
                        chunk = file.read(65536)

                        if not chunk:
                            break

                        with self.send_lock:
                            send_message(
                                self.sock, self.fernet,
                                {"type": "file_chunk"}, chunk,
                            )

                with self.send_lock:
                    send_message(
                        self.sock, self.fernet,
                        {"type": "file_end", "name": name},
                    )

                self.log_status("Sent %s" % name, "#2b7")
            except (OSError, ConnectionError):
                self.log_status("File send failed.", "#c33")

        threading.Thread(target=worker, daemon=True).start()

    def open_downloads(self):
        if sys.platform == "darwin":
            subprocess.Popen(["open", self.download_dir])
        elif sys.platform.startswith("win"):
            os.startfile(self.download_dir)
        else:
            subprocess.Popen(["xdg-open", self.download_dir])


def main():
    root = tk.Tk()
    ClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()