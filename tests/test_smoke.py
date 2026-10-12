import re

import shafa


def test_version_is_semver_string():
    assert isinstance(shafa.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", shafa.__version__)
