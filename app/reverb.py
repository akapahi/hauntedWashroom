"""Streaming convolution reverb for the entity's 16 kHz mono PCM output.

The impulse response is synthetic: exponentially decaying noise with a short pre-delay and some
high-frequency damping, which sounds like a hard, tiled room. Convolution is FFT overlap-add, with the
tail carried between chunks so the reverb rings out after the entity stops talking.
"""

import numpy as np

PREDELAY_SECONDS = 0.02


class Reverb:
    def __init__(self, mix: float, decay_seconds: float, sample_rate: int = 16000):
        """mix: wet level 0..1. decay_seconds: time for the reverb to fall by 60 dB (room size)."""
        self.mix = mix
        n = max(1, int(sample_rate * decay_seconds))
        t = np.arange(n) / sample_rate
        noise = np.random.default_rng(7).standard_normal(n)
        ir = noise * np.exp(-6.9 * t / decay_seconds)            # -60 dB at decay_seconds
        ir = np.convolve(ir, np.ones(6) / 6, mode="same")         # damp the highs
        ir = np.concatenate([np.zeros(int(sample_rate * PREDELAY_SECONDS)), ir])
        self.ir = (ir / np.sqrt(np.sum(ir**2))).astype(np.float32)  # unit energy: wet ≈ dry loudness
        self.tail = np.zeros(len(self.ir) - 1, dtype=np.float32)
        self._ir_fft: dict[int, np.ndarray] = {}

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        n = len(x)
        if n == 0:
            return pcm
        size = n + len(self.ir) - 1
        nfft = 1 << (size - 1).bit_length()
        if nfft not in self._ir_fft:
            self._ir_fft[nfft] = np.fft.rfft(self.ir, nfft)
        wet = np.fft.irfft(np.fft.rfft(x, nfft) * self._ir_fft[nfft], nfft)[:size].astype(np.float32)
        wet[: len(self.tail)] += self.tail
        self.tail = wet[n:]
        return self._to_pcm((1 - 0.5 * self.mix) * x + self.mix * wet[:n])  # duck the dry signal to leave headroom

    def flush(self, frames: int) -> bytes | None:
        """Next `frames` samples of the ringing tail, or None once it has died away."""
        if np.max(np.abs(self.tail)) < 1e-3:
            return None
        out = self.tail[:frames]
        self.tail = np.concatenate([self.tail[frames:], np.zeros(len(out), dtype=np.float32)])
        return self._to_pcm(self.mix * out)

    def reset(self):
        self.tail[:] = 0

    @staticmethod
    def _to_pcm(y: np.ndarray) -> bytes:
        return (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
