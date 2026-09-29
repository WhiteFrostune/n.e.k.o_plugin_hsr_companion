"""Privacy-bounded observation of the foreground Honkai: Star Rail window.

The observer deliberately captures only the detected game client while it is
the foreground window. Frames stay in memory and are never written to disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

HSR_PROCESS_NAMES = {"starrail.exe", "honkaistarrail.exe"}
HSR_TITLE_TOKENS = ("崩坏：星穹铁道", "崩坏: 星穹铁道", "Honkai: Star Rail")


@dataclass(frozen=True)
class GameWindow:
    hwnd: int
    title: str
    pid: int
    process_name: str
    left: int
    top: int
    width: int
    height: int
    minimized: bool
    foreground: bool

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("hwnd", None)
        return value


def is_hsr_window(*, title: str, process_name: str) -> bool:
    """Return True only for a known HSR process or an explicit game title."""

    normalized_process = str(process_name or "").strip().lower()
    normalized_title = str(title or "").strip().lower()
    return normalized_process in HSR_PROCESS_NAMES or any(
        token.lower() in normalized_title for token in HSR_TITLE_TOKENS
    )


def frame_signature(image: Any, *, width: int = 32, height: int = 18) -> bytes:
    """Build a tiny luminance signature used only for in-memory deduplication."""

    from PIL import Image

    resampling = getattr(Image, "Resampling", Image).BILINEAR
    return image.convert("L").resize((width, height), resampling).tobytes()


def frame_difference(previous: bytes | None, current: bytes) -> float:
    if not previous or len(previous) != len(current) or not current:
        return 1.0
    return sum(abs(a - b) for a, b in zip(previous, current)) / (255.0 * len(current))


def find_hsr_window() -> GameWindow | None:
    """Find the best visible top-level Star Rail window on Windows."""

    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return None

    candidates: list[tuple[int, GameWindow]] = []
    foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)

    def visit(hwnd: int, _: object) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = str(win32gui.GetWindowText(hwnd) or "").strip()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                process_name = str(psutil.Process(pid).name() or "")
            except (psutil.Error, OSError):
                process_name = ""
            if not is_hsr_window(title=title, process_name=process_name):
                return
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
            screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
            width = max(0, screen_right - screen_left)
            height = max(0, screen_bottom - screen_top)
            if width < 320 or height < 180:
                return
            minimized = bool(win32gui.IsIconic(hwnd))
            foreground = int(hwnd) == foreground_hwnd
            window = GameWindow(
                hwnd=int(hwnd),
                title=title,
                pid=int(pid),
                process_name=process_name,
                left=int(screen_left),
                top=int(screen_top),
                width=int(width),
                height=int(height),
                minimized=minimized,
                foreground=foreground,
            )
            score = (
                (100 if process_name.lower() in HSR_PROCESS_NAMES else 0)
                + (20 if foreground else 0)
                + min(width * height // 100_000, 10)
            )
            candidates.append((score, window))
        except Exception:
            return

    win32gui.EnumWindows(visit, None)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def capture_game_window(window: GameWindow) -> tuple[bytes, str, bytes]:
    """Capture a foreground game client and return JPEG bytes plus signature."""

    try:
        import win32gui
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - Windows release includes these
        raise RuntimeError("当前系统缺少游戏画面读取组件") from exc

    if window.minimized or int(win32gui.GetForegroundWindow() or 0) != window.hwnd:
        raise RuntimeError("星铁窗口当前不在前台")

    try:
        import mss

        with mss.mss() as screen:
            shot = screen.grab(
                {
                    "left": window.left,
                    "top": window.top,
                    "width": window.width,
                    "height": window.height,
                }
            )
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except ImportError:
        from PIL import ImageGrab

        image = ImageGrab.grab(
            bbox=(
                window.left,
                window.top,
                window.left + window.width,
                window.top + window.height,
            ),
            all_screens=True,
        ).convert("RGB")

    if int(win32gui.GetForegroundWindow() or 0) != window.hwnd:
        raise RuntimeError("读取过程中星铁窗口离开了前台")

    signature = frame_signature(image)
    max_edge = 1280
    if max(image.size) > max_edge:
        scale = max_edge / float(max(image.size))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            resampling,
        )
    for quality in (76, 68, 60, 52):
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        payload = output.getvalue()
        if len(payload) <= 220 * 1024:
            return payload, "image/jpeg", signature
    raise RuntimeError("当前画面过于复杂，暂时无法安全送入视觉上下文")


__all__ = [
    "GameWindow",
    "capture_game_window",
    "find_hsr_window",
    "frame_difference",
    "frame_signature",
    "is_hsr_window",
]
