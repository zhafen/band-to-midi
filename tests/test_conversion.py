"""
Parametric round-trip tests: convert each .band fixture and compare musical
content against the corresponding reference .mid file.

For each fixture pair the tests verify:
  - BPM within 1 BPM
  - Same number of instrument tracks
  - Same channels present
  - Per channel: identical pitches, velocities, and onset/duration in beats
    (timing within TIMING_TOL beats)
"""

import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from gb_to_midi import build_midi, detect_bpm, load_metadata, load_project_binary

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TIMING_TOL = 0.05  # beats

FIXTURE_PAIRS = [
    pytest.param(
        "test1_minimal_grand_piano.mid",
        "test1_minimal_grand_pieano.band",
        id="test1_minimal_grand_piano",
    ),
    pytest.param(
        "test2_complex_3track.mid",
        "test2_complex_3track.band",
        id="test2_complex_3track",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notes_by_channel(mid: mido.MidiFile) -> dict[int, list[dict]]:
    """Parse note events from all tracks, keyed by channel with beat times."""
    tpb = mid.ticks_per_beat
    by_channel: dict[int, list[dict]] = {}

    for track in mid.tracks:
        abs_tick = 0
        pending: dict[tuple[int, int], tuple[int, int]] = {}  # (ch, pitch) -> (onset, vel)
        for msg in track:
            abs_tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                pending[(msg.channel, msg.note)] = (abs_tick, msg.velocity)
            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                key = (msg.channel, msg.note)
                if key in pending:
                    onset_tick, vel = pending.pop(key)
                    by_channel.setdefault(msg.channel, []).append({
                        "pitch": msg.note,
                        "onset": onset_tick / tpb,
                        "duration": (abs_tick - onset_tick) / tpb,
                        "velocity": vel,
                    })

    for notes in by_channel.values():
        notes.sort(key=lambda n: (n["onset"], n["pitch"]))
    return by_channel


def _bpm_from_midi(mid: mido.MidiFile) -> float:
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                return mido.tempo2bpm(msg.tempo)
    return 120.0


def _convert(band_name: str) -> tuple[mido.MidiFile, float]:
    """Load and convert a .band fixture, returning (MidiFile, bpm)."""
    band_path = FIXTURES_DIR / band_name
    binary = load_project_binary(band_path)
    meta = load_metadata(band_path)
    bpm = float(meta.get("BeatsPerMinute") or 0) or detect_bpm(binary)
    return build_midi(binary, bpm), bpm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("midi_name,band_name", FIXTURE_PAIRS)
def test_bpm(midi_name: str, band_name: str) -> None:
    ref_bpm = _bpm_from_midi(mido.MidiFile(str(FIXTURES_DIR / midi_name)))
    _, out_bpm = _convert(band_name)
    assert abs(ref_bpm - out_bpm) < 1.0, (
        f"BPM mismatch: ref={ref_bpm:.1f} out={out_bpm:.1f}"
    )


@pytest.mark.parametrize("midi_name,band_name", FIXTURE_PAIRS)
def test_instrument_track_count(midi_name: str, band_name: str) -> None:
    ref_mid = mido.MidiFile(str(FIXTURES_DIR / midi_name))
    ref_count = len(ref_mid.tracks) - 1  # subtract tempo track
    out_mid, _ = _convert(band_name)
    out_count = len(out_mid.tracks) - 1
    assert ref_count == out_count, (
        f"Instrument track count: ref={ref_count} out={out_count}"
    )


@pytest.mark.parametrize("midi_name,band_name", FIXTURE_PAIRS)
def test_notes_match(midi_name: str, band_name: str) -> None:
    ref = _notes_by_channel(mido.MidiFile(str(FIXTURES_DIR / midi_name)))
    out_mid, _ = _convert(band_name)
    out = _notes_by_channel(out_mid)

    assert set(ref.keys()) == set(out.keys()), (
        f"Channels present: ref={sorted(ref.keys())} out={sorted(out.keys())}"
    )

    for ch in sorted(ref.keys()):
        r_notes, o_notes = ref[ch], out[ch]
        assert len(r_notes) == len(o_notes), (
            f"ch={ch}: note count ref={len(r_notes)} out={len(o_notes)}"
        )
        for i, (r, o) in enumerate(zip(r_notes, o_notes)):
            assert r["pitch"] == o["pitch"], (
                f"ch={ch} note[{i}]: pitch ref={r['pitch']} out={o['pitch']}"
            )
            assert r["velocity"] == o["velocity"], (
                f"ch={ch} note[{i}]: velocity ref={r['velocity']} out={o['velocity']}"
            )
            onset_diff = abs(r["onset"] - o["onset"])
            assert onset_diff <= TIMING_TOL, (
                f"ch={ch} note[{i}] pitch={r['pitch']}: "
                f"onset ref={r['onset']:.4f} out={o['onset']:.4f} diff={onset_diff:.4f}"
            )
            dur_diff = abs(r["duration"] - o["duration"])
            assert dur_diff <= TIMING_TOL, (
                f"ch={ch} note[{i}] pitch={r['pitch']}: "
                f"duration ref={r['duration']:.4f} out={o['duration']:.4f} diff={dur_diff:.4f}"
            )
