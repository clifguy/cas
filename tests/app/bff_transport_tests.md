# BFF → SAGE transport seam tests (CAS-ADR-042)

Covers the `SageTransport` port and its two profile-selected bindings
(`app/backend/transport.py`). Implemented in `tests/app/test_bff_transport.py`.

The seam is one authenticated request/response call. The in-process binding
dispatches against the co-located SAGE app (no socket, no bearer); the HTTP
binding reaches SAGE over the wire via the on-behalf-of client, attaching the
user's delegated bearer on every call. Per CAS-ADR-042's weakest-binding
constraint, the contract carries no guarantee a binding cannot honor.

| ID | Behavior | Anti-coincidental control |
|----|----------|---------------------------|
| TX-001 | The ABC rejects a subclass that does not implement `request`. | Drop `@abstractmethod` → the incomplete subclass instantiates. |
| TX-002 | Both real bindings are `SageTransport` instances. | — |
| TX-003 | The HTTP binding carries `Authorization: Bearer <token>`. | Drop the header / skip `acquire_sage_token` → no captured bearer. |
| TX-004 | When the session cannot mint a token, the HTTP binding raises and SAGE is never reached. | Issue the request before minting → an upstream call is recorded. |
| TX-004b | The HTTP binding refuses a call with no session before minting or reaching SAGE. | Skip the session check → `acquire_sage_token`/the mock is reached. |
| TX-005 | The in-process binding reaches the co-located app and carries no bearer. | Make it attach a bearer (collapse into the HTTP binding) → recorded auth is non-null. |
| TX-006 | Both bindings, pointed at the same SAGE, return the same status + body for a read. | Point one binding at a different app → bodies diverge. |
| TX-007 | Importing the module registers the seam for both profiles; `resolve_bff_transport` selects the named binding. | Register one profile / swap builders → wrong binding type. |
