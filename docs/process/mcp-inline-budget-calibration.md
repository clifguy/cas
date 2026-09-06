# Calibrating the MCP inline budget

`DEFAULT_MCP_INLINE_BUDGET_BYTES` in `sage/services/retrieval.py` is the size
above which SAGE warns a caller that a response will not be delivered inline.
Two hints depend on it — the catalog `response_exceeds_inline_budget` and the
facets `facets_response_exceeds_inline_budget` — and both fit their
recommendation to it with no margin of their own, so the constant is the whole
calibration.

The ceiling it approximates is a property of the **calling MCP client**, not of
SAGE. It cannot be measured from a test, because no test has a client; it is
measured by driving a live client and watching what it does. This document is
that procedure.

## 1. What is actually bounded

A client that will not deliver a result inline writes it to a file and hands
back a notice instead. Two distinct mechanisms were observed on Claude Code,
with different messages and different units:

| Mechanism | Message | Denominated in |
|---|---|---|
| Output spill | `Output too large (NN.NKB). Full output saved to: …` | bytes |
| Token ceiling | `result (N characters …) exceeds maximum allowed tokens` | tokens |

Whichever binds *first* is the one to calibrate against. On Claude Code the
output spill does: it fired at 50 KB while the token message appeared only past
80 KB. Re-check this when recalibrating — a client that raises one and not the
other changes which axis matters.

The bound is on the **tool result's own text**, not on the JSON-escaped envelope
the client wraps it in. That distinction is worth confirming rather than
assuming, because the two differ by about 11% and land on different numbers;
§4 shows how to tell them apart. It matters because
`_serialized_response_bytes` measures the text — so if the bound were on the
envelope, the constant would have to carry an escaping-expansion allowance, and
a content-dependent one at that.

Note also that the client counts **characters** where SAGE counts **bytes**.
They coincide for ASCII and diverge upward for anything multibyte, so a byte
budget is the conservative side of that difference and needs no allowance.

## 2. Sizing a probe without paying for it

Use catalog-mode `search` against a vault with enough documents, and vary
`limit` (and `offset`, for finer steps than a whole row).

The exact size of any candidate call can be read **for free** from the REST
surface, because the budget hint reports it:

```bash
curl -s -X POST "http://127.0.0.1:8000/sage_vaults/cas/discover" \
  -H 'Content-Type: application/json' -d '{"mode":"catalog","limit":52,"offset":9}' \
  | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["hints"]["response_size_bytes"])'
```

That figure is `_serialized_response_bytes` — the response as measured *before*
its hint is merged. The delivered payload is larger by the serialized hint
itself: **155 bytes** on the catalog shape. Add it when comparing against a
client-reported size.

Do not size probes by re-serializing the REST body directly: REST keeps `null`
fields that the MCP tool layer drops with `exclude_none=True`, which inflates
the estimate by roughly 20%.

## 3. The search

Probe costs are asymmetric, and the search should exploit that:

- A probe **over** the ceiling costs almost nothing — the client returns a short
  notice, and the notice reports the size.
- A probe **under** the ceiling delivers its whole payload into the session.

So bisect from above, and let the last, smallest probe be the one that succeeds.
Ten probes were enough to pin the ceiling to 23 bytes.

Record two numbers:

- `E_inline` — the largest delivered size that arrived inline.
- `E_spill` — the smallest that did not.

The ceiling lies in `(E_inline, E_spill]`. If exactly one round number sits in
that interval, that is the client's constant; if none does, or several do,
tighten the bracket with `offset` before concluding.

## 4. Distinguishing the text bound from the envelope bound

A spilled result is written to a file whose contents are the wrapped form:
a JSON array of `{"type": "text", "text": …}`. Both quantities come out of it.

```bash
.venv/bin/python - <<'PY'
import json
p = "<path the spill notice named>"
wrapped = open(p, "rb").read()
inner = json.loads(wrapped)[0]["text"].encode()
print("inner text bytes :", len(inner))
print("wrapped bytes    :", len(wrapped))
print("expansion factor : %.4f" % (len(wrapped) / len(inner)))
PY
```

Carry both brackets — inner and wrapped — through the search. The bound is on
whichever one lands on a round number; the other will not.

## 5. Deriving the constant

```
DEFAULT_MCP_INLINE_BUDGET_BYTES = ceiling - a stated margin
```

The margin is a judgement, not a measurement, and the comment on the constant
must say so. It covers two things: the hint's own bytes, which are merged after
the size is taken and so are absent from it, and the fact that a client other
than the reference one may sit somewhat lower.

`_MEASURED_INLINE_CEILING_BYTES` records the ceiling beside the budget, and
`test_inline_budget_stays_below_the_measured_ceiling` holds the relation between
them. Nothing else would notice a budget raised to or past its ceiling: every
hint would keep firing and every fixture would keep passing, and the only
symptom would be responses the hints called safe arriving by disk round-trip.

## 6. What a recalibration breaks

Fixtures that must cross the budget are sized *from* it, so they scale — but
they should be re-run and re-reasoned, not assumed:

- The wide-tag facets fixtures (`tests/sage/test_retrieval.py`,
  `tests/app/test_mcp_app_tools.py`) derive their tag width from the budget so
  fifty values still overrun it with room to spare rather than at the margin.
- The high-cardinality delivered-bytes fixture
  (`tests/app/test_mcp_app_tools.py`) derives its vocabulary from the budget.
  This one is mutation-verified and the verification must be repeated: it has to
  go **red** against a compact `json.dumps` measurement and **green** on the
  wide-tag shape. A rescale that leaves it green under that mutation has
  disarmed it.
- The synthetic facets payloads (`tests/sage/test_retrieval.py`) scale every
  hand-dimensioned width from a single recorded origin, so the proportion
  between heavy row and fixed part — which is what those tests are about —
  survives.
- The three tool docstrings in `sage/sage_api_tools.py` state the figure, and
  `tests/sage/test_mcp_self_documentation.py` pins it against the constant.

## 7. The measurement on record

| | |
|---|---|
| Client | Claude Code |
| Date | 2026-09-06 |
| Vault | `cas`, catalog mode |
| Largest inline | 49,977 delivered bytes (49,822 as measured, +155 hint) |
| Smallest spill | 50,193 delivered bytes |
| Inner bracket | (49,977, 50,193] → **50,000** |
| Wrapped bracket | (55,341, 55,583] → nothing round; not the bound |
| Envelope expansion | 1.1072–1.1075 across four samples |
| Ceiling adopted | 50,000 |
| Budget adopted | 45,000 (10% margin) |
