"""Native-reader injection guard; legacy behavior remains in text_policy."""
import pytest
from plugin.plugins.hsr_companion.adapters.native_text import read_native_text


@pytest.mark.parametrize("missing", ("name_labels", "skip_name_reread", "name_verifier"))
def test_catalog_requires_all_policy_callbacks_before_ocr(missing):
    callbacks = {name: lambda *args: None for name in ("name_labels", "skip_name_reread", "name_verifier")}
    callbacks.pop(missing)
    with pytest.raises(ValueError, match="requires all three"):
        read_native_text(object(), object(), catalog=object(), **callbacks)
