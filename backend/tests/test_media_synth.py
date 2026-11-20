import io
import struct
import wave

from scripts.synth_media import AMPLITUDE, SAMPLE_RATE, Mixer, encode


def test_normalise_reuses_the_mixer_buffer() -> None:
    mixer = Mixer(1 / SAMPLE_RATE)
    mixer.buffer[:] = [0.5, -1.0, 0.25]
    original = mixer.buffer

    normalised = mixer.normalise()

    assert normalised is original
    assert mixer.buffer == [AMPLITUDE / 2, -AMPLITUDE, AMPLITUDE / 4]


def test_encode_writes_little_endian_mono_pcm() -> None:
    audio = encode([0.0, 1.0, -1.0])

    with wave.open(io.BytesIO(audio), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == SAMPLE_RATE
        assert struct.unpack("<hhh", source.readframes(3)) == (0, 32_767, -32_767)
