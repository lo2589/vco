#!/usr/bin/env python3
"""Synthesize the promo soundtrack -> promo/audio.wav (48 s, 44.1 kHz stereo).

Procedural, royalty-free by construction: Am - F - C - G pads, sub bass,
kick/hats, pluck arpeggio, a riser into the architecture scene (28 s).
"""
import math
import wave
from pathlib import Path

import numpy as np

SR = 44100
DUR = 66.0
BPM = 96
BEAT = 60.0 / BPM          # 0.625 s
BAR = 4 * BEAT             # 2.5 s

OUT = Path(__file__).resolve().parent / "audio.wav"


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


# Am  F  C  G  (roots as midi), triad + octave for pads
CHORDS = [
    (45, [57, 60, 64, 69]),   # A2 / A3 C4 E4 A4
    (41, [53, 57, 60, 65]),   # F2 / F3 A3 C4 F4
    (48, [55, 60, 64, 67]),   # C3 / G3 C4 E4 G4
    (43, [55, 59, 62, 67]),   # G2 / G3 B3 D4 G4
]
CHORD_LEN = 4.0             # 12 chords over 48 s


def adsr_env(n, a, r, sr):
    env = np.ones(n)
    na = max(1, int(a * sr)); nr = max(1, int(r * sr))
    env[:na] = np.linspace(0, 1, na) ** 1.5
    if nr < n:
        env[-nr:] = np.linspace(1, 0, nr) ** 1.2
    return env


def pad(freq, dur, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    sig = np.zeros(n)
    for h, g in [(1, 1.0), (2, 0.45), (3, 0.22), (4, 0.12), (5, 0.06)]:
        sig += g * np.sin(2 * np.pi * freq * h * t + 0.3 * h)
    # slow vibrato keeps it alive
    sig *= 1 + 0.12 * np.sin(2 * np.pi * 0.35 * t)
    return sig * adsr_env(n, 0.9, 1.1, sr)


def pluck(freq, dur=0.30, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    sig = np.sin(2 * np.pi * freq * t) + 0.35 * np.sign(np.sin(2 * np.pi * freq * t)) * 0.4
    sig += 0.5 * np.sin(2 * np.pi * freq * 2 * t)
    return sig * np.exp(-t / 0.11) * 0.9


def kick(dur=0.22, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    f = 45 + 75 * np.exp(-t / 0.03)
    ph = np.cumsum(2 * np.pi * f / sr)
    return np.sin(ph) * np.exp(-t / 0.07)


def hat(dur=0.06, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = np.random.default_rng(7).standard_normal(n)
    hp = np.diff(noise, prepend=0)          # crude high-pass
    return hp * np.exp(-t / 0.012) * 0.6


def sub(freq, dur, sr=SR):
    n = int(dur * sr)
    t = np.arange(n) / sr
    return np.sin(2 * np.pi * freq * t) * adsr_env(n, 0.01, 0.12, sr)


def place(track, sig, at, gain=1.0):
    i = int(at * SR)
    j = min(len(track), i + len(sig))
    track[i:j] += gain * sig[: j - i]


def lowpass(x, alpha):
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += alpha * (x[i] - acc)
        y[i] = acc
    return y


def main():
    n_total = int(DUR * SR)
    left = np.zeros(n_total)
    right = np.zeros(n_total)

    pads = np.zeros(n_total)
    bass = np.zeros(n_total)
    drums = np.zeros(n_total)
    arp = np.zeros(n_total)

    n_chords = int(DUR / CHORD_LEN) + 1
    for ci in range(n_chords):
        root, tones = CHORDS[ci % 4]
        t0 = ci * CHORD_LEN
        dur = CHORD_LEN + 1.2
        layer = np.zeros(int(dur * SR))
        for tn in tones:
            layer += pad(midi(tn), dur)
        layer /= len(tones)
        place(pads, layer, t0, 0.16)
        # sub bass on beats 1 and 3 of each bar inside the chord span
        f = midi(root - 12)
        b = t0
        while b < t0 + CHORD_LEN - 0.01:
            place(bass, sub(f, 0.5), b, 0.30)
            place(bass, sub(f, 0.5), b + 2 * BEAT, 0.24)
            b += BAR

    # drums: kick on every beat, hats on off-8ths; skip bars of the intro
    t = 0.0
    while t < DUR - 0.1:
        if t >= 4.8:                       # enter with S2
            place(drums, kick(), t, 0.55 if t < 45 else 0.4)
        place(drums, hat(), t + BEAT / 2, 0.06 if t < 34 else 0.09)
        t += BEAT

    # pluck arpeggio: 8th notes climbing the chord tones, brighter in S6
    step = BEAT / 2
    t = 10.8                               # enter with the web scene
    while t < DUR - 0.5:
        ci = int(t / CHORD_LEN) % 4
        tones = CHORDS[ci][1] + [CHORDS[ci][1][0] + 12]
        idx = int(t / step) % len(tones)
        gain = 0.11
        if 46.0 <= t < 53.0:
            gain = 0.17                    # lift under the stats
        if t >= 61.0:
            gain = 0.08                    # settle for the CTA
        place(arp, pluck(midi(tones[idx] + 12)), t, gain)
        t += step

    # riser into the architecture reveal (32.7 -> 34.4) + impact
    riser_n = int(1.7 * SR)
    rt = np.arange(riser_n) / SR
    noise = np.random.default_rng(3).standard_normal(riser_n)
    riser = lowpass(noise, 0.08) * (rt / 1.7) ** 2
    place(drums, riser, 32.7, 0.20)
    place(drums, kick(0.5), 34.4, 0.9)
    crash = np.random.default_rng(5).standard_normal(int(1.2 * SR))
    crash = np.diff(crash, prepend=0) * np.exp(-np.arange(int(1.2 * SR)) / SR / 0.25)
    place(drums, crash, 34.4, 0.12)

    # gentle stereo: pads slightly widened, arp delayed on the right
    left += pads * 0.9 + bass + drums + arp
    right += np.roll(pads, int(0.012 * SR)) * 0.9 + bass + drums
    delay = int(0.28 * SR)
    right[delay:] += arp[:-delay] * 0.45

    # master: fades, soft clip, normalize
    mix = np.stack([left, right], axis=1)
    fade_in = int(0.6 * SR)
    fade_out = int(2.5 * SR)
    mix[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
    mix[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None] ** 1.4
    mix = np.tanh(mix * 1.15)
    mix *= 0.89 / np.max(np.abs(mix))

    pcm = (mix * 32767).astype(np.int16)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {DUR:.0f}s stereo)")


if __name__ == "__main__":
    main()
