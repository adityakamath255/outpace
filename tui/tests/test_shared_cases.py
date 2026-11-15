import subprocess
import sys
from pathlib import Path


def test_the_shared_cases_match_this_model():
    # a separate process, since the generator sets its process's time zone
    check = subprocess.run(
        [sys.executable, "-m", "tests.shared_cases", "--check"],
        cwd=Path(__file__).parents[1],
    )
    assert check.returncode == 0, (
        "spec/cases.json is stale; run `python -m tests.shared_cases` in tui/"
    )
