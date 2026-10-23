"""Safety checks for the opt-in live-test runner; this module makes no calls."""

import os
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import scripts.run_small_sample as sample


def test_full_pipeline_requires_local_gemini_key_before_search():
    args = SimpleNamespace(full_pipeline=True, prompt=None)
    error = StringIO()
    with patch.dict(os.environ, {"OFFLINE": "0"}, clear=True):
        with patch.object(sample, "parse_args", return_value=args):
            with redirect_stderr(error):
                assert sample.main() == 2
    assert "GEMINI_API_KEY" in error.getvalue()
    assert "no searches or API calls were made" in error.getvalue()


def test_full_pipeline_refuses_offline_mode_before_search():
    args = SimpleNamespace(full_pipeline=True, prompt=None)
    error = StringIO()
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "OFFLINE": "1"}, clear=True):
        with patch.object(sample, "parse_args", return_value=args):
            with redirect_stderr(error):
                assert sample.main() == 2
    assert "OFFLINE=0" in error.getvalue()
    assert "no searches or API calls were made" in error.getvalue()


if __name__ == "__main__":
    test_full_pipeline_requires_local_gemini_key_before_search()
    test_full_pipeline_refuses_offline_mode_before_search()
    print("Small-sample live-run safety tests passed; no API or search calls were made.")
