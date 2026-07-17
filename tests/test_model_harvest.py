"""Spec 007 — observed model harvest.

`runs.model` is derived at ingest from the run's init system event (observed);
meta never populates the column. The wrapper records the `--model` argument as
the additive meta field `model_requested`, retrievable via the detail's
meta_json passthrough. See specs/007-model-harvest.md.

Scans are driven directly via the ingester; the wrapper is invoked in-process
with Popen short-circuited, mirroring test_labels.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agmon import runner


# ============================================================================
# Wrapper — --model becomes meta model_requested; meta model is gone
# ============================================================================


def _run_wrapper(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)

    def _boom(*a, **k):
        raise FileNotFoundError  # short-circuit before launching claude

    monkeypatch.setattr(runner.subprocess, "Popen", _boom)
    with pytest.raises(SystemExit):
        runner.main(argv)
    return json.loads(next(tmp_path.glob("*.meta.json")).read_text())


def test_wrapper_writes_model_requested_when_flag_passed(tmp_path, monkeypatch):
    meta = _run_wrapper(
        tmp_path, monkeypatch, ["hello", "--cwd", str(tmp_path), "--model", "opus"]
    )
    assert meta["model_requested"] == "opus"
    # Assert on the parsed dict, not the JSON text: meta["argv"] legitimately
    # contains the literal --model flag.
    assert "model" not in meta


def test_wrapper_omits_model_requested_without_flag(tmp_path, monkeypatch):
    meta = _run_wrapper(tmp_path, monkeypatch, ["hello", "--cwd", str(tmp_path)])
    assert "model_requested" not in meta
    assert "model" not in meta
