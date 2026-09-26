#!/usr/bin/env python3
"""Compare the set of tests that actually run, before and after a change.

A change can shrink the regression surface without producing a single failing
signal. The suite stays green precisely because the lost tests no longer run,
and a line-coverage floor does not observe them either: the lines they
exercised are usually still touched by something else. Two shapes have been
observed in this repository, and they fail differently.

**A test stops being collected.** A module-level ``def`` at column 0 lands
inside a class body; every method written after it dedents out of the class,
reparents into a nested function, and pytest silently stops collecting it. The
file still parses and still imports. ``tests/test_collection_integrity.py`` is
the deterministic gate against that specific mechanism, walking the AST for a
``test_*`` function nested inside another function. It does not generalize: it
cannot see a deleted module, a class renamed off the ``Test*`` prefix, a
shrunken ``parametrize`` set, or the second shape below.

**A test stops being selected.** A class gains an opt-in ``skipif`` gate. The
tests remain perfectly collectable -- a local run with the opt-in set collects
and runs them normally -- but they stop running in the environment that had
been running them. Comparing collected counts is blind to this, because the
count does not move.

The measurement that unifies both is the set of tests that would *actually
execute in a given environment*: the first shape removes ids from collection,
the second moves ids from active to skipped, and both drop out of that set.
This check emits that set for one tree and compares two of them.

Three properties keep it usable rather than merely correct:

* **Sets, not counts.** A change that adds one test while silently losing
  another leaves the count flat. The observed instance of the first shape did
  exactly that -- it added two stub functions in the same edit that disabled
  fourteen methods.
* **A relocated test is not a lost test.** Before reporting a loss, the leaf
  key (everything after the last ``.py::``) is counted across the new active
  set. Without this escape, every file rename reads as a mass contraction,
  which is how a gate like this gets switched off in a week. It has to be a
  count rather than a presence check: method names repeat across parallel
  modules, so a rule asking only whether the name survives somewhere lets a
  deleted module hide behind its namesakes.
* **An empty measurement fails.** A base side that collected nothing is a
  broken comparison, not a clean one, and is reported as such rather than
  passing.

Deliberate removals are declared in ``KNOWN_TEST_REMOVALS`` below, which puts
them in the diff where a reviewer sees them.

The comparison is only as good as the environment it runs in: a check run
where the opt-in variables differ from the suite whose coverage is at stake
measures the wrong thing, which is the second shape recurring one level up.
Both sides must be emitted under the same environment as that suite.

Usage::

    python -m scripts.check_test_surface --emit --out head.json
    python -m scripts.check_test_surface --emit --target tests/sage --out part.json
    python -m scripts.check_test_surface --compare base.json head.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final, TypedDict

# Evaluating a ``skipif`` mark without running the test is what separates this
# check from a collected-count comparison, and pytest exposes it only on a
# private module. The dependency is deliberate and pinned by the lockfile, so
# an upgrade that moves it is always a deliberate act; the accompanying test
# named for this import turns that move into a pointed failure rather than a
# silent degradation to collection-only.
try:
    from _pytest.skipping import evaluate_skip_marks
except ImportError as exc:  # pragma: no cover - exercised by the named guard test
    raise ImportError(
        "cannot import evaluate_skip_marks from _pytest.skipping; without it this "
        "check degrades to a collected-count comparison, which cannot see a test "
        "that stopped running in this environment while remaining collectable"
    ) from exc

# Minimum active tests the base side must carry for the comparison to mean
# anything. A base that measured nothing reports no contraction, which is the
# worst shape of false green: the gate goes quiet exactly when it broke. The
# floor is an order of magnitude below the observed size of this suite (~7,000
# collected), so it catches a failed or misdirected measurement without
# tracking the suite's real growth.
MIN_BASE_ACTIVE: Final[int] = 500

# Maximum number of entries to enumerate in a single report section.
_MAX_REPORTED: Final[int] = 30


class Surface(TypedDict):
    """One revision's measurement, as it crosses the boundary between the two
    steps that produce and consume it.

    Written as JSON by ``--emit`` and read back by ``--compare``, in the
    general case from a different checkout, so this is a file format rather
    than an in-process structure and is named accordingly.
    """

    target: str
    exit_code: int
    collected: list[str]
    active: list[str]


# ---------------------------------------------------------------------------
# Declared removals
#
# node id (exact) or prefix ending in ``::`` or ``.py`` -> one-line reason for
# the tests no longer running. Empty by default: a shrinking regression
# surface is almost always accidental, and the few deliberate cases should be
# visible in the diff that causes them.
#
# Entries go inert on their own. Once the removal lands on the default branch
# it is absent from the base side too, so the entry never matches again;
# pruning a landed entry is housekeeping, not correctness.
#
# Reasons describe what changed in the product, not which piece of work
# changed it -- this is a durable code surface, where CAS-ADR-NNN is the only
# sanctioned anchor.
# ---------------------------------------------------------------------------

# The Core API interfaces the frontend type conformance gate enrolled when its ids
# carried no specification, for the declaration below.
_CONFORMANCE_INTERFACES: Final[str] = (
    "AdapterInfo BatchDocumentsCreated BatchIngestFileError BatchIngestFileMetadata "
    "BatchIngestUploadMetadata BatchProgressEvent BatchSummaryEvent BulkLifecycleItem "
    "BulkLifecycleItemResult BulkLifecycleRequest BulkLifecycleResponse BulkLinkItem "
    "BulkLinkItemResult BulkLinkRequest BulkLinkResponse BulkMetadataItem BulkMetadataItemResult "
    "BulkMetadataRequest BulkMetadataResponse ChainEntry ChainResponse CreateVaultRequest "
    "DiscoverHit DiscoverRequest DiscoverResponse DocTypeEntry DocTypeRequirements Document "
    "DocumentDownloadUrlResponse DocumentSummary Edge EdgeWarning ExtractedField FieldChange "
    "HealthIndicators IngestPreview LastOptimizeSummary LifecycleState LinkRequest ListFieldPatch "
    "OpenDocumentResponse OptimizeContentStoreReport PendingMetadata ReabstractProgressEvent "
    "ReabstractReport ReabstractReportEntry ReabstractRequest ReabstractStartedResponse "
    "ReabstractSummaryEvent ReadMeta RelocationPointer ResolutionPathEntry RetrievalFilters "
    "StagingEdge StagingEdgeConfirmResponse StagingEdgeDismissResponse Tier3Patch TraversalNode "
    "TraverseRequest TraverseResponse UpdateConfigResponse UpdateMetadataRequest "
    "UpdateVaultConfigRequest VaultConfigPreview VaultStats VaultSummary"
)
_CONFORMANCE_REQUESTS: Final[str] = (
    "BatchIngestFileMetadata BatchIngestUploadMetadata BulkLifecycleItem BulkLifecycleRequest "
    "BulkLinkItem BulkLinkRequest BulkMetadataItem BulkMetadataRequest CreateVaultRequest "
    "DiscoverRequest LinkRequest ListFieldPatch ReabstractRequest RelocationPointer "
    "RetrievalFilters Tier3Patch TraverseRequest UpdateMetadataRequest UpdateVaultConfigRequest"
)
_CONFORMANCE_STREAMS: Final[str] = (
    "BatchDocumentsCreated BatchIngestFileError BatchProgressEvent BatchSummaryEvent "
    "DocTypeRequirements EdgeWarning IngestPreview ReabstractProgressEvent ReabstractReportEntry "
    "ReabstractSummaryEvent"
)

KNOWN_TEST_REMOVALS: Final[dict[str, str]] = {
    (
        "tests/sage/test_postgres_schema.py::"
        "test_bootstrap_adds_a_new_column_to_an_already_provisioned_schema"
    ): (
        "parametrized over each additive documents column, so the case runs under "
        "its [stored_content_hash-text] and [adapter_config-jsonb] ids"
    ),
    (
        "tests/sage/test_reindex_chunks_script.py::"
        "test_reindex_skips_documents_already_at_current_version"
    ): (
        "inverted and renamed test_reindex_selects_documents_by_chunks_not_version: "
        "the script no longer stamps adapter_version, so the version no longer "
        "selects what it re-embeds"
    ),
    (
        "tests/sage/test_reindex_chunks_script.py::"
        "test_reindex_handles_source_type_with_no_adapter_registered"
    ): (
        "inverted and renamed test_reindex_reembeds_whatever_the_source_type: "
        "re-embedding reads stored passages, never a source, so no adapter is needed"
    ),
    (
        "tests/deploy/test_stub_server.py::"
        "test_stub_modules_import_the_shared_helper[test_cloud_preflight.py]"
    ): (
        "the preflight gate was split by theme and its stub server moved to "
        "_preflight_harness.py, so the import guard runs under that module's id"
    ),
    (
        "tests/deploy/test_cloud_preflight.py::"
        "test_sweep_marker_resolves_against_its_response_schema[/staging-edges-^[[:space:]]*\\\\[]"
    ): (
        "the staging-edges response became an object carrying read markers, so the "
        "sweep's marker is the required `items` field and the case runs under that id"
    ),
    (
        "tests/sage/test_mcp_docstring_disclosure_parity.py::"
        "test_unenrolled_pins_are_not_stale[sage_core-restore_vault_source_file]"
    ): (
        "the pair came clean and was enrolled, so it is now held by the enrolled-pair "
        "claim and identifier tests instead of a pin"
    ),
    "tests/sage/test_mcp_tool_description_budget.py::test_known_unconverted_is_not_stale": (
        "retired with the list it guarded: every registered tool now satisfies the "
        "description budget, so the budget gate holds each one unconditionally and "
        "there is no exemption list left to go stale"
    ),
    "tests/sage/test_mcp_server.py::test_docstring_documents_doc_id_alias[chain]": (
        "renamed test_published_tool_documents_doc_id_alias when the alias pin moved "
        "from the Python docstring to the text a client publishes, which is the "
        "description plus the parameter descriptions"
    ),
    "tests/sage/test_mcp_server.py::test_docstring_documents_doc_id_alias[get_document]": (
        "renamed test_published_tool_documents_doc_id_alias when the alias pin moved "
        "from the Python docstring to the text a client publishes, which is the "
        "description plus the parameter descriptions"
    ),
    "tests/sage/test_mcp_server.py::test_docstring_documents_doc_id_alias[list_headings]": (
        "renamed test_published_tool_documents_doc_id_alias when the alias pin moved "
        "from the Python docstring to the text a client publishes, which is the "
        "description plus the parameter descriptions"
    ),
    "tests/sage/test_mcp_server.py::test_docstring_documents_doc_id_alias[read_projection]": (
        "renamed test_published_tool_documents_doc_id_alias when the alias pin moved "
        "from the Python docstring to the text a client publishes, which is the "
        "description plus the parameter descriptions"
    ),
    "tests/sage/test_mcp_server.py::test_docstring_documents_doc_id_alias[read_section]": (
        "renamed test_published_tool_documents_doc_id_alias when the alias pin moved "
        "from the Python docstring to the text a client publishes, which is the "
        "description plus the parameter descriptions"
    ),
    "tests/deploy/test_cloud_preflight.py::test_sweep_marker_resolves_against_its_response_schema"
    "[/pending-metadata-^[[:space:]]*\\\\[]": (
        're-parametrized as [/pending-metadata-"total_available"] when the pending-metadata '
        "response became an object and the sweep's marker became a field name"
    ),
    "tests/app/test_app_unknown_parameter.py::"
    "test_undeclared_file_entry_field_is_a_nested_invalid_parameter": (
        "renamed test_undeclared_file_entry_field_names_the_entry_models_fields when a "
        "nested undeclared key became undeclared_key carrying the refusing model's "
        "field set"
    ),
    "tests/app/test_app_unknown_parameter.py::"
    "test_undeclared_parsed_metadata_key_is_a_nested_invalid_parameter": (
        "renamed test_undeclared_parsed_metadata_key_names_the_metadata_models_fields "
        "for the same reason"
    ),
    "tests/app/test_mcp_app_tools.py::TestAppBatchIngest::"
    "test_undeclared_file_entry_key_is_refused": (
        "renamed test_undeclared_file_entry_key_names_the_accepted_set when the refusal "
        "gained the entry's field set"
    ),
    "tests/app/test_mcp_app_tools.py::TestAppBatchIngest::"
    "test_undeclared_parsed_metadata_key_is_refused": (
        "renamed test_undeclared_parsed_metadata_key_names_the_accepted_set for the same reason"
    ),
    "tests/app/test_mcp_app_tools.py::TestAppBatchIngest::"
    "test_wrong_typed_parsed_metadata_is_refused[undeclared-key-wins]": (
        "split out as test_an_undeclared_name_wins_over_a_bad_value_in_the_same_object "
        "when the undeclared key stopped sharing a code with the wrong-typed values the "
        "remaining arms cover"
    ),
    "tests/sage/test_batch_ingest_endpoint.py::"
    "test_b26_undeclared_file_entry_key_is_invalid_parameter": (
        "renamed test_b26_undeclared_file_entry_key_names_the_accepted_set when the "
        "refusal moved to undeclared_key and gained the upload entry's field set"
    ),
    "tests/sage/test_validation_envelope_parity.py::"
    "test_unknown_item_field_parity_between_surfaces": (
        "renamed test_undeclared_item_field_parity_between_surfaces when both surfaces "
        "began answering with the item model's field set at the same location"
    ),
    "tests/test_development_policy_proposal.py::test_proposal_does_not_install_governing_pointer": (
        "Activation candidate intentionally selects the guide/profile; replaced by "
        "test_candidate_selection_is_explicit_and_live_activation_fails_closed and "
        "test_policy_conformance_accepts_both_activation_states."
    ),
    "tests/test_specialized_source_package.py::"
    "test_declared_local_references_survive_source_and_target_layouts": (
        "Canonical repository procedures no longer ship through the historical overlay; "
        "test_canonical_repository_references_remain_resolvable preserves source and "
        "applicable target-link checks."
    ),
    **{
        f"tests/sage/test_adapter_config_refusal.py::"
        f"test_ad_185_a_malformed_source_keeps_its_reporting_{surface}": (
            f"renamed test_ad_185_a_malformed_source_is_not_a_config_refusal_{surface} "
            "when an unreadable source gained its own typed refusal, which the "
            "retargeted assertion now pins"
        )
        for surface in ("in_a_batch", "on_ingest_document")
    },
    "tests/sage/test_mcp_server.py::test_error_response_vault_not_found_returns_unknown_vault": (
        "renamed test_error_response_vault_not_found_carries_the_typed_envelope when an "
        "unregistered vault became the vault_not_found refusal on both surfaces"
    ),
    "tests/sage/test_graph_store_seam.py::test_stub_hash_lookup_signature_matches_port": (
        "single-method stub signature check absorbed by the stub arm of the graph-store "
        "signature gate, which covers every port method"
    ),
    **{
        f"tests/sage/test_retrieval.py::"
        f"test_semantic_and_keyword_responses_are_not_degraded[{mode}]": (
            "pinned scored modes carrying no budget element; replaced by "
            "test_scored_responses_reach_a_budget_outcome_but_never_the_catalog_degrade, "
            "which requires one now that scored responses are excerpted over budget"
        )
        for mode in ("semantic", "keyword")
    },
    "tests/test_pipeline_poll_discipline.py::test_no_fixture_yields_an_unsettled_document": (
        "renamed test_no_fixture_hands_off_an_unsettled_document when the unwaited-fixture "
        "arm began anchoring on return and completion as well as yield"
    ),
    "tests/app/test_frontend_type_conformance.py::test_reader_refuses_a_heritage_clause": (
        "replaced by test_reader_resolves_a_heritage_clause when the reader began resolving "
        "bare-name heritage; the forms it still refuses are pinned by "
        "test_reader_refuses_heritage_it_does_not_model"
    ),
    **{
        f"tests/app/test_bff_cloud_ingest.py::test_app_{old}_{rest}": (
            f"renamed test_app_{new}_{rest}: its APP number duplicated one in "
            "test_bff_standalone_app.py, which shares the APP-NNN sequence"
        )
        for old, new, rest in (
            ("009", "025", "cloud_ingest_route_stays_co_located_only"),
            ("010", "026", "cloud_batch_upload_via_proxy_requires_session"),
        )
    },
    "tests/sage/test_passage_replace_if_unchanged.py": (
        "the content-store compare-and-replace and per-document passage write lock it pinned "
        "are removed; migrate_vault now excludes pipeline work at the vault level"
    ),
    **{
        f"tests/sage/test_content_store_seam.py::"
        f"test_cs5_binding_signature_matches_port[replace_chunks_if_unchanged-{binding}]": (
            "signature arm for a port method removed from the port and every binding"
        )
        for binding in ("PostgresContentStore", "StubContentStore")
    },
    **{
        f"tests/sage/test_passage_bound_migration.py::{name}": (
            "pinned the division deferring a document to a later migration run, which the "
            "vault-level migration exclusion replaces; covered by "
            "test_migration_pipeline_exclusion.py"
        )
        for name in (
            "test_a_document_with_pipeline_work_in_flight_is_left_for_a_later_run",
            "test_passages_rewritten_while_the_division_embeds_are_not_overwritten",
            "test_a_document_left_for_a_later_run_is_logged_with_its_reason",
        )
    },
    **{
        f"tests/sage/test_passage_bound_migration.py::"
        f"test_a_document_mid_pipeline_is_left_for_a_later_run[{status}]": (
            "inverted and renamed test_a_document_at_a_non_terminal_status_is_divided now that "
            "the division no longer skips a non-terminal document"
        )
        for status in ("indexing_in_progress", "abstraction_in_progress")
    },
    "tests/sage/test_passage_bound_migration.py::"
    "test_the_division_holds_the_document_while_it_embeds": (
        "renamed test_the_division_holds_the_document_from_its_read_to_its_write when it began "
        "observing the per-document lock at the read as well as the embed"
    ),
    "tests/sage/test_list_headings.py::test_list_headings_returns_only_authored_headings": (
        "renamed test_list_headings_leaks_no_internal_marker: the listing may now carry the "
        "empty path, so the name states the property the unchanged assertion checks"
    ),
    **{
        f"tests/app/test_frontend_type_conformance.py::{gate}[{interface}]": (
            "the frontend type conformance gate began reading a second specification and its "
            "per-interface ids were keyed by specification; the same pair runs as core-<name>"
        )
        for gate, interfaces in (
            ("test_schema_properties_are_declared_on_the_interface", _CONFORMANCE_INTERFACES),
            ("test_interface_declares_no_property_absent_from_the_schema", _CONFORMANCE_INTERFACES),
            (
                "test_member_names_the_interface_of_each_enrolled_component_it_references",
                _CONFORMANCE_INTERFACES,
            ),
            (
                "test_required_request_properties_are_not_optional_on_the_interface",
                _CONFORMANCE_REQUESTS,
            ),
            ("test_stream_member_optionality_matches_the_required_list", _CONFORMANCE_STREAMS),
        )
        for interface in interfaces.split()
    },
    "tests/app/test_frontend_type_conformance.py::"
    "test_stream_enrollment_matches_the_event_stream_contract": (
        "parametrized by specification when the gate began reading a second one; the same "
        "check runs as [core]"
    ),
    **{
        f"tests/sage/test_mcp_heavy_parameter_descriptions.py::{test_id}": (
            "superseded by the per-tool presented-size ceiling in "
            "test_mcp_presented_size_budget, which bounds each parameter's text as part of "
            "its tool's whole presented size"
        )
        for test_id in (
            "test_heavy_parameter_ceilings_name_published_parameters",
            *(
                f"test_heavy_parameter_description_within_ceiling[{key}]"
                for key in (
                    "bulk_ingest_document.files",
                    "bulk_ingest_document.infer_edges",
                    "create_edges.items",
                    "ingest_document.dry_run",
                    "search.query",
                    "search.response_mode",
                    "update_lifecycles.items",
                    "update_metadata.items",
                )
            ),
        )
    },
    **{
        (
            "tests/sage/test_content_store_seam.py::"
            f"test_cs5_binding_signature_matches_port[passage_vector_ranks_indexed_structure-{binding}]"
        ): (
            "the port method became passage_vector_is_current once the vector's "
            "currency covered more than its structure, so the case runs under that id"
        )
        for binding in ("PostgresContentStore", "StubContentStore")
    },
    **{
        f"tests/sage/test_rest_unknown_parameter.py::{test}[confirm_staging_edge]": (
            "confirm_staging_edge declares an optional body carrying agent, so it "
            "is no longer a bodyless operation; its body refusals and its empty-object "
            "acceptance are asserted in tests/sage/test_write_provenance.py"
        )
        for test in (
            "test_json_field_on_bodyless_operation_refused",
            "test_empty_object_body_on_bodyless_operation_accepted",
        )
    },
    "tests/sage/test_access_control.py": (
        "the per-vault user registry is removed, so owner bootstrap and register_user have "
        "nothing to test"
    ),
    "tests/sage/test_alias_invariants.py::test_invalid_inputs_rejected[UserIdStr-empty_string]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_alias_invariants.py::test_invalid_inputs_rejected[UserIdStr-extended_uuid]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_alias_invariants.py::test_invalid_inputs_rejected[UserIdStr-non_hex_chars]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_alias_invariants.py::test_invalid_inputs_rejected[UserIdStr-random_text]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_alias_invariants.py::test_invalid_inputs_rejected[UserIdStr-truncated_uuid]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    (
        "tests/sage/test_alias_invariants.py::"
        "test_invalid_inputs_rejected[UserIdStr-wrong_separator]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_alias_invariants.py::"
        "test_user_id_non_canonical_inputs_normalized_to_canonical[brace_wrapped]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_alias_invariants.py::"
        "test_user_id_non_canonical_inputs_normalized_to_canonical[mixed_case]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_alias_invariants.py::"
        "test_user_id_non_canonical_inputs_normalized_to_canonical[no_hyphens_hex]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_alias_invariants.py::"
        "test_user_id_non_canonical_inputs_normalized_to_canonical[urn_prefixed]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    "tests/sage/test_alias_invariants.py::test_uuid_format_is_what_the_alias_emits[UserIdStr]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_alias_invariants.py::test_valid_inputs_accepted[UserIdStr]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    "tests/sage/test_api_integration.py::test_register_user_201": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_graph_store_seam.py::test_concrete_signature_matches_port[get_user]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    (
        "tests/sage/test_graph_store_seam.py::"
        "test_concrete_signature_matches_port[get_user_by_display_name]"
    ): ("the graph-store port no longer stores users; the per-vault user registry is removed"),
    "tests/sage/test_graph_store_seam.py::test_concrete_signature_matches_port[insert_user]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    "tests/sage/test_graph_store_seam.py::test_concrete_signature_matches_port[list_users]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    "tests/sage/test_graph_store_seam.py::test_stub_signature_matches_port[get_user]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    (
        "tests/sage/test_graph_store_seam.py::"
        "test_stub_signature_matches_port[get_user_by_display_name]"
    ): ("the graph-store port no longer stores users; the per-vault user registry is removed"),
    "tests/sage/test_graph_store_seam.py::test_stub_signature_matches_port[insert_user]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    "tests/sage/test_graph_store_seam.py::test_stub_signature_matches_port[list_users]": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    "tests/sage/test_mcp_server.py::test_error_response_maps_typed_alias_family[invalid_user_id]": (
        "UserIdStr and the invalid_user_id code are removed with the per-vault user registry"
    ),
    (
        "tests/sage/test_mcp_server.py::"
        "test_translate_validation_error_maps_typed_alias_family[invalid_user_id]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_mcp_tool_conformance.py::"
        "test_openapi_operation_has_mcp_tool[sage_core-get_editors]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_mcp_tool_conformance.py::"
        "test_openapi_operation_has_mcp_tool[sage_core-register_user]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_mcp_tool_conformance.py::"
        "test_openapi_operation_has_mcp_tool[sage_core-set_editors]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_openapi_conformance.py::"
        "test_alias_typed_params_declare_their_400[POST /sage_vaults/{vault_id}/users -> inva"
        "lid_vault_id]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_openapi_conformance.py::"
        "test_live_openapi_matches_yaml_error_envelope[POST /sage_vaults/{vault_id}/users -> "
        "400]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_openapi_conformance.py::"
        "test_live_openapi_matches_yaml_error_envelope[POST /sage_vaults/{vault_id}/users -> "
        "404]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_openapi_conformance.py::"
        "test_live_openapi_matches_yaml_error_envelope[POST /sage_vaults/{vault_id}/users -> "
        "422]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_postgres_graph_store.py::test_pg_row_to_user_populates_every_field": (
        "the graph-store port no longer stores users; the per-vault user registry is removed"
    ),
    (
        "tests/sage/test_published_shape_parity.py::"
        "test_alias_publishes_its_declared_shape[UserIdStr-serialization]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_published_shape_parity.py::"
        "test_alias_publishes_its_declared_shape[UserIdStr-validation]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_published_shape_parity.py::"
        "test_refusal_code_is_the_validators_own[UserIdStr]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    "tests/sage/test_rest_unknown_parameter.py::test_declared_body_not_refused[register_user]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_rest_unknown_parameter.py::test_unknown_body_field_refused[register_user]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_router_conformance.py::test_route_models_are_pydantic[sage-users]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_router_conformance.py::test_router_conformance[sage-users]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_typed_alias_coverage.py::"
        "test_fastapi_route_param_coverage[sage.api.dependencies.get_user_service.vault_id]"
    ): ("the graph-store port no longer stores users; the per-vault user registry is removed"),
    (
        "tests/sage/test_typed_alias_coverage.py::"
        "test_fastapi_route_param_coverage[sage.api.routers.users.register_user.vault_id]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_typed_alias_coverage.py::test_typed_alias_coverage[EditorList.document_id]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_typed_alias_coverage.py::"
        "test_typed_alias_coverage[SetEditorsRequest.user_ids]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    "tests/sage/test_typed_alias_coverage.py::test_typed_alias_coverage[User.id]": (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_typed_alias_coverage.py::"
        "test_typed_alias_validator_raises_structured_custom_error[invalid_user_id]"
    ): ("UserIdStr and the invalid_user_id code are removed with the per-vault user registry"),
    (
        "tests/sage/test_vault_config_api.py::"
        "test_create_vault_rolls_back_when_bootstrap_owner_fails_post_register"
    ): (
        "folded into test_create_vault_rolls_back_yaml_on_initialize_failure: owner "
        "bootstrap is removed, so nothing can fail once the services are registered"
    ),
    (
        "tests/sage/test_vault_not_found_refusal_parity.py::"
        "test_vault_scoped_operations_declare_vault_not_found[sage_core-get_editors]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_vault_not_found_refusal_parity.py::"
        "test_vault_scoped_operations_declare_vault_not_found[sage_core-register_user]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    (
        "tests/sage/test_vault_not_found_refusal_parity.py::"
        "test_vault_scoped_operations_declare_vault_not_found[sage_core-set_editors]"
    ): (
        "the per-vault user registry and its editor contract are removed; nothing registers a"
        " user or accepts a user id"
    ),
    "tests/sage/test_write_attribution.py::test_u1_principal_actor_derivation[delegated-oid]": (
        "re-parametrized for the stable key: the cases run under their delegated-tenant-* and"
        " delegated-issuer-* ids"
    ),
    "tests/sage/test_write_attribution.py::test_u1_principal_actor_derivation[delegated-sub]": (
        "re-parametrized for the stable key: the cases run under their delegated-tenant-* and"
        " delegated-issuer-* ids"
    ),
    "tests/sage/test_write_attribution.py::test_u1_principal_actor_derivation[delegated-upn]": (
        "re-parametrized for the stable key: the cases run under their delegated-tenant-* and"
        " delegated-issuer-* ids"
    ),
}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class _SurfacePlugin:
    """Record every collected item and whether it would actually run.

    ``pytest_collection_modifyitems`` is the last hook that sees the whole
    collected set, and it runs under ``--collect-only``, so the walk costs one
    collection pass rather than a suite run. ``evaluate_skip_marks`` returns a
    ``Skip`` for an item a ``skip`` or ``skipif`` mark takes out of the run,
    and ``None`` for one that survives to execution.

    Skips raised at run time -- from inside a fixture or a test body -- are
    invisible here, because nothing has run. That is a known limit: this
    measures the statically determined active set, which is where both
    observed contractions lived.
    """

    def __init__(self) -> None:
        self.collected: list[str] = []
        self.active: list[str] = []

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        for item in items:
            self.collected.append(item.nodeid)
            if evaluate_skip_marks(item) is None:
                self.active.append(item.nodeid)


def measure(target: str) -> Surface:
    """Collect ``target`` and return its collected and active node id sets.

    ``-n 0`` keeps collection on one process: the parallel default would both
    distribute the walk and, through the worker-sizing hook, open a database
    connection this measurement has no use for. ``-p no:cacheprovider`` keeps
    the run from writing a cache directory into the tree being measured, which
    matters when that tree is a throwaway checkout of another revision.
    """
    import pytest

    plugin = _SurfacePlugin()
    outcome = pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", "-n", "0", target],
        plugins=[plugin],
    )
    return {
        "target": target,
        "exit_code": int(outcome),
        "collected": sorted(plugin.collected),
        "active": sorted(plugin.active),
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def leaf_key(node_id: str) -> str:
    """The part of a node id that survives moving the test to another module.

    ``tests/sage/test_x.py::TestThing::test_case[a]`` yields
    ``TestThing::test_case[a]``. A node id with no module separator is its own
    leaf key.
    """
    marker = ".py::"
    index = node_id.rfind(marker)
    return node_id if index == -1 else node_id[index + len(marker) :]


def is_declared(node_id: str, removals: dict[str, str]) -> bool:
    """True when ``node_id`` is covered by a declared-removal entry.

    An entry matches exactly, or as a prefix when it ends in ``::`` (a class or
    module boundary) or ``.py`` (a whole module). Bare substring matching is
    deliberately not offered: it would let a short entry silently cover tests
    nobody meant to waive.
    """
    if node_id in removals:
        return True
    return any(
        node_id.startswith(entry)
        for entry in removals
        if entry.endswith("::") or entry.endswith(".py")
    )


def classify(
    base: Surface,
    head: Surface,
    removals: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Partition the base-to-head change in the active set.

    ``lost`` is the only failing category. ``deactivated`` is the subset of it
    that head still collects -- the test is still there and still parses, it
    just no longer runs -- because that distinction points at a different fix
    from a deletion.

    The relocation escape compares leaf-key *populations*, not membership.
    Method names repeat heavily across parallel modules in a suite of any size,
    so asking only whether the leaf key still exists somewhere cannot separate
    "this test moved" from "a namesake in another module survived and this one
    is gone" -- and under that weaker rule, deleting a whole module whose names
    recur elsewhere reports every one of its tests as relocated and exits
    clean. Requiring the head's count for a leaf key to hold at the base's
    keeps a true move (the count is unchanged) and reports a deletion beside a
    namesake (the count drops).
    """
    entries = KNOWN_TEST_REMOVALS if removals is None else removals
    base_active = set(base["active"])
    head_active = set(head["active"])
    head_collected = set(head["collected"])
    base_leaves = Counter(leaf_key(node_id) for node_id in base_active)
    head_leaves = Counter(leaf_key(node_id) for node_id in head_active)

    lost: list[str] = []
    moved: list[str] = []
    declared: list[str] = []
    for node_id in sorted(base_active - head_active):
        leaf = leaf_key(node_id)
        if is_declared(node_id, entries):
            declared.append(node_id)
        elif head_leaves[leaf] >= base_leaves[leaf]:
            moved.append(node_id)
        else:
            lost.append(node_id)

    return {
        "lost": lost,
        "deactivated": [node_id for node_id in lost if node_id in head_collected],
        "moved": moved,
        "declared": declared,
        "added": sorted(head_active - base_active),
    }


def _section(title: str, entries: list[str]) -> str:
    head = entries[:_MAX_REPORTED]
    body = "\n".join(f"  {entry}" for entry in head)
    overflow = len(entries) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return f"{title} ({len(entries)}):\n{body}{tail}"


def format_report(
    base: Surface,
    head: Surface,
    verdict: dict[str, list[str]],
) -> str:
    """Render the comparison, failing sections first."""
    lines = [
        f"tests active: {len(base['active'])} before, {len(head['active'])} after "
        f"({len(base['collected'])} and {len(head['collected'])} collected)",
    ]
    if verdict["lost"]:
        lines.append("")
        lines.append(_section("Tests that no longer run", verdict["lost"]))
        if verdict["deactivated"]:
            lines.append("")
            lines.append(
                _section(
                    "  of which are still collected, so they were gated rather than removed",
                    verdict["deactivated"],
                )
            )
        lines.append("")
        lines.append(
            "Each of these ran before this change and does not run now. If that is "
            "deliberate, add the node id (or a '::' / '.py' prefix) to "
            "KNOWN_TEST_REMOVALS in scripts/check_test_surface.py with a one-line "
            "reason, so the removal is visible in the diff."
        )
    for title, key in (
        ("Tests moved to another module", "moved"),
        ("Declared removals", "declared"),
    ):
        if verdict[key]:
            lines.append("")
            lines.append(_section(title, verdict[key]))
    if verdict["added"]:
        lines.append("")
        lines.append(f"newly running: {len(verdict['added'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load(path: str) -> Surface:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_compare(base_path: str, head_path: str) -> int:
    base = _load(base_path)
    head = _load(head_path)

    if len(base["active"]) < MIN_BASE_ACTIVE:
        print(
            f"the earlier measurement carries {len(base['active'])} active tests, "
            f"below the floor of {MIN_BASE_ACTIVE}; refusing to report a comparison "
            "against a measurement that appears to have examined nothing",
            file=sys.stderr,
        )
        return 1

    verdict = classify(base, head)
    print(format_report(base, head, verdict))
    return 1 if verdict["lost"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the set of tests that actually run, before and after a change."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--emit",
        action="store_true",
        help="measure this tree and write the collected and active node id sets.",
    )
    mode.add_argument(
        "--compare",
        nargs=2,
        metavar=("EARLIER", "LATER"),
        help="compare two measurements and fail if any test stopped running.",
    )
    parser.add_argument(
        "--target",
        default="tests",
        help="path to collect when emitting (default: tests).",
    )
    parser.add_argument(
        "--out",
        help="write the measurement here instead of standard output.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return _run_compare(*args.compare)

    surface = measure(args.target)
    if surface["exit_code"] not in (0, 5):
        print(
            f"collection of {args.target!r} failed with pytest exit code "
            f"{surface['exit_code']}; the measurement is unusable",
            file=sys.stderr,
        )
        return 1

    payload = json.dumps(surface, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(
            f"{len(surface['active'])} of {len(surface['collected'])} collected tests "
            f"would run; wrote {args.out}"
        )
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
