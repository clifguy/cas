"""Drive the shipped retained-source and projection verifiers with synthetic evidence."""

import dataclasses
import hashlib
import io
import json
from unittest.mock import patch

import pytest

from scripts.sage_recovery import (
    client,
    config,
    recovery_checks,
    verify_projections,
    verify_sources,
)


@pytest.fixture
def source_world(tmp_path, recovery_settings):
    manifest, ledger, documents = [], [], {}
    for n, identity in enumerate(recovery_settings.six_ids):
        text = f"early_{n}\nmiddle_{n}\nlate_{n}\n"
        digest = hashlib.sha256(text.encode()).hexdigest()
        metadata = dict(
            title=f"Synthetic {n}",
            source_type="structured_data",
            source_path=f"synthetic_{n}.csv",
            doc_type="synthetic_report",
            metadata_confirmed=True,
            lifecycle_status="active",
            pipeline_status="abstraction_complete",
            pipeline_error=None,
        )
        documents[identity] = {
            **metadata,
            "id": identity,
            "source_content_hash": "sha256:" + digest,
            "stored_content_hash": "sha256:" + digest,
        }
        manifest.append(
            dict(
                filename=f"synthetic_{n}.csv",
                sha256=digest,
                bytes=len(text.encode()),
                expected_projection_sha256=digest,
                samples=[
                    {"identity": token, "row": index}
                    for index, token in enumerate(text.splitlines())
                ],
            )
        )
        ledger.append(dict(filename=f"synthetic_{n}.csv", document_id=identity, metadata=metadata))
    for name, value in [("manifest", manifest), ("ledger", ledger)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    cfg = dataclasses.replace(
        recovery_settings,
        manifest=tmp_path / "manifest.json",
        ledger=tmp_path / "ledger.json",
        manifest_sha256=hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest(),
        ledger_sha256=hashlib.sha256((tmp_path / "ledger.json").read_bytes()).hexdigest(),
    )
    config.configure(cfg)

    def request(path, method="GET", body=None, timeout=30):
        if path.endswith("/chain"):
            return {
                "status": 200,
                "body": dict(is_linear=True, head_id=body["document_id"], total_length=1),
                "seconds": 0.1,
            }
        if path.endswith("/discover"):
            ids = body.get("filters", {}).get("document_ids")
            results = [{"document": documents[i]} for i in (ids or documents)]
            return {
                "status": 200,
                "body": {"results": results, "total_available": len(results)},
                "seconds": 0.1,
            }
        identity = path.split("/")[-2] if path.endswith("/projection") else path.rsplit("/", 1)[-1]
        if path.endswith("/projection"):
            n = cfg.six_ids.index(identity)
            return {
                "status": 200,
                "body": {"projection_text": f"early_{n}\nmiddle_{n}\nlate_{n}\n"},
                "seconds": 0.1,
            }
        return {"status": 200, "body": documents[identity], "seconds": 0.1}

    def opener(*args):
        class Opener:
            def open(self, req, timeout=120):
                identity = req.full_url.split("/")[-2]
                n = cfg.six_ids.index(identity)
                stream = io.BytesIO(f"early_{n}\nmiddle_{n}\nlate_{n}\n".encode())
                stream.status = 200
                stream.headers = {"Content-Length": str(len(stream.getvalue()))}
                return stream

        return Opener()

    with (
        patch.object(client, "request", side_effect=request),
        patch.object(client, "token", return_value="fake-only"),
        patch("urllib.request.build_opener", side_effect=opener),
    ):
        yield cfg, documents, request


def test_both_verifiers_publish_complete_current_run_evidence(source_world):
    cfg, _, _ = source_world
    verify_sources.main("synthetic-run")
    verify_projections.main("synthetic-run")
    for name in ["six-source-integrity.json", "six-projection-search.json"]:
        result = json.loads((cfg.root / name).read_text())
        assert result["complete"] is True
        assert result["run_id"] == "synthetic-run"
        assert {row["document_id"] for row in result["records"]} == set(cfg.six_ids)
        assert len(result["records"]) == 6
        assert (cfg.root / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "field,value",
    [
        ("stored_content_hash", "sha256:" + "0" * 64),
        ("created_by", "changed"),
        ("lifecycle_status", "archived"),
    ],
)
def test_source_corruption_never_publishes_completion(source_world, field, value):
    cfg, documents, _ = source_world
    documents[cfg.six_ids[0]][field] = value
    with pytest.raises(recovery_checks.Unsafe):
        verify_sources.main("synthetic-run")
    assert not (cfg.root / "six-source-integrity.json").exists()


@pytest.mark.parametrize("failure", ["wrong_hash", "missing_hit", "interrupted"])
def test_projection_failure_never_publishes_completion(source_world, failure):
    cfg, documents, original = source_world

    def request(path, method="GET", body=None, timeout=30):
        response = original(path, method, body, timeout)
        if failure == "wrong_hash" and path.endswith("/projection"):
            response["body"]["projection_text"] = "changed"
        if failure == "missing_hit" and path.endswith("/discover") and body["mode"] == "keyword":
            response["body"]["results"] = []
        return response

    if failure == "interrupted":
        documents[cfg.six_ids[0]]["pipeline_status"] = "abstraction_interrupted"
    with (
        patch.object(client, "request", side_effect=request),
        pytest.raises(recovery_checks.Unsafe),
    ):
        verify_projections.main("synthetic-run")
    assert not (cfg.root / "six-projection-search.json").exists()
