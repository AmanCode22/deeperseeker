import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import plugin_helper  # noqa: E402


# prompt masking ships enabled and scrambles every outgoing prompt, which turns
# any test asserting exact prompt text into a test asserting noise
@pytest.fixture(autouse=True)
def masking_disabled():
    plugin_helper._current_mask_config["master_enabled"] = False
