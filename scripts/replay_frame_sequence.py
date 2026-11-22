"""Expose a recorded sequence through the assistant's fetch_frame interface.

Callers may poll at any interval. Only frames available by that instant are
returned; an identical sequence never becomes a newly captured frame.
"""

import argparse
import bisect
import hashlib
import json
import math
import time
from functools import lru_cache
from pathlib import Path

from plugin.plugins.hsr_companion.assistant import AssistantFrame


class RecordedFrames:
    def __init__(self, directory: Path):
        root = directory.resolve()
        with (root / "frames.jsonl").open(encoding="utf-8") as source:
            header = json.loads(next(source))
            if header.get("format") != "hsr-frame-sequence-v1":
                raise ValueError("Unknown recorded frame format")
            start = float(header["origin_timestamp"])
            self.rows = []
            self.availability = []
            previous_seq, previous_capture = 0, -1.0
            for line in source:
                row = json.loads(line)
                captured = float(row["timestamp"]) - start
                available = max(float(row["available_offset"]), captured)
                file = (root / row["file"]).resolve()
                seq = row["frame_seq"]
                if (
                    not math.isfinite(captured)
                    or not math.isfinite(available)
                    or captured < 0
                    or captured < previous_capture
                    or type(seq) is not int
                    or seq <= previous_seq
                    or (self.availability and available < self.availability[-1])
                    or file.parent != root
                ):
                    raise ValueError("Invalid or out-of-order recorded frame")
                self.rows.append((seq, captured, row["mime"], file, row["sha256"]))
                self.availability.append(available)
                previous_seq, previous_capture = seq, captured
        if not self.rows:
            raise ValueError("Recording contains no complete frames")
        self.game = {"running": True, "pid": 1, "minimized": False, "foreground": True}
        self.capture = {"state": "capturing", "isolated_window": True, "frame_semantics": "every_capture"}
        self.wall_start = time.time()
        self.tick_start = time.monotonic()

    def _position(self):
        return bisect.bisect_right(self.availability, time.monotonic() - self.tick_start) - 1

    @lru_cache(maxsize=2)
    def _payload(self, index):
        _, _, _, file, digest = self.rows[index]
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Recorded frame checksum differs")
        return data

    def health(self):
        index = self._position()
        return {
            "connected": True,
            "frame_seq": self.rows[index][0] if index >= 0 else 0,
            "game": dict(self.game),
            "capture": dict(self.capture),
        }

    def fetch_frame(self, since_frame=0):
        index = self._position()
        if index < 0:
            return None
        seq, captured, mime, _, digest = self.rows[index]
        if seq <= since_frame:
            return None
        return AssistantFrame(
            seq, self.wall_start + captured, self._payload(index), mime, digest, dict(self.game), dict(self.capture)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    frames = RecordedFrames(args.recording)
    seen = 0
    while seen != frames.rows[-1][0]:
        frame = frames.fetch_frame(seen)
        if frame:
            seen = frame.frame_seq
            print(
                json.dumps({"frame_seq": seen, "replay_timestamp": frame.timestamp, "sha256": frame.signature}),
                flush=True,
            )
        time.sleep(0.03)


if __name__ == "__main__":
    main()
