"""Unit tests for scripts/release-stamp-version.py.

Imports the module under a stable alias and exercises its tag normalizer
against the contract advertised in its module docstring. If CI ever
pushes a tag the release workflow can't normalize, these tests fail
loudly before the wheel build does.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "release-stamp-version.py"


@pytest.fixture(scope="module")
def stamper():
    """Load scripts/release-stamp-version.py as an importable module.

    Path has a hyphen, so the standard `import` syntax won't work — use
    importlib to pull it in under a clean name.
    """
    spec = importlib.util.spec_from_file_location("release_stamp_version", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["release_stamp_version"] = module
    spec.loader.exec_module(module)
    return module


class TestTagNormalizer:
    """Locks the PEP 440 normalizations advertised in the script docstring."""

    def test_plain_release_tag(self, stamper):
        assert stamper.normalize_tag("v0.10.0") == "0.10.0"

    def test_dotted_dev_tag(self, stamper):
        assert stamper.normalize_tag("v0.10.0.dev1") == "0.10.0.dev1"

    def test_dashed_dev_tag_is_normalized(self, stamper):
        # The smoke-test tag shape we pushed for PR #6 verification.
        assert stamper.normalize_tag("v0.10.0-dev.1") == "0.10.0.dev1"

    def test_rc_tag_loses_its_dot(self, stamper):
        # PEP 440 wants `0.10.0rc1`, not `0.10.0.rc1`.
        assert stamper.normalize_tag("v0.10.0.rc1") == "0.10.0rc1"
        assert stamper.normalize_tag("v0.10.0-rc.2") == "0.10.0rc2"

    def test_alpha_and_beta_are_single_letter(self, stamper):
        assert stamper.normalize_tag("v0.10.0-alpha.2") == "0.10.0a2"
        assert stamper.normalize_tag("v0.10.0-beta.3") == "0.10.0b3"
        assert stamper.normalize_tag("v0.10.0.a1") == "0.10.0a1"
        assert stamper.normalize_tag("v0.10.0.b2") == "0.10.0b2"

    def test_post_release(self, stamper):
        assert stamper.normalize_tag("v0.10.0.post1") == "0.10.0.post1"
        assert stamper.normalize_tag("v0.10.0-post.2") == "0.10.0.post2"

    def test_leading_v_is_optional(self, stamper):
        # Guard against a workflow caller that forgot the `v` prefix.
        assert stamper.normalize_tag("0.10.0") == "0.10.0"

    @pytest.mark.parametrize(
        "bad_tag",
        [
            "vGARBAGE",
            "v1.2",                 # missing patch
            "v0.10",                # missing patch
            "v0.10.0.rc",           # missing number
            "v0.10.0-xyz.1",        # unknown pre-release kind
            "v0.10.0dev1",          # missing separator
            "random-branch-name",
        ],
    )
    def test_malformed_tags_fail_loudly(self, stamper, bad_tag):
        with pytest.raises(SystemExit) as exc_info:
            stamper.normalize_tag(bad_tag)
        # The error message is scraped by GitHub Actions' `::error::` line
        # prefix, so make sure that string is present.
        assert "::error::" in str(exc_info.value)
        assert bad_tag in str(exc_info.value)
