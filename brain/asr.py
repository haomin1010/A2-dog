"""语音输入：arecord 录音 + 简单能量 VAD + 通义 qwen3-asr-flash 识别。"""
import base64
import io
import select
import subprocess
import sys
import time
import wave

import numpy as np

RATE = 16000
CHUNK = 480  # 30ms


class Recorder:
    """按回车开始；说完自动结束（静音 silence_s 秒），或再按一次回车结束。"""

    def __init__(self, device: str, silence_s: float = 0.8, max_s: float = 10.0, no_speech_s: float = 6.0):
        self.device = device
        self.silence_s = silence_s
        self.max_s = max_s
        self.no_speech_s = no_speech_s

    def record(self):
        cmd = ["arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-q"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        frames, noise = [], []
        started, silent_chunks, loud_chunks = False, 0, 0
        thr = None
        t0 = time.monotonic()
        print("🎙  录音中…（说完自动结束，或按回车结束）", flush=True)
        try:
            while True:
                data = proc.stdout.read(CHUNK * 2)
                if not data:
                    err = proc.stderr.read().decode(errors="ignore")
                    raise RuntimeError(f"arecord 结束: {err.strip()}")
                frames.append(data)
                rms = float(np.sqrt(np.mean(np.frombuffer(data, np.int16).astype(np.float32) ** 2)))
                elapsed = time.monotonic() - t0
                if thr is None:
                    noise.append(rms)
                    if len(noise) >= 10:  # 前 0.3 秒估计底噪
                        thr = max(3.0 * float(np.median(noise)), 500.0)
                    continue
                if rms > thr:
                    loud_chunks += 1
                    silent_chunks = 0
                    if loud_chunks >= 3:
                        started = True
                else:
                    loud_chunks = 0
                    silent_chunks += 1
                if started and silent_chunks * CHUNK / RATE >= self.silence_s:
                    break
                if not started and elapsed > self.no_speech_s:
                    print("（没听到说话）")
                    return None
                if elapsed > self.max_s:
                    break
                if select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                    break
        finally:
            proc.terminate()
            proc.wait()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(frames))
        return buf.getvalue()


class Transcriber:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    def transcribe(self, wav_bytes: bytes) -> str:
        b64 = base64.b64encode(wav_bytes).decode()
        r = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": f"data:audio/wav;base64,{b64}"}}]}],
            extra_body={"asr_options": {"language": "zh", "enable_itn": True}},
        )
        return (r.choices[0].message.content or "").strip()
