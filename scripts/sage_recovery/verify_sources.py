from .config import settings
from .recovery_checks import atomic, originals, require, utc


def main(run_id):
    import hashlib
    import urllib.request

    from . import client as h

    root = settings().root
    expected, ledger = originals()
    rows = []
    for s in expected:
        i = next((x["document_id"] for x in ledger if x["filename"] == s["filename"]))
        d = h.request(f"/sage_vaults/{settings().vault_id}/documents/" + i, timeout=30)
        require(d["status"] == 200, "Source validation failed")
        d = d["body"]
        require(
            d["source_content_hash"] == d["stored_content_hash"] == "sha256:" + s["sha256"],
            "Source validation failed",
        )
        req = urllib.request.Request(  # noqa: S310 - config validates the HTTPS origin
            settings().base_url + f"/sage_vaults/{settings().vault_id}/documents/" + i + "/content",
            headers={"Authorization": "Bearer " + h.token()},
        )
        digest = hashlib.sha256()
        count = 0
        with urllib.request.build_opener(h.NoRedirect).open(req, timeout=120) as r:
            require(r.status == 200, "Source validation failed")
            length = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
            while b := r.read(1024 * 1024):
                digest.update(b)
                count += len(b)
        require(
            count == s["bytes"] and digest.hexdigest() == s["sha256"] and (length in (None, count)),
            "Source validation failed",
        )
        c = h.request(
            f"/sage_vaults/{settings().vault_id}/chain", "POST", {"document_id": i}, timeout=30
        )
        require(c["status"] == 200, "Source validation failed")
        c = c["body"]
        require(
            c["is_linear"] and c["head_id"] == i and (c["total_length"] == 1),
            "Source validation failed",
        )
        require(
            d["lifecycle_status"] == "active"
            and (d["source_type"] == "structured_data")
            and d["metadata_confirmed"],
            "Source validation failed",
        )
        prior = next((x for x in ledger if x["filename"] == s["filename"]))["metadata"]
        for k in [
            "title",
            "source_type",
            "source_path",
            "version_label",
            "project",
            "authority_scope",
            "doc_type",
            "document_date",
            "tags",
            "tier3_metadata",
            "created_by",
            "created_at",
            "created_client",
            "created_agent",
            "metadata_confirmed",
        ]:
            require(d.get(k) == prior.get(k), (s["filename"], k))
        row = {
            "filename": s["filename"],
            "document_id": i,
            "bytes": count,
            "sha256": digest.hexdigest(),
            "retained_bytes_verified": True,
            "metadata_and_provenance_preserved": True,
            "linear_single_record_chain": True,
            "pipeline_at_read": d["pipeline_status"],
        }
        rows.append(row)
        print(s["filename"], "retained bytes verified", flush=True)
    records = []
    offset = 0
    while True:
        r = h.request(
            f"/sage_vaults/{settings().vault_id}/discover",
            "POST",
            {
                "mode": "catalog",
                "limit": 100,
                "offset": offset,
                "response_mode": "full",
                "filters": {"source_type": "structured_data", "exclude_terminal_lifecycle": False},
            },
            timeout=30,
        )
        require(r["status"] == 200, "Source validation failed")
        b = r["body"]
        records.extend(b["results"])
        offset += len(b["results"])
        if offset >= b["total_available"]:
            break
        require(b["results"], "Source validation failed")
    require(len(records) == b["total_available"], "Source validation failed")
    for s in expected:
        matches = [
            x["document"]
            for x in records
            if x["document"].get("source_path", "").split("/")[-1] == s["filename"]
            or x["document"].get("title")
            == next((x for x in ledger if x["filename"] == s["filename"]))["metadata"]["title"]
        ]
        require(len(matches) == 1, (s["filename"], len(matches)))
        require(
            matches[0]["id"]
            == next((x["document_id"] for x in ledger if x["filename"] == s["filename"])),
            "Source validation failed",
        )
    atomic(root / "structured-catalog.json", records)
    atomic(
        root / "six-source-integrity.json",
        {
            "at": utc().isoformat(),
            "manifest_sha256": settings().manifest_sha256,
            "records": rows,
            "complete": True,
            "run_id": run_id,
        },
    )
    print(
        (
            "All six original retained byte streams, metadata/provenance and single-record "
            "lineages verified; structured catalog records:"
        ),
        len(records),
        flush=True,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    main(parser.parse_args().run_id)
