"""Release versioning has ONE source: pyproject.toml.

The release yaml builds images only on CI and tags them with the
[project] version from pyproject.toml (repo releases are vX.Y —
v1.0 ... v5.0 all match pyproject's major.minor string). Two ways to
drift, both guarded here:

1. pyproject.toml stops pinning a literal major.minor version -> the
   workflow cannot derive image tags (6.0 / 6 / latest) and any tag
   would publish a versionless :latest only.
2. The workflow regresses to deriving tags from the git tag pattern
   (type=semver vX.Y.Z) or hardcoding example versions -> the image
   version and the Python package version silently diverge.

Run:  python tests/test_release_version.py   (pytest-compatible)
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_pins_a_literal_major_minor_version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "(\d+\.\d+)"$', text, re.M)
    assert match, (
        "pyproject.toml [project] must pin a literal major.minor version "
        '(e.g. version = "6.0") — the release workflow derives the image '
        "tags from exactly this string"
    )


def test_release_workflow_reads_pyproject_and_validates_the_tag():
    yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "pyproject.toml" in yml, (
        "release.yml must read the version from pyproject.toml"
    )
    # The tag/version agreement check (v<pyproject-version> or bust).
    assert "TAG_VERSION" in yml and "GITHUB_REF_NAME" in yml, (
        "release.yml must refuse a tag that does not match the "
        "pyproject.toml version"
    )
    # Image tags come from the parsed version, not from git-tag patterns.
    assert "type=raw,value=${{ steps.ver.outputs.version }}" in yml, (
        "image version tag must be the pyproject.toml version"
    )
    assert "type=raw,value=${{ steps.ver.outputs.major }}" in yml, (
        "image major tag must be derived from the pyproject.toml version"
    )
    assert "type=raw,value=latest" in yml, ":latest must still be published"


def test_release_workflow_does_not_hardcode_versions():
    yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "type=semver" not in yml, (
        "type=semver keys tags off the git tag pattern (vX.Y.Z) instead of "
        "pyproject.toml — this repo releases as vX.Y, so the scheme diverges"
    )
    assert "v1.0.0" not in yml, "no hardcoded example versions in release.yml"


if __name__ == "__main__":
    test_pyproject_pins_a_literal_major_minor_version()
    test_release_workflow_reads_pyproject_and_validates_the_tag()
    test_release_workflow_does_not_hardcode_versions()
    print("ok: release versioning is single-sourced to pyproject.toml")
