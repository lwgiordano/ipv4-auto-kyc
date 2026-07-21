import re

from kyc_tool.domain.engine import ENGINE_BUILD_ID


def test_engine_build_id_is_nonblank_versioned():
    assert re.fullmatch(r"eng-[1-9]\d*", ENGINE_BUILD_ID), ENGINE_BUILD_ID
