"""Compatibility facade for native OCR helpers and game-specific policy."""

from .native_text import ObservationSuperseded as ObservationSuperseded
from .native_text import QueryPreempted as QueryPreempted
from .native_text import _box_value as _box_value
from .native_text import _deduplicate as _deduplicate
from .native_text import check_current as check_current
from .native_text import native_tiles as native_tiles
from .native_text import read_region as read_region
from .native_text import split_label_geometry as split_label_geometry
from .text_policy import read_native_text as read_native_text
from .text_verification import verify_name_regions as verify_name_regions
