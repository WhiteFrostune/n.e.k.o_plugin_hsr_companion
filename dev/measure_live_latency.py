"""Read-only live latency sample for step 0 of the v2 architecture.

Measures, on the real game window:
  1. capture one fresh frame through the local vision assistant
  2. page detection (probe_scene)
  3. the current full-screen analyze pass
  4. whole-frame OCR (one call) vs native tiled OCR (N calls)
  5. numeric-token agreement between the two OCR modes in a small region

The small region is derived from the same frame's equipment evidence unless
--small-region overrides it. Prints timings and, only with --show-stats, the
numeric tokens for the user to check against the screen. Never writes images,
OCR text, player data or files.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from io import BytesIO
from pathlib import Path

HOST_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(HOST_ROOT))

from PIL import Image, ImageOps  # noqa: E402

from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter  # noqa: E402
from plugin.plugins.hsr_companion.adapters.text_reader import native_tiles  # noqa: E402
from plugin.plugins.hsr_companion.assistant import VisionAssistantClient, VisionAssistantProcess  # noqa: E402
from plugin.plugins.hsr_companion.data import ExternalKnowledgeCatalog  # noqa: E402
from plugin.plugins.hsr_companion.data.avatar_pack import AvatarTemplatePack  # noqa: E402
from plugin.plugins.hsr_companion.data.vision_runtime import VisionMatchRuntime  # noqa: E402

NUMBER = re.compile(r"\d+(?:[.,]\d+)?%?")


def summary(values: list[float]) -> dict[str, float]:
    return {"min": round(min(values), 3), "avg": round(statistics.fmean(values), 3), "max": round(max(values), 3)}


def parse_region(value: str) -> tuple[float, float, float, float]:
    try:
        left, top, right, bottom = (float(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("region must be four comma-separated fractions") from exc
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise argparse.ArgumentTypeError("region must be four increasing fractions in [0, 1]")
    return left, top, right, bottom


def pixel_box(image: Image.Image, region: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    left, top, right, bottom = region
    return (int(image.width * left), int(image.height * top),
            int(image.width * right), int(image.height * bottom))


def open_image(payload: bytes) -> Image.Image:
    with Image.open(BytesIO(payload)) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB")


def dedupe(boxes: list[dict]) -> list[dict]:
    seen, result = set(), []
    for box in boxes:
        key = (str(box.get("text") or ""),
               round((float(box["left"]) + float(box["right"])) / 16),
               round((float(box["top"]) + float(box["bottom"])) / 16))
        if key not in seen:
            seen.add(key)
            result.append(box)
    return result


def tiled_boxes(backend, image: Image.Image) -> list[dict]:
    result = []
    for left, top, right, bottom in native_tiles(*image.size):
        _, values = backend.extract_text_with_boxes(image.crop((left, top, right, bottom)))
        for value in values:
            result.append({**value,
                           "left": float(value["left"]) + left,
                           "right": float(value["right"]) + left,
                           "top": float(value["top"]) + top,
                           "bottom": float(value["bottom"]) + top})
    return dedupe(result)


def numeric_rows(boxes: list[dict], region: tuple[float, float, float, float],
                 width: int, height: int) -> list[str]:
    left, top = int(width * region[0]), int(height * region[1])
    right, bottom = int(width * region[2]), int(height * region[3])
    rows = []
    for box in boxes:
        center_x = (float(box["left"]) + float(box["right"])) / 2
        center_y = (float(box["top"]) + float(box["bottom"])) / 2
        if left <= center_x <= right and top <= center_y <= bottom:
            for token in NUMBER.findall(str(box.get("text") or "")):
                rows.append((round(float(box["top"]), 1), round(float(box["left"]), 1), token.replace(",", ".")))
    rows.sort()
    return [token for _, _, token in rows]


def equipment_region(analysis: dict, override: tuple[float, float, float, float] | None):
    """Use the same equipment-fact evidence as the plugin, never a fixed crop."""
    if override is not None:
        return override
    facts = (analysis.get("screen_facts") or {}).get("facts") or []
    boxes = []
    for fact in facts:
        if fact.get("kind") not in {"equipment_level", "main_stat", "sub_stat"}:
            continue
        for evidence in fact.get("evidence") or []:
            region = evidence.get("region") if isinstance(evidence, dict) else None
            if isinstance(region, dict) and all(key in region for key in ("left", "top", "right", "bottom")):
                boxes.append((float(region["left"]), float(region["top"]),
                              float(region["right"]), float(region["bottom"])))
    if not boxes:
        return None
    left = max(0.0, min(box[0] for box in boxes) - .01)
    top = max(0.0, min(box[1] for box in boxes) - .01)
    right = min(1.0, max(box[2] for box in boxes) + .01)
    bottom = min(1.0, max(box[3] for box in boxes) + .01)
    return (left, top, right, bottom) if left < right and top < bottom else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5, help="measured samples per item (default 5)")
    parser.add_argument("--assistant-url", default="http://127.0.0.1:8219")
    parser.add_argument("--small-region", default="auto",
                        help="auto derives the relic stat column from the same frame; otherwise left,top,right,bottom fractions")
    parser.add_argument("--show-stats", action="store_true",
                        help="print only the numeric tokens from both modes so the user can check the screen")
    parser.add_argument("--wait-game", type=float, default=60.0)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("samples must be between 1 and 10")
    override = None
    if args.small_region != "auto":
        try:
            override = parse_region(args.small_region)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))

    root = Path(__file__).resolve().parents[1]
    catalog = ExternalKnowledgeCatalog(
        registry=json.loads((root / "resources/character_registry.json").read_text(encoding="utf-8")))
    avatars = AvatarTemplatePack(
        json.loads((root / "resources/avatar_templates.json").read_text(encoding="utf-8")),
        root=catalog.root, runtime=VisionMatchRuntime(root=catalog.root))
    adapter = LocalVisionAdapter(catalog, avatar_pack=avatars)
    client = VisionAssistantClient(args.assistant_url)
    helper = VisionAssistantProcess(root, base_url=args.assistant_url)

    def wait_connected() -> dict:
        state = client.health()
        if state.get("connected"):
            return state
        try:
            helper.start()
        except (OSError, RuntimeError) as exc:
            return {**state, "last_error": f"{type(exc).__name__}: {exc}"}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = client.health()
            if state.get("connected"):
                return state
            time.sleep(0.5)
        return state

    def fresh_frame(previous: int = 0):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            frame = client.fetch_frame(previous)
            if frame is not None:
                return frame
            time.sleep(0.2)
        raise RuntimeError("no fresh frame from the assistant")

    try:
        state = wait_connected()
        if not state.get("connected"):
            print(json.dumps({"ok": False, "reason": "assistant_unavailable",
                              "detail": state.get("last_error", "")}, ensure_ascii=False))
            return 2
        deadline = time.monotonic() + args.wait_game
        while time.monotonic() < deadline:
            game = client.health().get("game") or {}
            if game.get("running") and not game.get("minimized"):
                break
            time.sleep(1.0)
        else:
            print(json.dumps({"ok": False, "reason": "game_not_running_or_minimized",
                              "hint": "Open Honkai: Star Rail, keep it on a stable screen, then rerun."},
                             ensure_ascii=False))
            return 3

        backend = adapter._get_backend()  # model load is excluded from the samples
        warm = fresh_frame()
        warm_result = adapter.analyze_live(warm.image_bytes, cancelled=lambda: False, on_scene=lambda value: None)
        print(json.dumps({"kind": "warmup",
                          "page_type": (warm_result.get("page") or {}).get("page_type", "unknown")}), flush=True)

        captures, probes, full_passes = [], [], []
        times = {"whole": [], "tiled": []}
        small_regions: list[float] = []
        tiled_all, whole_all = Counter(), Counter()
        previous = 0
        tile_count = None
        region_printed = False
        missing_regions = 0
        for index in range(args.samples):
            started = time.perf_counter()
            frame = fresh_frame(previous)
            captures.append(time.perf_counter() - started)
            previous = frame.frame_seq
            image = open_image(frame.image_bytes)
            if tile_count is None:
                tile_count = len(native_tiles(*image.size))
                print(json.dumps({"kind": "precondition", "frame_size": [image.width, image.height],
                                  "tile_count": tile_count,
                                  "assistant": client.health().get("service_version", "")}), flush=True)

            started = time.perf_counter()
            adapter.probe_scene(frame.image_bytes, cancelled=lambda: False)
            probes.append(time.perf_counter() - started)

            started = time.perf_counter()
            full = adapter.analyze_live(frame.image_bytes, cancelled=lambda: False, on_scene=lambda value: None)
            full_passes.append(time.perf_counter() - started)
            region = equipment_region(full, override)
            if region is not None and not region_printed:
                box = pixel_box(image, region)
                print(json.dumps({"kind": "region", "sample": index,
                                  "source": "override" if override is not None else "landmark",
                                  "small_region_pixels": list(box),
                                  "small_region_size": [box[2] - box[0], box[3] - box[1]],
                                  "small_area_fraction": round((region[2] - region[0]) * (region[3] - region[1]), 3)},
                                 ensure_ascii=False), flush=True)
                region_printed = True

            # Alternate order so neither mode always pays the first call.
            boxes_by_mode = {}
            for mode in (("whole", "tiled") if index % 2 == 0 else ("tiled", "whole")):
                started = time.perf_counter()
                if mode == "whole":
                    _, boxes = backend.extract_text_with_boxes(image)
                else:
                    boxes = tiled_boxes(backend, image)
                times[mode].append(time.perf_counter() - started)
                boxes_by_mode[mode] = boxes

            if region is None:
                missing_regions += 1
                if args.show_stats:
                    print(json.dumps({"kind": "region_unavailable", "sample": index}, ensure_ascii=False), flush=True)
                continue

            tiled_tokens = numeric_rows(boxes_by_mode["tiled"], region, image.width, image.height)
            whole_tokens = numeric_rows(boxes_by_mode["whole"], region, image.width, image.height)
            tiled_all.update(tiled_tokens)
            whole_all.update(whole_tokens)
            small_crop = image.crop(pixel_box(image, region))
            started = time.perf_counter()
            backend.extract_text_with_boxes(small_crop)
            small_regions.append(time.perf_counter() - started)
            if args.show_stats:
                print(json.dumps({"kind": "stats", "sample": index,
                                  "tiled": tiled_tokens, "whole": whole_tokens}, ensure_ascii=False), flush=True)

        matched = sum((tiled_all & whole_all).values())
        tiled_seconds, whole_seconds = summary(times["tiled"]), summary(times["whole"])
        small_seconds = summary(small_regions) if small_regions else None
        print(json.dumps({
            "ok": True, "samples": args.samples, "tile_count": tile_count,
            "capture_seconds": summary(captures),
            "page_probe_seconds": summary(probes),
            "full_screen_analyze_seconds": summary(full_passes),
            "whole_frame_ocr_seconds": whole_seconds,
            "tiled_ocr_seconds": tiled_seconds,
            "small_region_ocr_seconds": small_seconds,
            "region_samples": len(small_regions), "missing_regions": missing_regions,
            "tiled_average_per_call_seconds": round(tiled_seconds["avg"] / tile_count, 3),
            "whole_pass_blocks_in_8s": round(8.0 / whole_seconds["avg"], 1),
            "tiled_pass_blocks_in_8s": round(8.0 / tiled_seconds["avg"], 1),
            "small_region_blocks_in_8s": round(8.0 / small_seconds["avg"], 1) if small_seconds else None,
            "numeric_agreement": {
                "tiled_total": sum(tiled_all.values()), "whole_total": sum(whole_all.values()),
                "matched": matched, "missing_from_whole": sum(tiled_all.values()) - matched,
                "extra_in_whole": sum(whole_all.values()) - matched} if tiled_all or whole_all else None,
        }, ensure_ascii=False), flush=True)
        return 0
    finally:
        adapter.close()
        helper.stop()


if __name__ == "__main__":
    raise SystemExit(main())
