"""Unsupported YAML native types retain authored, deterministic lexical evidence."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from sage.source_adapters.structure import FactLimits, source_facts

YAML_NATIVE_CASES = {
    "set": "root:\n  members: !!set\n    alpha: null\n    beta: null\n    gamma: null\n",
    "omap": "root:\n  ordered: !!omap\n    - first: alpha\n    - second: beta\n",
    "pairs": "root:\n  pairs: !!pairs\n    - first: alpha\n    - first: beta\n",
    "binary": "root:\n  blob: !!binary |\n    AAECAw==\n",
}

_REAL_INSPECTION = """
import asyncio,json,sys
from pathlib import Path
from sage.services.ingestion import _run_inspected_projection
from sage.source_adapters.structured_data_adapter import StructuredDataAdapter
async def run():
    projection = await _run_inspected_projection(StructuredDataAdapter(), Path(sys.argv[1]), {})
    evidence = projection.discovery_evidence
    print(json.dumps({'selected': [x.input_text for x in evidence.selected],
                      'renderings': [x.rendering for x in evidence.selected],
                      'inspection_complete': evidence.inspection_complete,
                      'limitations': evidence.limitations,
                      'identity': evidence.identity.processing_identity().model_dump(mode='json'),
                      'projection': projection.text}, sort_keys=True))
asyncio.run(run())
"""


@pytest.mark.parametrize("kind", list(YAML_NATIVE_CASES))
def test_native_yaml_fallback_real_inspection_is_cross_seed_faithful(tmp_path, kind):
    text = YAML_NATIVE_CASES[kind]
    source = tmp_path / "native.yaml"
    source.write_text(text)
    root = Path(__file__).resolve().parents[2]
    results = []
    for seed in ("1", "2"):
        run = subprocess.run(
            [str(root / ".venv/bin/python"), "-c", _REAL_INSPECTION, str(source)],
            cwd=root,
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        results.append(json.loads(run.stdout))
    assert results[0] == results[1]
    for result in results:
        assert result["inspection_complete"] is False
        assert any("unresolved" in value for value in result["limitations"])
        assert result["selected"] == text.splitlines()
        assert result["renderings"] == text.splitlines()


@pytest.mark.parametrize("kind", list(YAML_NATIVE_CASES))
def test_nested_native_yaml_values_remain_lexical_not_python_representations(tmp_path, kind):
    text = YAML_NATIVE_CASES[kind]
    source = tmp_path / "native.yaml"
    source.write_text(text)
    stream = source_facts(source, None, FactLimits())
    facts = list(stream)
    assert [fact.value for fact in facts] == text.splitlines()
    assert all(type(fact.value) is str for fact in facts)
    assert [fact.rendering for fact in facts] == text.splitlines()
    assert stream.exhausted
    assert any("unresolved" in value for value in stream.limitations)


@pytest.mark.parametrize("kind", ["alias", "set"])
def test_yaml_lexical_fallback_is_incremental_and_bounded(tmp_path, monkeypatch, kind):
    prefix = "a: &a [hello]\nb: *a\n" if kind == "alias" else "members: !!set\n  alpha: null\n"
    text = prefix + "# authored comment\n" * 1000
    source = tmp_path / "native.yaml"
    source.write_text(text)

    class NoWholeLineList(str):
        def splitlines(self, *args, **kwargs):
            pytest.fail("lexical fallback allocated a whole-source line list")

    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            NoWholeLineList(text) if path == source else original(path, *args, **kwargs)
        ),
    )
    stream = source_facts(source, None, FactLimits(max_parse_nodes=30))
    facts = list(stream)
    assert len(facts) == 30
    assert facts[0].value == prefix.splitlines()[0]
    assert facts[-1].value == facts[-1].rendering == "# authored comment"
    assert facts[-1].location == "line:30"
    assert not stream.exhausted
    assert any("line limit" in item for item in stream.limitations)
