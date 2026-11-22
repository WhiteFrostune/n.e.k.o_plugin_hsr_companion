"""Record existing isolated-window frames locally when the player approves.

Use a fresh directory outside the repository. This script does not start the
helper, operate the game, submit requests, or upload frames.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from plugin.plugins.hsr_companion.assistant import DEFAULT_ASSISTANT_URL, VisionAssistantClient


def record(output: Path, seconds: float, max_mib: int, url: str):
    client = VisionAssistantClient(url, timeout=2)
    if not client.health().get("connected"):
        raise RuntimeError("Existing vision assistant is not available")
    output.mkdir(parents=True, exist_ok=False)
    base_wall, base_mono = time.time(), time.monotonic()
    last_seq = 0
    recorded = size = 0
    with (output / "frames.jsonl").open("x", encoding="utf-8") as index:
        index.write(json.dumps({"format": "hsr-frame-sequence-v1", "origin_timestamp": base_wall}) + "\n")
        index.flush()
        while time.monotonic() - base_mono < seconds:
            frame = client.fetch_frame(last_seq)
            received = time.monotonic() - base_mono
            if frame is None or frame.frame_seq <= last_seq:
                time.sleep(0.15)
                continue
            last_seq = frame.frame_seq
            if frame.timestamp < base_wall:
                continue  # Cached before the user-approved recording window.
            if size + len(frame.image_bytes) > max_mib * 1024 * 1024:
                break
            name = f"frame-{last_seq:010d}.bin"
            with (output / name).open("xb") as dest:
                dest.write(frame.image_bytes)
            index.write(
                json.dumps(
                    {
                        "frame_seq": last_seq,
                        "timestamp": frame.timestamp,
                        "available_offset": received,
                        "mime": frame.mime,
                        "file": name,
                        "sha256": hashlib.sha256(frame.image_bytes).hexdigest(),
                    }
                )
                + "\n"
            )
            index.flush()
            size += len(frame.image_bytes)
            recorded += 1
            time.sleep(0.1)
    print(json.dumps({"frames": recorded, "bytes": size, "seconds": time.monotonic() - base_mono}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--max-mib", type=int, default=4096)
    parser.add_argument("--assistant", default=DEFAULT_ASSISTANT_URL)
    args = parser.parse_args()
    if not 0 < args.seconds <= 600 or not 0 < args.max_mib <= 8192:
        parser.error("seconds must be in (0, 600] and max-mib in (0, 8192]")
    record(args.output, args.seconds, args.max_mib, args.assistant)


if __name__ == "__main__":
    main()
