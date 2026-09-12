import base64
import socket
import threading

try:
    import pyaudio
    HAVE_AUDIO = True
except ImportError:
    pyaudio = None
    HAVE_AUDIO = False

from common import (
    AUDIO_CHUNK_BYTES,
    AUDIO_SALT,
    AUDIO_RATE,
    AUDIO_CHANNELS,
    AUDIO_PORT,
    make_fernet,
)

CHUNK_SAMPLES = AUDIO_CHUNK_BYTES // 2


class AudioLink:
    def __init__(self, password, peer_ip, port_out):
        self.fernet = make_fernet(password, AUDIO_SALT)
        self.peer_ip = peer_ip
        self.port_out = port_out
        self.port_in = AUDIO_PORT
        self.running = False
        self.threads = []

    def _capture_loop(self):
        audio = pyaudio.PyAudio()

        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=CHUNK_SAMPLES,
            )

            while self.running:
                chunk = stream.read(
                    CHUNK_SAMPLES, exception_on_overflow=False
                )

                if len(chunk) != AUDIO_CHUNK_BYTES:
                    continue

                encrypted = self.fernet.encrypt(chunk)
                self.sender_sock.sendto(
                    encrypted, (self.peer_ip, self.port_out)
                )
        except OSError:
            pass
        finally:
            try:
                stream.close()
                audio.terminate()
            except Exception:
                pass

    def _playback_loop(self):
        audio = pyaudio.PyAudio()

        try:
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=CHUNK_SAMPLES,
            )

            self.receiver_sock.settimeout(1.0)

            while self.running:
                try:
                    data, _addr = self.receiver_sock.recvfrom(8192)
                    chunk = self.fernet.decrypt(data)
                    stream.write(chunk)
                except socket.timeout:
                    continue
                except Exception:
                    pass
        finally:
            try:
                stream.close()
                audio.terminate()
            except Exception:
                pass

    def start(self):
        if not HAVE_AUDIO:
            return False

        self.running = True
        self.sender_sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        )
        self.receiver_sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        )

        self.receiver_sock.bind(("0.0.0.0", self.port_in))

        self.threads = [
            threading.Thread(
                target=self._capture_loop, daemon=True
            ),
            threading.Thread(
                target=self._playback_loop, daemon=True
            ),
        ]

        for thread in self.threads:
            thread.start()

        return True

    def stop(self):
        self.running = False

        for thread in self.threads:
            thread.join(timeout=1.0)

        for sock in (self.sender_sock, self.receiver_sock):
            try:
                sock.close()
            except OSError:
                pass