# API smoke sweep (TEMPORARY — never merge this branch)

Live-exercises every SAGE API endpoint and MCP tool that can produce a
database interaction (PostgreSQL and/or pgvector), against an isolated
local stack or a cloud deployment. Exists to validate the retirement of
the embedded storage backend; retired (branch deleted, database dropped)
once both runs are green.

Structural guarantee: the driver enumerates the OpenAPI spec plus live
`tools/list` on both MCP mounts and **fails** if any operation lacks a
check or a written justification — coverage is enforced, not hand-counted.
Probe self-tests run before ingest and must FAIL; a passing probe aborts
the sweep (the assertion machinery can no longer detect that breakage).

## Local run

```sh
smoke/local_stack.sh up          # createdb sage_smoke + bootstrap + SAGE :8765
.venv/bin/python smoke/driver.py \
    --profile local \
    --sage-url http://127.0.0.1:8765 \
    --storage-root ~/.cache/cas-smoke/vault-sources \
    --expected-build '<git short hash of the deployed HEAD>' \
    --report smoke/reports/local-$(date +%Y%m%d).md
smoke/local_stack.sh down        # kill server, dropdb, remove scratch
```

Notes:
- Run the driver with `PYTHONPATH` set to this checkout (the stack script
  does this for the server side automatically).
- `--storage-root` must be a server-visible scratch directory **outside**
  `~/sage_vaults/`; the driver creates the smoke vault under it via the
  API and stages the retrieval-health assertions file there post-ingest.
- The dedicated server loads its own embedding + abstraction models
  (~5 GB with the committed default); do not run while a 30B abstracter
  is resident elsewhere.
- First ingest includes model load; the pipeline poll allows 15 minutes.

## Cloud run (from the operator laptop)

```sh
.venv/bin/python smoke/driver.py \
    --profile cloud \
    --sage-url https://<sage-edge-host> \
    --bff-url https://<bff-host> \
    --token-cmd 'az account get-access-token --resource <sage-app-id-uri> --query accessToken -o tsv' \
    --expected-build '<deployed hash>' \
    --report smoke/reports/cloud-$(date +%Y%m%d).md
```

Profile differences are asserted, not skipped, wherever the contract
itself differs by deployment: path-based ingest routes give way to the
multipart `documents:batch` upload surface, supersession runs through the
`lifecycles` `supersede` action, `download-url`/`open`/eval-retrieval/BFF
co-location assert their documented per-profile status codes, and the
auth login challenge asserts the real identity-provider redirect.

Cloud teardown caveat: there is no delete-vault API. The smoke vault (and
the aux vault the admin-create check makes locally; the cloud run avoids
creating one) must be disposed of out-of-band — see the run report.

## Layout

- `driver.py` — CLI, probe self-tests, coverage gate, report writer
- `checks.py` — ordered scenario check catalog (groups 0–10)
- `client.py` — REST / SSE / MCP JSON-RPC client + recorder
- `vault_config_template.yaml` — smoke vault config (Tier-2 covers
  inference enabled; retrieval-health assertions declared)
- `fixtures/` — seven synthetic markdown documents; the shared `SPC-001`
  code drives Tier-2 staging-edge inference, the beta pair drives the
  supersession chain
- `local_stack.sh` — up/down/status for the isolated local stack
- `reports/` — coverage-matrix run artifacts (gitignored content is fine
  to keep locally; commit the final matrices before retirement)
