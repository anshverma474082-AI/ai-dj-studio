"""Synthesise the catalog's `audio_asset_url` files — the sound a lounge plays.

§4.2 forbids licensed music and real Spotify data, and royalty-free downloads are
unverifiable to a reviewer who cannot check a licence. So the demo audio is generated
here: original, deterministic, and derived from each record's *authored* acoustic
profile. A track marked 124 BPM / 8A / energy 0.78 gets a 124-BPM bed in a minor-
sounding scale at a correspondingly busy density, which makes §6.1's beatmatching
demonstration audible as well as true on the board.

Why the wave files have to exist as files: `audioAssetUrl` is a location a browser
fetches, and a 404 there leaves the console with a silent Now Playing card — the one
part of the demo the PRD says a reviewer must hear.

    backend/.venv/bin/python -m scripts.synth_media       # write every file
    backend/.venv/bin/python -m scripts.synth_media --force

Deterministic by construction: every value comes from `catalog.json` plus a per-track
seed, so two runs write byte-identical files.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import struct
import wave
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "catalog.json"
MEDIA_DIR = Path(__file__).resolve().parents[2] / "media"

SAMPLE_RATE = 22_050  # enough for a clean 16 Hz beat and a ~7 kHz tone
AMPLITUDE = 0.62
FADE_SECONDS = 0.05


# --- pitch -------------------------------------------------------------------

# A Camelot letter is a mode, not a chord: "A" is the minor family, "B" the major
# family. Two scales, chosen by that letter, give the demo an audible harmonic
# difference between records without pretending to reproduce an arrangement.
_MINOR = [0.0, 3.0, 5.0, 7.0, 10.0]
_MAJOR = [0.0, 4.0, 7.0, 11.0, 12.0]


def semitone_offset(camelot_key: str) -> int:
    """Place the root somewhere distinct for each of the 24 codes, in semitones from A2.

    Only a lookup off the code, so the same key always lands on the same pitch across
    runs and across tracks that claim to be compatible.
    """
    number = int(camelot_key[:-1])
    letter = camelot_key[-1].upper()
    return (number - 1) * 2 + (0 if letter == "A" else 7)


def frequency(root_semitones: int, scale_degree: int) -> float:
    """A2 (110 Hz) plus `root_semitones` plus a whole number of octaves of the degree."""
    return 110.0 * (2.0 ** ((root_semitones + scale_degree) / 12.0))


# --- voices ------------------------------------------------------------------


def sine(seconds: float, hz: float, phase: float = 0.0) -> list[float]:
    n = int(seconds * SAMPLE_RATE)
    step = 2.0 * math.pi * hz / SAMPLE_RATE
    return [math.sin(phase + i * step) for i in range(n)]


def exponential_decay(samples: list[float], tau_seconds: float) -> list[float]:
    tau = tau_seconds * SAMPLE_RATE
    return [s * math.exp(-i / tau) for i, s in enumerate(samples)]


def pad(samples: list[float]) -> list[float]:
    """Fade head and tail so a looped track has no click at the seam."""
    edge = int(FADE_SECONDS * SAMPLE_RATE)
    if len(samples) < 2 * edge:
        return samples
    for i in range(edge):
        k = i / edge
        samples[i] *= k
        samples[-1 - i] *= k
    return samples


# --- mixing ------------------------------------------------------------------


class Mixer:
    """Additive mixer with a fixed frame count, so parts cannot disagree on length."""

    def __init__(self, total_seconds: float) -> None:
        self.frames = int(total_seconds * SAMPLE_RATE)
        self.buffer = [0.0] * self.frames

    def add(self, start_seconds: float, samples: list[float], gain: float) -> None:
        start = int(start_seconds * SAMPLE_RATE)
        for offset, value in enumerate(samples):
            index = start + offset
            if index >= self.frames:
                break
            if index >= 0:
                self.buffer[index] += gain * value

    def normalise(self) -> list[float]:
        peak = max((abs(v) for v in self.buffer), default=0.0)
        if peak == 0.0:
            return self.buffer
        scale = AMPLITUDE / peak
        for index, value in enumerate(self.buffer):
            self.buffer[index] = value * scale
        return self.buffer


def render(track: dict) -> bytes:
    """One track's demo bed: kick on the beat, bass on the degree, melody, hats by energy."""
    bpm = track["bpm"]
    duration = float(track["duration_seconds"])
    energy = float(track["energy"])
    valence = float(track["valence"])
    key = track["camelot_key"]

    root = semitone_offset(key)
    scale = _MINOR if key[-1].upper() == "A" else _MAJOR
    beat = 60.0 / bpm
    bar = 4.0 * beat

    mixer = Mixer(duration)

    # The kick carries the tempo, which is the thing §6.1's ±8% rule is about. It has to be
    # unmistakable at 124 BPM and unmistakably different at 150 BPM.
    beat_seconds = 0.28
    for b in range(int(duration / beat) + 1):
        tone = exponential_decay(sine(beat_seconds, frequency(root, -12.0)), 0.06)
        mixer.add(b * beat, tone, 1.0)

    # Bass on the scale degree, one per bar, so harmonic compatibility is a pitch fact
    # and not only a number on the screen.
    for bar_index in range(int(duration / bar) + 1):
        degree = scale[bar_index % len(scale)]
        tone = sine(bar, frequency(root, degree - 12.0))
        mixer.add(bar_index * bar, pad(tone), 0.30)

    # Melody moves through the scale at a rate set by energy; valence picks the register.
    note_seconds = beat / 2.0
    octave_shift = 12.0 if valence >= 0.5 else 0.0
    steps_per_bar = max(1, round(2 + 4 * energy))
    step_index = 0
    for bar_index in range(int(duration / bar) + 1):
        for s in range(steps_per_bar):
            degree = scale[(step_index * 3) % len(scale)]
            tone = exponential_decay(
                sine(note_seconds / steps_per_bar * 3.0, frequency(root, degree + octave_shift)),
                0.10,
            )
            mixer.add(bar_index * bar + s * (bar / steps_per_bar), tone, 0.22)
            step_index += 1

    # Hats: eighth notes when the record is energetic, quarters when it is not.
    hat_every = bar / (8.0 if energy >= 0.6 else 4.0)
    # A short noise burst, seeded per track so two records do not share a hat pattern.
    rng_state = track_seed(track["id"])
    hit_seconds = 0.05
    for h in range(int(duration / hat_every) + 1):
        samples = []
        for _ in range(int(hit_seconds * SAMPLE_RATE)):
            rng_state = (rng_state * 1_103_515_245 + 12_345) & 0x7FFF_FFFF
            samples.append((rng_state / 0x7FFF_FFFF) * 2.0 - 1.0)
        mixer.add(h * hat_every, exponential_decay(samples, 0.012), 0.12 * energy)

    return encode(mixer.normalise())


def track_seed(track_id: str) -> int:
    """A small stable integer from the id — no `random.seed` globals, no clock."""
    accumulator = 7
    for char in track_id:
        accumulator = (accumulator * 31 + ord(char)) & 0x7FFF_FFFF
    return accumulator or 1


def encode(samples: list[float]) -> bytes:
    # Pack into one bounded buffer instead of materializing millions of tiny `bytes`
    # objects in a list before joining them. This keeps the free Render instance below
    # its 512 MiB memory ceiling when audio is prepared for a build.
    frames = bytearray(len(samples) * 2)
    for index, sample in enumerate(samples):
        struct.pack_into("<h", frames, index * 2, int(max(-1.0, min(1.0, sample)) * 32_767))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames)
    return buffer.getvalue()


def target_path(track: dict) -> Path:
    """Resolve the catalog's `/media/<name>` to a file inside `media/`.

    The basename only: a stored path is data an Admin wrote (§6.3), and honouring its
    directory component would let "../etc" leave the media folder.
    """
    url = track.get("audio_asset_url") or f"/media/{track['id']}.wav"
    return MEDIA_DIR / Path(url).name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="rewrite files that already exist"
    )
    args = parser.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text())["tracks"]
    MEDIA_DIR.mkdir(exist_ok=True)
    for track in catalog:
        path = target_path(track)
        if path.exists() and not args.force:
            print(f"  = {path.name} ({path.stat().st_size // 1024} KiB, unchanged)")
            continue
        path.write_bytes(render(track))
        print(
            f"  + {path.name} {track['duration_seconds']}s "
            f"{track['bpm']} BPM {track['camelot_key']} "
            f"({path.stat().st_size // 1024} KiB)"
        )
    print(f"wrote {len(catalog)} demo recordings to {MEDIA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
