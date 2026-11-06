"""Windows handling for the Kit tests (cross-platform plan W9/W15). WRITTEN, NOT RUN.

Kits are Linux images (X12): their entrypoints and ``kits/_common/kitrt.py`` only ever run inside a Linux container, so a
few tests assert POSIX behaviour of that in-container code. Those tests are skipped on Windows, by exact name, with the
reason below. Everything that is operator tooling (``scripts/kit_ci.py`` gates, manifests, schemas, Dockerfile lint,
smoke specs) is NOT skipped and must pass on Windows.

To see what a Windows run skips: ``pytest kits/tests -rs``. Adding a name here needs a one-line justification.
"""

from __future__ import annotations

import sys

import pytest

# name -> why it only makes sense inside a Linux container
POSIX_CONTAINER_ONLY = {
    "test_commit_marker_roundtrip_and_tamper": "kitrt.commit stores os.path.relpath keys; inside the Kit container that is always '/'",
    "test_scientific_sanitize_results_and_tail": "needs os.symlink to absolute POSIX paths (needs a privilege on Windows) and '/' separators from the container",
}


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if sys.platform != "win32":
        return
    for item in items:
        why = POSIX_CONTAINER_ONLY.get(item.originalname or item.name)
        if why:
            item.add_marker(pytest.mark.skip(reason="Windows: in-container POSIX behaviour (X12): " + why))
