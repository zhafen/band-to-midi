#!/usr/bin/env python3
"""
Convert GarageBand .band files to MIDI.

Handles both file layout variants:
  - Newer: Alternatives/000/ProjectData  (raw binary)
  - Older: projectData                   (XML NSKeyedArchiver plist wrapping the same binary)

Usage:
    python gb_to_midi.py path/to/Song.band [output.mid]

If no output path is given, writes <band_name>.mid next to the .band directory.
"""

import struct
import sys
import plistlib
from pathlib import Path

import mido

# ── Binary format constants ────────────────────────────────────────────────────

FILE_HEADER_SIZE   = 24   # magic + version + total-size fields before first chunk
CHUNK_HEADER_SIZE  = 32   # tag[4] ver[2] track[2] flags[2] ctr[2] unk[16] size[4]
INTER_CHUNK_PAD    = 4    # null bytes written after every chunk

# track_id of the logical "MIDI input" track that holds all note events.
# GarageBand projects always route recorded MIDI through this track.
MIDI_TRACK_ID      = 23

# ── File loading ───────────────────────────────────────────────────────────────

def load_project_binary(band_path: Path) -> bytes:
    """Return the raw projectData binary from a .band package."""
    alt = band_path / "Alternatives" / "000" / "ProjectData"
    if alt.exists():
        return alt.read_bytes()

    plist_path = band_path / "projectData"
    if plist_path.exists():
        with plist_path.open("rb") as f:
            plist = plistlib.load(f)
        for obj in plist.get("$objects", []):
            if isinstance(obj, (bytes, bytearray)) and len(obj) > 4:
                return bytes(obj)
            if isinstance(obj, dict) and "NS.data" in obj:
                return bytes(obj["NS.data"])

    raise FileNotFoundError(f"No projectData found in {band_path}")


def load_metadata(band_path: Path) -> dict:
    """Load Alternatives/000/MetaData.plist if present."""
    meta = band_path / "Alternatives" / "000" / "MetaData.plist"
    if meta.exists():
        with meta.open("rb") as f:
            return plistlib.load(f)
    return {}

# ── Chunk scanning ─────────────────────────────────────────────────────────────

def scan_chunks(data: bytes, tag: str):
    """
    Find all occurrences of a chunk tag by byte-scanning the file.

    For each candidate, validates the 32-byte header and yields
    (offset, track_id, counter, payload_bytes).

    Byte-scanning is used because the chunk hierarchy includes container types
    (e.g. 'tSnI') whose sub-structure isn't fully documented; scanning avoids
    having to recursively decode containers.
    """
    needle = tag.encode("ascii")
    pos = 0
    while (pos := data.find(needle, pos)) != -1:
        if pos + CHUNK_HEADER_SIZE > len(data):
            break
        track_id   = struct.unpack_from("<H", data, pos + 6)[0]
        counter    = struct.unpack_from("<H", data, pos + 10)[0]
        payload_sz = struct.unpack_from("<I", data, pos + 28)[0]
        if 0 < payload_sz < len(data) - pos - CHUNK_HEADER_SIZE:
            payload = data[pos + CHUNK_HEADER_SIZE : pos + CHUNK_HEADER_SIZE + payload_sz]
            yield pos, track_id, counter, payload
        pos += 1

# ── Tempo detection ────────────────────────────────────────────────────────────

def detect_bpm(data: bytes) -> float:
    """
    Read BPM from the tempo qSvE (track_id=1).  Falls back to 120.

    In the tempo track each 16-byte record with byte[0]==0x00 stores the BPM
    as a uint8 at byte[1].
    """
    for _off, track_id, counter, payload in scan_chunks(data, "qSvE"):
        if track_id == 1:
            pos = 4  # skip preamble
            while pos + 16 <= len(payload):
                rec = payload[pos : pos + 16]
                if rec[0] == 0x00 and rec[1] > 0:
                    return float(rec[1])
                pos += 16
    return 120.0

# ── Event parsing ──────────────────────────────────────────────────────────────

def parse_events(payload: bytes) -> list:
    """
    Parse 16-byte event records from a qSvE payload (skips 4-byte preamble).

    Returns a list of tuples:
      ('note_on',  channel, onset_ticks, pitch, velocity)
      ('note_off', duration_ticks)          ← no channel; always paired with preceding note_on
      ('cc',       channel, onset_ticks, controller, value)
      ('pc',       channel, onset_ticks, program)
    """
    events = []
    pos = 4  # skip preamble
    while pos + 16 <= len(payload):
        rec = payload[pos : pos + 16]
        status = rec[0]
        pos += 16

        if status >= 0xF0:
            continue  # skip system/meta markers (e.g. 0xF1 end-of-block marker)

        etype   = status & 0xF0
        channel = status & 0x0F
        onset   = struct.unpack_from("<I", rec, 4)[0]

        if etype == 0x90:                          # Note On
            events.append(("note_on", channel, onset, rec[12], rec[11]))

        elif status == 0x80 or etype == 0x80 or status == 0x00:  # Note Off
            duration = struct.unpack_from("<I", rec, 12)[0]
            events.append(("note_off", duration))

        elif etype == 0xB0:                        # Control Change
            events.append(("cc", channel, onset, rec[11], rec[12]))

        elif etype == 0xC0:                        # Program Change
            events.append(("pc", channel, onset, rec[11]))

    return events


def pair_notes(events: list) -> list:
    """
    Pair consecutive note_on / note_off records into absolute-time note tuples.

    Returns list of (channel, onset_ticks, pitch, velocity, duration_ticks).
    """
    notes = []
    pending = None
    for evt in events:
        if evt[0] == "note_on":
            pending = evt
        elif evt[0] == "note_off" and pending is not None:
            _, channel, onset, pitch, velocity = pending
            _, duration = evt
            notes.append((channel, onset, pitch, velocity, duration))
            pending = None
    return notes

# ── MIDI construction ──────────────────────────────────────────────────────────

def find_note_payloads(data: bytes) -> list[tuple[int, bytes]]:
    """
    Return (counter, payload) pairs for every qSvE block that contains note events,
    sorted by counter (which preserves instrument order within the project).

    Scans all qSvE chunks on MIDI_TRACK_ID and keeps only those whose payload
    contains at least one NoteOn record.
    """
    candidates = {}
    for _off, track_id, counter, payload in scan_chunks(data, "qSvE"):
        if track_id != MIDI_TRACK_ID:
            continue
        # Quick check: any 0x9n NoteOn status byte in the payload?
        pos = 4  # skip preamble
        while pos + 16 <= len(payload):
            if payload[pos] & 0xF0 == 0x90:
                candidates[counter] = payload
                break
            pos += 16

    return sorted(candidates.items())


def build_midi(data: bytes, bpm: float, ticks_per_beat: int = 960) -> mido.MidiFile:
    """Build a mido.MidiFile (type 1) from the raw projectData binary."""

    note_blocks = find_note_payloads(data)   # [(counter, payload), ...]

    mid = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)

    # Track 0: tempo + time signature
    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)
    tempo_track.append(
        mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0)
    )
    tempo_track.append(
        mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0)
    )

    # Find earliest note onset across all tracks so we can trim leading silence
    min_onset = None
    for _ctr, payload in note_blocks:
        for evt in parse_events(payload):
            if evt[0] == "note_on":
                onset = evt[2]
                if min_onset is None or onset < min_onset:
                    min_onset = onset
    min_onset = min_onset or 0

    # Assign output channels: drums (source channel 9) keep channel 9;
    # melodic tracks get sequential channels 0, 1, 2 …  (skipping 9)
    melodic_ch_counter = 0
    for _ctr, payload in note_blocks:
        events = parse_events(payload)
        notes  = pair_notes(events)
        if not notes:
            continue

        # Determine if this block is a drum track by checking NoteOn channel
        source_channel = notes[0][0]
        if source_channel == 9:
            out_channel = 9
        else:
            out_channel = melodic_ch_counter
            melodic_ch_counter += 1
            if melodic_ch_counter == 9:   # skip the reserved drums channel
                melodic_ch_counter = 10

        # Collect all messages as (abs_tick, mido.Message)
        msgs: list[tuple[int, mido.Message]] = []

        # Program Change — first PC in this block, if any
        pc_program = next(
            (evt[3] for evt in events if evt[0] == "pc"),
            None
        )
        if pc_program is not None and out_channel != 9:
            msgs.append((0, mido.Message("program_change", channel=out_channel,
                                         program=pc_program, time=0)))

        # Control Changes
        for evt in events:
            if evt[0] == "cc":
                _, _ch, onset, ctrl, val = evt
                adj = max(0, onset - min_onset)
                msgs.append((adj, mido.Message("control_change", channel=out_channel,
                                               control=ctrl, value=val, time=0)))

        # Notes
        for _ch, onset, pitch, velocity, duration in notes:
            adj = max(0, onset - min_onset)
            msgs.append((adj,            mido.Message("note_on",  channel=out_channel, note=pitch, velocity=velocity, time=0)))
            msgs.append((adj + duration, mido.Message("note_off", channel=out_channel, note=pitch, velocity=0,        time=0)))

        # Sort by time; note_off before note_on at the same tick
        msgs.sort(key=lambda x: (x[0], 0 if x[1].type == "note_off" else 1))

        # Convert absolute ticks → delta ticks
        track = mido.MidiTrack()
        mid.tracks.append(track)
        prev = 0
        for abs_tick, msg in msgs:
            track.append(msg.copy(time=abs_tick - prev))
            prev = abs_tick

        track.append(mido.MetaMessage("end_of_track", time=0))

    return mid

# ── Entry point ────────────────────────────────────────────────────────────────

def convert(band_path: Path, output_path: Path | None = None) -> Path:
    """Convert a .band file to MIDI. Returns the output path."""
    if output_path is None:
        output_path = band_path.parent / (band_path.stem + ".mid")

    binary   = load_project_binary(band_path)
    metadata = load_metadata(band_path)

    bpm = float(metadata.get("BeatsPerMinute") or 0) or detect_bpm(binary)

    print(f"  BPM:    {bpm}")
    print(f"  Tracks: {metadata.get('NumberOfTracks', '?')}")

    mid = build_midi(binary, bpm)

    print(f"  MIDI tracks built: {len(mid.tracks) - 1} instrument + 1 tempo")

    mid.save(str(output_path))
    print(f"  Saved → {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} path/to/Song.band [output.mid]")
        sys.exit(1)

    band = Path(sys.argv[1])
    out  = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not band.exists():
        print(f"Error: {band} not found")
        sys.exit(1)

    print(f"Converting {band.name} ...")
    convert(band, out)
