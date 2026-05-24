"""Metadata update logic for the update_metadata endpoint (BH-005, BH-006)."""

import re
from datetime import datetime, timezone

import jsonschema

from sage.adapters.interfaces import ContentStore
from sage.api.errors import (
    DocumentNotFoundError,
    InvalidDocTypeError,
    SAGEError,
    TagAddConflictError,
    TagRemoveConflictError,
    Tier3DocTypeChangeStaleKeysError,
    Tier3SchemaViolationError,
    Tier3UnsetConflictError,
)
from sage.config import VaultConfig
from sage.models.enums import LIGHT_DEFAULT_THRESHOLD, ResponseMode
from sage.models.schemas import (
    BulkMetadataItemResult,
    BulkMetadataRequest,
    BulkMetadataResponse,
    Document,
    ExtractedField,
    PendingMetadataItem,
    TagsPatch,
    Tier3Patch,
    UpdateMetadataRequest,
    UpdateMetadataResponse,
)
from sage.services._bulk_envelope import sage_error_to_envelope
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


class MetadataService:
    def __init__(
        self,
        graph_store: GraphStore,
        lock_manager: DocumentLockManager,
        config: VaultConfig,
        content_store: ContentStore | None = None,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._config = config
        self._content = content_store

    async def update_metadata(
        self,
        document_id: str,
        request: UpdateMetadataRequest,
        modified_by: str,
    ) -> UpdateMetadataResponse:
        """Partial update of mutable metadata fields with patch semantics.

        Scalar fields (``title``, ``version_label``, ``project``,
        ``doc_type``, ``authority_scope``, ``document_date``) use
        set-or-omit semantics. ``tags`` and ``tier3_metadata`` take ops
        objects (``TagsPatch`` / ``Tier3Patch``) and apply patch
        operations to the stored state with strict-conflict semantics:
        adding a tag already present, removing a tag absent, or
        unsetting a tier3 key absent each raise 400. ``set`` on an
        existing tier3 key overwrites without error. The merged
        tier3 dict is validated against the resolved doc_type's
        metadata_schema after applying the patch in memory.

        Returns:
            ``UpdateMetadataResponse`` wrapping the post-patch document
            and a ``dry_run`` echo (T-0152). On a real run, ``document``
            is the persisted row; on a dry-run, ``document`` is the
            computed post-patch state with no ``updated_at`` advance.

        Raises:
            DocumentNotFoundError: document_id does not exist.
            InvalidDocTypeError: doc_type not in vault config.
            TagAddConflictError: TagsPatch.add includes already-present tags.
            TagRemoveConflictError: TagsPatch.remove includes absent tags.
            Tier3UnsetConflictError: Tier3Patch.unset includes absent keys.
            Tier3SchemaViolationError: merged tier3 invalid for the
                resolved doc_type, or the doc_type has no metadata_schema
                declared.
        """
        async with self._locks.lock(document_id):
            doc = await self._store.get_document(document_id)
            if doc is None:
                raise DocumentNotFoundError(document_id)

            updates: dict = {}
            if request.title is not None:
                updates["title"] = request.title
            if request.version_label is not None:
                updates["version_label"] = request.version_label
            if request.project is not None:
                updates["project"] = request.project
            if request.tags is not None:
                updates["tags"] = self._apply_tags_patch(document_id, doc.tags, request.tags)
            if request.authority_scope is not None:
                updates["authority_scope"] = request.authority_scope
            if request.doc_type is not None:
                valid_types = self._config.valid_doc_type_values()
                if request.doc_type not in valid_types:
                    raise InvalidDocTypeError(request.doc_type, valid_types)
                updates["doc_type"] = request.doc_type
            if request.document_date is not None:
                updates["document_date"] = request.document_date
            # T-0156 Wart 1: a doc_type change must revalidate the stored
            # tier3 against the new schema even when no Tier3Patch was
            # supplied -- otherwise stored keys from the old doc_type
            # silently survive against the new one. So the validation
            # block runs whenever the caller supplies a patch OR the
            # doc_type is actually changing.
            is_doc_type_change = request.doc_type is not None and request.doc_type != doc.doc_type
            if request.tier3_metadata is not None or is_doc_type_change:
                # Resolve against the post-update doc_type: when the caller
                # also changes doc_type in the same request, validation
                # uses the new doc_type so the stored tier3 conforms to
                # the schema of its new owner.
                effective_dt = request.doc_type if request.doc_type is not None else doc.doc_type
                if request.tier3_metadata is not None:
                    merged = self._apply_tier3_patch(
                        document_id, effective_dt, doc.tier3_metadata, request.tier3_metadata
                    )
                else:
                    # No patch supplied: validate the stored dict as-is
                    # against the new doc_type (Wart 1 no-patch path).
                    merged = dict(doc.tier3_metadata or {})
                # T-0151: when the call changes doc_type, surface stale
                # legacy keys explicitly so the caller knows exactly which
                # keys to add to `unset`. The post-merge `_validate_tier3`
                # would otherwise raise a generic tier3_schema_violation
                # that conflates "your patch is wrong for the new schema"
                # with "you forgot to unset legacy keys." A no-schema new
                # doc_type means every merged key is stale (zero allowed
                # properties). T-0156 broadened this to the no-patch path.
                if is_doc_type_change:
                    validator = self._config.tier3_validator(effective_dt)
                    allowed: set[str] = (
                        set(validator.schema.get("properties", {}).keys())
                        if validator is not None
                        else set()
                    )
                    stale = [k for k in merged if k not in allowed]
                    if stale:
                        raise Tier3DocTypeChangeStaleKeysError(
                            document_id=document_id,
                            previous_doc_type=doc.doc_type or "",
                            new_doc_type=request.doc_type,
                            stale_keys=stale,
                            merged_tier3_keys=list(merged.keys()),
                        )
                self._validate_tier3(effective_dt, merged)
                # Write discipline: only persist a new tier3 dict when the
                # caller actually supplied a patch. A doc_type-only change
                # that revalidates the stored dict must not rewrite it.
                if request.tier3_metadata is not None:
                    updates["tier3_metadata"] = merged

            # T-0152: dry-run branches BEFORE the persistence call and
            # BEFORE stamping updated_at / metadata_confirmed. The
            # computed `updates` dict already represents the would-be
            # post-patch state for every persisted field; applying it
            # to a shallow copy of the in-memory doc gives the preview
            # with byte-identical semantics to the real-run output.
            if request.dry_run:
                preview = doc.model_copy(update=updates)
                return UpdateMetadataResponse(document=preview, dry_run=True)

            # Mark metadata as confirmed on every update_metadata call,
            # even with an empty body (pure confirmation without edits).
            updates["metadata_confirmed"] = True
            updates["last_modified_by"] = modified_by
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            doc = await self._store.update_document(document_id, updates)

            # Sync chunk-pushdownable scalars to the content store so
            # LanceDB pre-filter pushdown (T-0050 for doc_type, T-0077
            # for lifecycle_status and project) stays accurate after
            # the document update. update_metadata never touches
            # lifecycle_status (that lives on LifecycleService); only
            # doc_type and project flow through here.
            if self._content:
                chunk_updates: dict[str, str | None] = {}
                if "doc_type" in updates:
                    chunk_updates["doc_type"] = updates["doc_type"]
                if "project" in updates:
                    chunk_updates["project"] = updates["project"]
                if chunk_updates:
                    await self._content.update_chunk_metadata(document_id, chunk_updates)

            return UpdateMetadataResponse(document=doc, dry_run=False)

    async def bulk_update_metadata(
        self,
        request: BulkMetadataRequest,
        modified_by: str,
    ) -> BulkMetadataResponse:
        """Apply one metadata patch per item; per-item lock and per-item transaction.

        The batch is NOT atomic (CAS-ADR-029). A SAGEError raised by one
        item does not roll back earlier-or-later successful items; the
        item's per-document lock and per-item transaction provide
        isolation. Anything outside the SAGEError hierarchy is treated
        as a programmer or infrastructure bug and propagates out of the
        batch.

        Per-item patch semantics (tags, tier3_metadata, scalar fields)
        are identical to single-item ``update_metadata`` (CAS-ADR-028).
        The bulk method is a thin loop around the single-item code path
        and reuses its patch validators, merge-and-validate-tier3 step,
        and locking discipline.

        The performance win versus N sequential ``sage_update_metadata``
        MCP calls comes from eliminating per-call MCP framing overhead
        and asyncio scheduling between items; the per-document lock and
        the per-item SQLite transaction are unchanged.

        ``request.response_mode`` (T-0153) controls per-item payload
        depth. ``light`` drops the per-item ``document`` body from
        success entries; failure entries always carry the full structured
        error envelope. When unset, the default-resolution rule mirrors
        ``sage_discover``: batches with more than
        ``LIGHT_DEFAULT_THRESHOLD`` items default to ``light``, smaller
        batches default to ``full``.
        """
        # T-0153: resolve the effective response_mode (mirror
        # ``RetrievalService._edges``). Driven by ``len(items)`` because
        # the bulk endpoint's blast radius is known up front.
        effective_mode = request.response_mode
        if effective_mode is None:
            effective_mode = (
                ResponseMode.LIGHT
                if len(request.items) > LIGHT_DEFAULT_THRESHOLD
                else ResponseMode.FULL
            )

        results: list[BulkMetadataItemResult] = []
        for item in request.items:
            single = UpdateMetadataRequest(
                title=item.title,
                version_label=item.version_label,
                project=item.project,
                tags=item.tags,
                doc_type=item.doc_type,
                authority_scope=item.authority_scope,
                document_date=item.document_date,
                tier3_metadata=item.tier3_metadata,
                # T-0152: propagate envelope dry_run to each per-item
                # call. Per-item override is not supported; the
                # envelope is the single source of truth for the batch.
                dry_run=request.dry_run,
            )
            try:
                response = await self.update_metadata(item.document_id, single, modified_by)
                results.append(
                    BulkMetadataItemResult(
                        document_id=item.document_id,
                        status="success",
                        # T-0153 light mode: drop the document body. The
                        # caller already knows the document_id (they
                        # passed it); the body's primary bloat field
                        # (semantic_abstract) is what the field report
                        # called out as overflowing the inline budget.
                        document=(
                            response.document if effective_mode == ResponseMode.FULL else None
                        ),
                    )
                )
            except SAGEError as exc:
                results.append(
                    BulkMetadataItemResult(
                        document_id=item.document_id,
                        status="error",
                        error=sage_error_to_envelope(exc),
                    )
                )
        success_count = sum(1 for r in results if r.status == "success")
        return BulkMetadataResponse(
            results=results,
            success_count=success_count,
            error_count=len(results) - success_count,
            total=len(results),
            # T-0152: envelope echo so callers can confirm the batch ran
            # as a preview even when every item's per-item response_mode
            # was light (and the per-item documents are absent).
            dry_run=request.dry_run,
        )

    @staticmethod
    def _apply_tags_patch(
        document_id: str, current_tags: list[str] | None, patch: TagsPatch
    ) -> list[str]:
        """Apply a TagsPatch to ``current_tags``, returning the new ordered list.

        Order discipline: survivors keep their stored position; new
        additions append in the order the caller supplied them. Raises
        on strict-conflict (add of present, remove of absent).
        """
        current = list(current_tags or [])
        current_set = set(current)
        if patch.add:
            already = [t for t in patch.add if t in current_set]
            if already:
                raise TagAddConflictError(document_id, already, current)
        if patch.remove:
            absent = [t for t in patch.remove if t not in current_set]
            if absent:
                raise TagRemoveConflictError(document_id, absent, current)
        removed_set: set[str] = set(patch.remove or [])
        added_in_order = list(patch.add or [])
        survivors = [t for t in current if t not in removed_set]
        return [*survivors, *added_in_order]

    @staticmethod
    def _apply_tier3_patch(
        document_id: str,
        doc_type: str | None,
        current_tier3: dict | None,
        patch: Tier3Patch,
    ) -> dict:
        """Apply a Tier3Patch to ``current_tier3``, returning the merged dict.

        Validates that all ``unset`` keys are currently present (strict
        conflict). ``set`` values overwrite existing keys without error.
        Does NOT run schema validation -- the caller does that on the
        merged result so the validator sees the post-patch state.
        """
        merged: dict = dict(current_tier3 or {})
        if patch.unset:
            absent = [k for k in patch.unset if k not in merged]
            if absent:
                raise Tier3UnsetConflictError(document_id, doc_type, absent, list(merged.keys()))
            for k in patch.unset:
                merged.pop(k)
        if patch.set:
            merged.update(patch.set)
        return merged

    def _validate_tier3(self, doc_type: str | None, tier3: dict) -> None:
        """Validate a tier3_metadata payload against a doc_type's schema.

        Raises Tier3SchemaViolationError when the doc_type has no
        metadata_schema declared (strict no-loose-mode per T-0004) or
        when the payload fails the schema. T-0156: an empty merged dict
        trivially satisfies a no-schema doc_type and is accepted, so a
        caller reclassifying to a no-schema target can unset every
        legacy key without then tripping the no-schema raise.
        """
        dt_key = doc_type or ""
        validator = self._config.tier3_validator(dt_key)
        if validator is None:
            if not tier3:
                return
            raise Tier3SchemaViolationError(
                doc_type=dt_key,
                path="",
                message=(f"doc_type '{dt_key}' has no metadata_schema declared in vault config"),
                instance=tier3,
            )
        try:
            validator.validate(tier3)
        except jsonschema.ValidationError as exc:
            raise Tier3SchemaViolationError(
                doc_type=dt_key,
                path=exc.json_path,
                message=exc.message,
                instance=tier3,
            ) from exc

    async def list_pending_metadata(self) -> list[PendingMetadataItem]:
        """Documents whose extracted metadata has not been confirmed (BE-014, BE-015)."""
        docs = await self._store.list_pending_metadata_documents()
        return [
            PendingMetadataItem(
                document=doc,
                extracted_fields=self._build_extracted_fields(doc),
            )
            for doc in docs
        ]

    @staticmethod
    def _build_extracted_fields(doc: Document) -> dict[str, ExtractedField]:
        """Build extracted field annotations for a document.

        Source annotations indicate how each metadata field was derived:
        - "filename": extracted from the source file path
        - "content": extracted from document content (headings, body)
        - "default": vault default or system-assigned value
        """
        fields: dict[str, ExtractedField] = {}

        # Title: derived from first heading (content) or filename
        fields["title"] = ExtractedField(
            value=doc.title,
            source="content",
            alt_value=doc.source_path.rsplit("/", 1)[-1]
            if "/" in doc.source_path
            else doc.source_path,
            alt_source="filename",
        )

        # doc_type: default unless explicitly set
        if doc.doc_type:
            fields["doc_type"] = ExtractedField(value=doc.doc_type, source="default")

        # project: if present
        if doc.project:
            fields["project"] = ExtractedField(value=doc.project, source="default")

        # tags: if present
        if doc.tags:
            fields["tags"] = ExtractedField(value=",".join(doc.tags), source="content")

        # document_date: filename if date pattern in source_path, otherwise default (BE-036)
        if doc.document_date:
            filename = (
                doc.source_path.rsplit("/", 1)[-1] if "/" in doc.source_path else doc.source_path
            )
            source = "filename" if re.search(r"\d{4}-\d{2}-\d{2}", filename) else "default"
            fields["document_date"] = ExtractedField(value=doc.document_date, source=source)

        return fields
