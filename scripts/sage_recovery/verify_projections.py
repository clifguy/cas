from .config import settings
from .recovery_checks import atomic, originals, require, utc


def main(run_id):
    import hashlib

    from . import client as h

    root = settings().root
    expected, ledger = originals()
    rows = []
    for s in expected:
        i = next((x["document_id"] for x in ledger if x["filename"] == s["filename"]))
        r = h.request(
            f"/sage_vaults/{settings().vault_id}/documents/" + i + "/projection", timeout=120
        )
        require(r["status"] == 200, (s["filename"], r["status"]))
        text = r["body"]["projection_text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        require(
            digest == s["expected_projection_sha256"], (s["filename"], "projection digest differs")
        )
        checks = []
        for sample in s["samples"]:
            require(sample["identity"] in text, "Source validation failed")
            r = h.request(
                f"/sage_vaults/{settings().vault_id}/discover",
                "POST",
                {
                    "mode": "keyword",
                    "query": '"' + sample["identity"] + '"',
                    "limit": 1,
                    "response_mode": "light",
                    "filters": {"document_ids": [i]},
                },
                timeout=12,
            )
            require(r["status"] == 200, "Source validation failed")
            matches = r["body"]["results"]
            require(
                matches and matches[0]["document"]["id"] == i,
                (s["filename"], sample["row"], "sample not searchable"),
            )
            checks.append(
                {
                    "row": sample["row"],
                    "present_in_projection": True,
                    "search_found_correct_document": True,
                    "search_seconds": r["seconds"],
                }
            )
        r = h.request(
            f"/sage_vaults/{settings().vault_id}/discover",
            "POST",
            {
                "mode": "semantic",
                "query": "Unified audit log creation user operations AuditData",
                "limit": 1,
                "response_mode": "light",
                "filters": {"document_ids": [i]},
            },
            timeout=12,
        )
        require(
            r["status"] == 200
            and r["body"]["results"]
            and (r["body"]["results"][0]["document"]["id"] == i),
            (s["filename"], "semantic retrieval failed"),
        )
        checks.append(
            {"semantic_retrieval_found_correct_document": True, "search_seconds": r["seconds"]}
        )
        d = h.request(f"/sage_vaults/{settings().vault_id}/documents/" + i, timeout=20)
        require(d["status"] == 200, "Source validation failed")
        d = d["body"]
        require(
            d["pipeline_status"] == "abstraction_complete" and (not d.get("pipeline_error")),
            (s["filename"], d["pipeline_status"], d.get("pipeline_error")),
        )
        rows.append(
            {
                "filename": s["filename"],
                "document_id": i,
                "pipeline_status": d["pipeline_status"],
                "pipeline_error": d.get("pipeline_error"),
                "projection_sha256": digest,
                "projection_characters": len(text),
                "projection_matches_original": True,
                "early_middle_late": checks,
            }
        )
        print(
            s["filename"],
            "complete; exact projection and three sampled searches verified",
            flush=True,
        )
    atomic(
        root / "six-projection-search.json",
        {
            "at": utc().isoformat(),
            "manifest_sha256": settings().manifest_sha256,
            "records": rows,
            "complete": True,
            "run_id": run_id,
        },
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    main(parser.parse_args().run_id)
