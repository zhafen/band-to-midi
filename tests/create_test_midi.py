#!/usr/bin/env python3
"""
Generate test MIDI files for garageband-to-midi conversion testing.

Each file encodes what it tests in track names and embedded text meta events
so the expected structure is self-documenting when opened in any MIDI editor.

Usage:
    uv run python tests/create_test_midi.py

Output: tests/fixtures/*.mid
"""

from pathlib import Path

import mido
from music21 import instrument, metadata, meter, note, stream, tempo

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# 4/4, 120 BPM throughout
TIME_SIG = "4/4"
BPM = 120
BEATS_PER_MEASURE = 4


# ---------------------------------------------------------------------------
# Post-processing helpers (mido)
# ---------------------------------------------------------------------------

def _set_track_name(track: mido.MidiTrack, name: str) -> None:
    for i, msg in enumerate(track):
        if msg.type == "track_name":
            track[i] = mido.MetaMessage("track_name", name=name, time=0)
            return
    track.insert(0, mido.MetaMessage("track_name", name=name, time=0))


def _insert_text(track: mido.MidiTrack, tick: int, text: str) -> None:
    """Insert a MIDI text meta event at an absolute tick position."""
    abs_tick = 0
    for i, msg in enumerate(track):
        abs_tick += msg.time
        if abs_tick >= tick:
            delta = tick - (abs_tick - msg.time)
            track[i] = msg.copy(time=msg.time - delta)
            track.insert(i, mido.MetaMessage("text", text=text, time=delta))
            return
    # Append at end
    end_abs = abs_tick
    track.append(mido.MetaMessage("text", text=text, time=max(0, tick - end_abs)))


def _remap_channel(track: mido.MidiTrack, from_ch: int, to_ch: int,
                   drop_program_change: bool = False) -> mido.MidiTrack:
    new = mido.MidiTrack()
    for msg in track:
        if msg.type == "program_change" and drop_program_change and getattr(msg, "channel", -1) == from_ch:
            continue
        elif hasattr(msg, "channel") and msg.channel == from_ch:
            new.append(msg.copy(channel=to_ch))
        else:
            new.append(msg)
    return new


# ---------------------------------------------------------------------------
# Test 1: Minimal single-track grand piano
# ---------------------------------------------------------------------------

def create_test1_minimal_grand_piano() -> stream.Score:
    """
    Test 1: Minimal single track, grand piano.

    - 1 track, channel 0, program 0 (Acoustic Grand Piano)
    - 4/4 at 120 BPM
    - C-major scale up then back down (15 quarter notes, velocity 80)
    - Starts at beat 0, no leading silence
    """
    score = stream.Score()
    score.insert(0, metadata.Metadata())
    score.metadata.title = "Test1: Minimal Grand Piano"
    score.metadata.composer = "band-to-midi test suite"

    part = stream.Part()
    piano = instrument.Piano()
    piano.midiProgram = 0
    part.insert(0, piano)
    part.insert(0, meter.TimeSignature(TIME_SIG))
    part.insert(0, tempo.MetronomeMark(number=BPM))

    pitches = ["C4", "D4", "E4", "F4", "G4", "A4", "B4", "C5",
               "B4", "A4", "G4", "F4", "E4", "D4", "C4"]
    for i, pitch in enumerate(pitches):
        n = note.Note(pitch)
        n.duration.quarterLength = 1
        n.volume.velocity = 80
        part.insert(i, n)

    score.append(part)
    return score


def _postprocess_test1(path: Path, tpb: int) -> None:
    mid = mido.MidiFile(str(path))

    _set_track_name(mid.tracks[1], "Grand Piano")
    _insert_text(
        mid.tracks[1], 0,
        "TEST1: single track | program=0 grand piano | C-major scale up+down "
        "| 15 quarter notes | vel=80 | no silence | ch=0",
    )

    mid.save(str(path))


# ---------------------------------------------------------------------------
# Test 2: Complex 3-track
# ---------------------------------------------------------------------------

def create_test2_complex_three_track() -> stream.Score:
    """
    Test 2: Three tracks exercising timing edge cases.

    Measure grid (4/4, 120 BPM, 8 measures = 32 beats):

        m:  1    2    3    4    5    6    7    8
        b:  0----4----8----12---16---20---24---28--32

        Track 1 Piano:  |==============================|  full (beats 0-31)
        Track 2 Guitar:          |============|           starts m3 (beat 8),
                                                          last note m6b4 (beat 23)
        Track 3 Drums:  |====|        GAP     |==========|
                         m1-2  (beats 0-7)         m5-8 (beats 16-31)
                              gap = m3-4 (beats 8-15)

    Note choices:
        Piano:  C4, vel 80
        Guitar: E4, vel 70  (distinct pitch for easy identification)
        Drums:  kick C2 (MIDI 36) every beat, snare D2 (MIDI 38) on beats 2+4
    """
    score = stream.Score()
    score.insert(0, metadata.Metadata())
    score.metadata.title = "Test2: Complex 3-Track"
    score.metadata.composer = "band-to-midi test suite"

    TOTAL = 32
    GUITAR_START, GUITAR_END = 8, 24    # beats [8, 24)
    GAP_START, GAP_END = 8, 16           # drum gap beats [8, 16)

    # Track 1: Piano, beats 0–31
    part1 = stream.Part()
    part1.insert(0, instrument.Piano())
    part1.insert(0, meter.TimeSignature(TIME_SIG))
    part1.insert(0, tempo.MetronomeMark(number=BPM))
    for b in range(TOTAL):
        n = note.Note("C4")
        n.duration.quarterLength = 1
        n.volume.velocity = 80
        part1.insert(b, n)
    score.append(part1)

    # Track 2: Guitar, beats 8–23 (delayed start + early end)
    part2 = stream.Part()
    part2.insert(0, instrument.Guitar())
    for b in range(GUITAR_START, GUITAR_END):
        n = note.Note("E4")
        n.duration.quarterLength = 1
        n.volume.velocity = 70
        part2.insert(b, n)
    score.append(part2)

    # Track 3: Drums (channel 9), beats 0–7 and 16–31
    part3 = stream.Part()
    perc = instrument.Percussion()
    part3.insert(0, perc)

    def drum_beat(beat: int) -> None:
        kick = note.Note("C2")   # MIDI 36 = Bass Drum 1
        kick.duration.quarterLength = 1
        kick.volume.velocity = 90
        part3.insert(beat, kick)
        if beat % BEATS_PER_MEASURE in (1, 3):  # snare on beat 2 and 4
            snare = note.Note("D2")  # MIDI 38 = Acoustic Snare
            snare.duration.quarterLength = 1
            snare.volume.velocity = 70
            part3.insert(beat, snare)

    for b in list(range(GAP_START)) + list(range(GAP_END, TOTAL)):
        drum_beat(b)

    score.append(part3)
    return score


def _postprocess_test2(path: Path, tpb: int) -> None:
    mid = mido.MidiFile(str(path))

    GUITAR_START_TICK = 8 * tpb
    GAP_START_TICK = 8 * tpb
    GAP_END_TICK = 16 * tpb

    # Track names
    _set_track_name(mid.tracks[1], "Piano - Full Duration m1-8")
    _set_track_name(mid.tracks[2], "Guitar - Delayed Start m3 Early End m6")
    _set_track_name(mid.tracks[3], "Drums - Gap m3-4")

    # Text annotations
    _insert_text(mid.tracks[1], 0,
                 "TRACK1: piano | full 8 measures (beats 0-31) | C4 vel=80 | ch=0")
    _insert_text(mid.tracks[2], GUITAR_START_TICK,
                 "TRACK2: guitar | delayed start beat=8 (m3) | early end beat=23 (m6b4) | E4 vel=70 | ch=1")
    _insert_text(mid.tracks[3], 0,
                 "TRACK3: drums | beats 0-7 (m1-2) + 16-31 (m5-8) | gap beats 8-15 (m3-4) | ch=9")
    _insert_text(mid.tracks[3], GAP_START_TICK,
                 "TRACK3: drum gap starts (beat=8 m3 downbeat)")
    _insert_text(mid.tracks[3], GAP_END_TICK,
                 "TRACK3: drums resume (beat=16 m5 downbeat)")

    # Fix drum channel: music21 assigns an arbitrary channel; we need ch=9
    # Detect the channel currently used in track 3
    drum_src_ch = None
    for msg in mid.tracks[3]:
        if hasattr(msg, "channel"):
            drum_src_ch = msg.channel
            break
    if drum_src_ch is not None and drum_src_ch != 9:
        mid.tracks[3] = _remap_channel(mid.tracks[3], drum_src_ch, 9,
                                       drop_program_change=True)
        # Restore the track name that _remap_channel drops
        _set_track_name(mid.tracks[3], "Drums - Gap m3-4")

    mid.save(str(path))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_midi(score: stream.Score, path: Path, postprocess=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    score.write("midi", fp=str(path))
    if postprocess is not None:
        mid = mido.MidiFile(str(path))
        postprocess(path, mid.ticks_per_beat)
    print(f"Created: {path}")


def main() -> None:
    write_midi(
        create_test1_minimal_grand_piano(),
        FIXTURES_DIR / "test1_minimal_grand_piano.mid",
        postprocess=_postprocess_test1,
    )
    write_midi(
        create_test2_complex_three_track(),
        FIXTURES_DIR / "test2_complex_3track.mid",
        postprocess=_postprocess_test2,
    )


if __name__ == "__main__":
    main()
