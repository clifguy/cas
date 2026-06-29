# CAS Application Frontend Tests

Tier 2 behavioral tests for the CAS Application frontend. Each test encodes a
design decision or verifiable UI behavior derived from the CAS Application Spec
v0.4. Tests are grouped by view in navigation order.

Tests cover the six views: Dashboard, Ingest, Review (metadata + edge sub-tabs),
Search, Document Detail, and Graph Explorer, plus cross-view navigation and the
vault selector.

---

## 1. Navigation and Vault Selector

### TEST-APP-UI-001: Sidebar displays four top-level navigation items

**Artifact:** App Spec v0.4, Section 4 (Navigation Structure)
**Category:** navigation

**Decision:** The sidebar presents exactly four top-level views: Dashboard,
Ingest, Review, Search. Document Detail and Graph Explorer are drill-down views
and do not appear in the sidebar.

**Precondition:** Application loaded with at least one vault configured.

**Input:** Render the application shell.

**Expected:**
- Sidebar contains exactly four navigation entries
- Labels are Dashboard, Ingest, Review, Search (in that order)
- No entries for Document Detail or Graph Explorer

**Rationale:** Drill-down views are reached by navigation from other views, not
from the sidebar. Keeping the sidebar to four entries reduces cognitive load.

### TEST-APP-UI-002: Vault selector switches vault and resets to Dashboard

**Artifact:** App Spec v0.4, Section 4 (Navigation Structure)
**Category:** navigation

**Decision:** Switching vaults resets the current view to Dashboard.

**Precondition:** Two vaults configured (e.g., example_vault, personal_notes).
User is viewing Search in the example_vault vault.

**Input:** Select personal_notes from the vault selector.

**Expected:**
- Active vault changes to personal_notes
- Current view resets to Dashboard
- Dashboard displays statistics for the personal_notes vault

**Rationale:** View state (search queries, ingest progress) is vault-scoped.
Resetting to Dashboard prevents stale cross-vault state from confusing the user.

### TEST-APP-UI-003: Default view on vault selection is Dashboard

**Artifact:** App Spec v0.4, Section 5 (Vault Dashboard)
**Category:** navigation

**Decision:** Dashboard is the default view when a vault is selected.

**Precondition:** Application loaded.

**Input:** Initial page load with a vault configured.

**Expected:** Dashboard view is displayed.

**Rationale:** The Dashboard provides the at-a-glance vault summary that orients
the user before drilling into specific operations.

---

## 2. Vault Dashboard

### TEST-APP-UI-004: Dashboard displays vault identity

**Artifact:** App Spec v0.4, Section 5.1 (Vault Identity)
**Category:** dashboard

**Decision:** Vault name, description, and base path are displayed at the top
of the Dashboard.

**Precondition:** Vault loaded (e.g., example_vault).

**Input:** Navigate to Dashboard.

**Expected:**
- Vault name displayed (e.g., "Engineering Portfolio")
- Vault description displayed
- Base path (storage_root) displayed

**Rationale:** Vault identity confirms the user is looking at the correct vault,
especially when multiple vaults are configured.

### TEST-APP-UI-005: Dashboard displays all vault statistics

**Artifact:** App Spec v0.4, Section 5.2 (Vault Statistics)
**Category:** dashboard

**Decision:** Ten statistics are displayed: document count (total), documents by
lifecycle state, documents by doc_type, documents by source adapter, edge count
(total), edges by type, staging edge count, LanceDB size, SQLite size, last
ingestion timestamp.

**Precondition:** Vault loaded with documents and edges.

**Input:** Navigate to Dashboard.

**Expected:**
- Total document count shown
- Lifecycle breakdown shown (e.g., draft: 1, active: 5, archived: 1)
- Doc type breakdown shown
- Source adapter breakdown shown
- Total edge count shown
- Edge type breakdown shown (all seven types represented in labels)
- Staging edge count shown
- LanceDB disk size shown
- SQLite disk size shown
- Last ingestion timestamp shown

**Rationale:** Statistics provide the vault health overview without requiring
the user to navigate to individual views.

### TEST-APP-UI-006: Health indicators display counts and function as links

**Artifact:** App Spec v0.4, Section 5.3 (Health Indicators)
**Category:** dashboard, navigation

**Decision:** Each health indicator shows a count and links to the appropriate
view with relevant items pre-filtered.

**Precondition:** Vault with pending metadata, pending edges, deferred abstracts,
and failed ingestions.

**Input:** Render Dashboard.

**Expected:**
- Pending metadata review shows count; clicking navigates to Review > Metadata tab
- Pending edge review shows count; clicking navigates to Review > Edge tab
- Deferred abstracts shows count
- Failed ingestions shows count with filename and diagnostic message

**Rationale:** Health indicators are action items. Linking directly to the
relevant view with pre-filtering eliminates manual navigation.

### TEST-APP-UI-007: Health indicator with zero count renders as non-link

**Artifact:** App Spec v0.4, Section 5.3 (Health Indicators)
**Category:** dashboard

**Decision:** When a health indicator count is zero, it is displayed as a static
label rather than a navigable link.

**Precondition:** Vault with no pending metadata items.

**Input:** Render Dashboard.

**Expected:**
- Pending metadata review shows "0" as plain text (not a link)
- Other indicators with non-zero counts remain navigable

**Rationale:** Zero-count links are dead ends. Displaying them as static text
avoids unnecessary navigation.

### TEST-APP-UI-008: Adapter registry displays available adapters

**Artifact:** App Spec v0.4, Section 5.4 (Adapter Registry)
**Category:** dashboard

**Decision:** Adapter name, supported file extensions, and adapter version are
displayed for each configured adapter.

**Precondition:** Vault with markdown, docx, and pdf adapters configured.

**Input:** Render Dashboard.

**Expected:**
- Three adapter rows displayed
- Each row shows adapter name, extensions (e.g., ".docx"), and version
- Only enabled adapters are shown

**Rationale:** Users need to confirm which file types can be ingested before
initiating a directory scan.

### TEST-APP-UI-009: Dashboard is read-only

**Artifact:** App Spec v0.4, Section 5 (Vault Dashboard)
**Category:** dashboard

**Decision:** The Dashboard displays information but does not support editing
operations.

**Precondition:** Dashboard loaded.

**Input:** Inspect the rendered Dashboard for any editable controls (text inputs,
dropdowns, toggle switches, or mutation buttons other than health links).

**Expected:** No editable controls are present. Only display elements and
navigation links.

**Rationale:** The Dashboard is an overview. Mutation operations belong in their
respective views (Ingest, Review, Document Detail).

---

## 3. Ingest View

### TEST-APP-UI-010: Step 1 -- directory path input and validation

**Artifact:** App Spec v0.4, Section 6.1 (Directory Input)
**Category:** ingest

**Decision:** The user enters an absolute filesystem path. The application
validates existence and readability server-side before proceeding to scan.

**Precondition:** Ingest view displayed.

**Input:** Enter an invalid path (e.g., `/nonexistent/path`) and click Scan.

**Expected:**
- Inline error message displayed (e.g., "Directory not found or not readable")
- View remains on Step 1 (does not advance to Step 2)

**Rationale:** Server-side validation is required because the browser cannot
access the local filesystem. Early validation prevents a wasted scan attempt.

### TEST-APP-UI-011: Step 1 -- valid path advances to Step 2

**Artifact:** App Spec v0.4, Section 6.1 (Directory Input)
**Category:** ingest

**Decision:** A valid, readable directory path advances the workflow to Step 2
(Scan Preview).

**Precondition:** Ingest view displayed. A valid directory exists on the
filesystem.

**Input:** Enter the valid directory path and click Scan.

**Expected:**
- View advances to Step 2
- Scan results begin populating

**Rationale:** The scan is the first server-side operation; it requires a valid
path to proceed.

### TEST-APP-UI-012: Step 2 -- scan preview displays summary bar

**Artifact:** App Spec v0.4, Section 6.2 (Scan Preview)
**Category:** ingest

**Decision:** A summary bar shows total files found, files with a matching
adapter, and files with no matching adapter.

**Precondition:** Scan completed for a directory containing mixed file types.

**Input:** View Step 2 after scan completes.

**Expected:**
- Summary bar shows three counts: total files, files with adapter, files without
- Counts are consistent with the table below

**Rationale:** The summary bar provides at-a-glance scope before the user
inspects individual files.

### TEST-APP-UI-013: Step 2 -- scan table columns and status values

**Artifact:** App Spec v0.4, Section 6.2 (Scan Preview)
**Category:** ingest

**Decision:** Table columns are filename, file size, detected adapter, and
status. Status is one of: New, Modified, Unchanged, No adapter.

**Precondition:** Scan completed for a directory with files in all four status
categories.

**Input:** View Step 2 scan table.

**Expected:**
- Four columns present: filename, size, detected adapter, status
- Status column shows exactly one of: New, Modified, Unchanged, No adapter
- New files show the adapter name; No adapter files show null/empty adapter

**Rationale:** The four status values encode the full decision matrix: hash
presence x hash match x adapter match.

### TEST-APP-UI-014: Step 2 -- default selection is New and Modified only

**Artifact:** App Spec v0.4, Section 6.2 (Scan Preview)
**Category:** ingest

**Decision:** New and Modified files are selected by default. Unchanged and
No adapter files are deselected.

**Precondition:** Scan completed with files in all four status categories.

**Input:** View Step 2 scan table.

**Expected:**
- Checkboxes for New files: checked
- Checkboxes for Modified files: checked
- Checkboxes for Unchanged files: unchecked
- Checkboxes for No adapter files: unchecked

**Rationale:** Re-ingesting unchanged files is a no-op. Files without adapters
cannot be ingested. Defaulting to New + Modified minimizes unnecessary work.

### TEST-APP-UI-015: Step 2 -- user can toggle individual file selection

**Artifact:** App Spec v0.4, Section 6.2 (Scan Preview)
**Category:** ingest

**Decision:** Users can select or deselect individual files using checkboxes.

**Precondition:** Scan table displayed with multiple files.

**Input:** Toggle the checkbox on an Unchanged file to select it; toggle a New
file to deselect it.

**Expected:**
- Unchanged file becomes selected (checkbox checked)
- New file becomes deselected (checkbox unchecked)
- "Ingest Selected" button count updates to reflect the change

**Rationale:** Manual override allows the user to re-ingest an unchanged file
(e.g., after an adapter upgrade) or skip a new file.

### TEST-APP-UI-016: Step 3 -- progress indicator shows current file and stage

**Artifact:** App Spec v0.4, Section 6.3 (Ingestion Progress)
**Category:** ingest

**Decision:** Progress shows current file name, pipeline stage (projection,
indexing, abstraction), and overall count (e.g., "7 of 23 files").

**Precondition:** Ingestion in progress.

**Input:** View Step 3 during active ingestion.

**Expected:**
- Current filename displayed
- Current pipeline stage displayed (one of: projection, indexing, abstraction)
- Overall progress displayed as "N of M files"

**Rationale:** Per-file, per-stage progress lets the user gauge both position
and velocity (abstraction is slower than projection).

### TEST-APP-UI-017: Step 3 -- scrolling log shows per-file status

**Artifact:** App Spec v0.4, Section 6.3 (Ingestion Progress)
**Category:** ingest

**Decision:** A scrolling log area shows per-file status messages including
warnings and errors.

**Precondition:** Ingestion in progress with at least one warning or error.

**Input:** View Step 3 during ingestion.

**Expected:**
- Log area scrolls as new messages appear
- Successful files show completion status
- Files with warnings show warning text
- Files with errors show error diagnostic

**Rationale:** The log provides an audit trail during long-running batch
ingestions, especially for diagnosing adapter failures.

### TEST-APP-UI-018: Step 3 -- cancel stops remaining ingestion

**Artifact:** App Spec v0.4, Section 6.3 (Ingestion Progress)
**Category:** ingest

**Decision:** Cancel stops the remaining ingestion. Files already ingested are
retained; files not yet processed are skipped.

**Precondition:** Ingestion in progress with multiple files remaining.

**Input:** Click Cancel during ingestion.

**Expected:**
- Ingestion stops after the currently-in-progress file completes (or aborts)
- Files already ingested remain in the vault
- Files not yet processed are skipped
- View advances to Step 4 (Results Summary) with partial results

**Rationale:** Cancellation is non-destructive. Already-completed work is
preserved to avoid wasting the time already spent.

### TEST-APP-UI-019: Step 4 -- results summary displays all categories

**Artifact:** App Spec v0.4, Section 6.4 (Results Summary)
**Category:** ingest

**Decision:** Results summary shows: documents created (new vs. new-version),
metadata pending, edges inferred (Tier 1 auto-created by type, Tier 2 staged
by type), abstracts (generated vs. deferred), and errors.

**Precondition:** Ingestion completed.

**Input:** View Step 4.

**Expected:**
- Documents created count with new vs. new-version breakdown
- Metadata pending count (documents with review_required)
- Edge counts: Tier 1 auto-created by type, Tier 2 staged by type
- Abstract counts: generated vs. deferred
- Error count with expandable detail

**Rationale:** The summary captures all pipeline outcomes in a single view,
serving as both a completion report and a triage starting point.

### TEST-APP-UI-020: Step 4 -- review links navigate to Review with correct tab

**Artifact:** App Spec v0.4, Section 6.4 (Results Summary)
**Category:** ingest, navigation

**Decision:** When metadata or edge review items are pending, the summary
includes links to the Review view with the relevant sub-tab pre-selected.

**Precondition:** Ingestion completed with pending metadata and staging edges.

**Input:** Click the metadata review link in the results summary.

**Expected:**
- Navigation to Review view
- Metadata Review sub-tab is pre-selected

**Input:** Click the edge review link.

**Expected:**
- Navigation to Review view
- Edge Review sub-tab is pre-selected

**Rationale:** Direct links from results to review eliminate manual navigation
after every ingestion cycle.

---

## 4. Metadata Review

### TEST-APP-UI-021: Review queue table shows one row per document

**Artifact:** App Spec v0.4, Section 7.1 (Review Queue)
**Category:** metadata_review

**Decision:** Each row displays the document's source filename and extracted
metadata fields with extraction source labels.

**Precondition:** Vault with pending metadata items (e.g., 2 documents).

**Input:** Navigate to Review > Metadata tab.

**Expected:**
- Table has exactly 2 rows (one per document with pending metadata)
- Each row shows source filename
- Each metadata field shows value and source label (filename, content, or default)

**Rationale:** One-row-per-document keeps the review queue scannable and
groups related fields for per-document confirmation.

### TEST-APP-UI-022: Conflicting extraction sources show both values

**Artifact:** App Spec v0.4, Section 7.1 (Review Queue)
**Category:** metadata_review

**Decision:** When filename and content extraction produce different values,
both are shown with the winning value (content) highlighted.

**Precondition:** Document with conflicting metadata extraction (e.g., title
differs between filename and content).

**Input:** View the metadata row for that document.

**Expected:**
- Both values displayed (filename-extracted and content-extracted)
- Content-extracted value is visually highlighted as the winner
- Source labels ("filename", "content") are shown for each value

**Rationale:** Showing both values with precedence highlighting lets the user
verify the auto-resolution or override it before confirming.

### TEST-APP-UI-023: Inline editing overrides extracted values

**Artifact:** App Spec v0.4, Section 7.2 (Editing and Confirmation)
**Category:** metadata_review

**Decision:** Metadata fields are editable inline. Manual edits take highest
precedence over both filename and content extraction.

**Precondition:** Document with extracted metadata displayed in review queue.

**Input:** Edit the title field inline to a new value, then confirm the document.

**Expected:**
- Edited value is saved as the document's metadata
- The manually-entered value takes precedence over all extraction sources
- Document is removed from the review queue after confirmation

**Rationale:** The user is the ultimate authority on metadata correctness.
Manual edits must override automated extraction.

### TEST-APP-UI-024: Confirm per-document removes row from queue

**Artifact:** App Spec v0.4, Section 7.2 (Editing and Confirmation)
**Category:** metadata_review

**Decision:** Confirming a single document finalizes its metadata and removes
it from the review queue.

**Precondition:** Review queue with 3 documents.

**Input:** Click Confirm on the first document.

**Expected:**
- First document's metadata is finalized (API call to update_metadata)
- First document row disappears from the queue
- Remaining 2 documents still shown

**Rationale:** Per-document confirmation allows selective review without forcing
batch decisions.

### TEST-APP-UI-025: Confirm All finalizes all documents in queue

**Artifact:** App Spec v0.4, Section 7.2 (Editing and Confirmation)
**Category:** metadata_review

**Decision:** Confirm All finalizes metadata for every document in the queue.

**Precondition:** Review queue with 3 documents.

**Input:** Click Confirm All.

**Expected:**
- All 3 documents' metadata is finalized
- Queue becomes empty
- Dashboard health indicator updates to 0

**Rationale:** Batch confirmation is efficient when the user has reviewed
all items and trusts the extracted values.

---

## 5. Edge Review

### TEST-APP-UI-026: Staging edges grouped by edge type

**Artifact:** App Spec v0.4, Section 8.1 (Grouping and Display)
**Category:** edge_review

**Decision:** Tier 2 suggested edges are grouped by type (covers, derived_from,
bundles_with). Tier 1 (supersedes) and Tier 3 (authoritative_for, depends_on,
sync_target) do not appear in the edge review.

**Precondition:** Vault with staging edges of types covers, derived_from, and
bundles_with.

**Input:** Navigate to Review > Edge tab.

**Expected:**
- Edges grouped under type headings
- Only Tier 2 types shown (covers, derived_from, bundles_with)
- No supersedes edges (Tier 1, auto-created)
- No authoritative_for, depends_on, or sync_target edges (Tier 3, manual-only)

**Rationale:** Tier 1 edges are auto-created (no review needed). Tier 3 edges
are created manually in Document Detail. The review queue handles only the
inference-produced Tier 2 edges.

### TEST-APP-UI-027: Edge display shows source, target, evidence, confidence

**Artifact:** App Spec v0.4, Section 8.1 (Grouping and Display)
**Category:** edge_review

**Decision:** Each edge shows source document, target document, inference
evidence, and confidence tier.

**Precondition:** Vault with staging edges.

**Input:** View an edge entry in the Edge Review tab.

**Expected:**
- Source document title displayed
- Target document title displayed
- Inference evidence text displayed (e.g., "filename contains report code CD-04")
- Confidence tier displayed (always Tier 2 in this view)

**Rationale:** Evidence and confidence give the user enough context to make a
confirm/dismiss decision without navigating to the documents.

### TEST-APP-UI-028: Per-edge confirm moves edge to production table

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** edge_review

**Decision:** Confirming an edge moves it from the staging table to the
production edge table.

**Precondition:** Staging edge displayed.

**Input:** Click Confirm on a single edge.

**Expected:**
- Edge removed from staging table (disappears from review)
- Edge appears in production edge table (visible in Document Detail edge list)
- API call to confirm_staging_edge (or equivalent) made

**Rationale:** Production edges are the single source of truth for graph
operations. Confirmed edges must transition cleanly.

### TEST-APP-UI-029: Per-edge dismiss deletes edge from staging

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** edge_review

**Decision:** Dismissing an edge deletes it from the staging table.

**Precondition:** Staging edge displayed.

**Input:** Click Dismiss on a single edge.

**Expected:**
- Edge removed from staging table (disappears from review)
- Edge does not appear in production edge table
- API call to dismiss_staging_edge (or equivalent) made

**Rationale:** Dismissed edges are false positives. Deleting them prevents
re-review and keeps the staging table clean.

### TEST-APP-UI-030: Group-level Confirm All and Dismiss All

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** edge_review

**Decision:** Group-level actions confirm or dismiss all edges within a single
edge type group.

**Precondition:** Edge type group "covers" with 3 staging edges.

**Input:** Click Confirm All within the covers group.

**Expected:**
- All 3 covers edges moved to production table
- Covers group disappears from review
- Other edge type groups unaffected

**Input:** Click Dismiss All within the derived_from group.

**Expected:**
- All derived_from edges deleted from staging
- Other groups unaffected

**Rationale:** Group-level batch operations are efficient when the user trusts
an entire category of inference results.

### TEST-APP-UI-031: Global Confirm All confirms all staging edges

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** edge_review

**Decision:** A global Confirm All button confirms every staging edge across
all groups.

**Precondition:** Staging edges in multiple groups.

**Input:** Click global Confirm All.

**Expected:**
- All staging edges across all groups moved to production table
- Edge Review tab shows empty state
- Dashboard staging edge count updates to 0

**Rationale:** Global confirm is the fast path for users who review the
full list and accept all suggestions.

---

## 6. Search

### TEST-APP-UI-032: Search interface has text input, mode selector, and filters

**Artifact:** App Spec v0.4, Section 9.1 (Query Interface)
**Category:** search

**Decision:** The search UI presents a text input, a retrieval mode selector
(three modes), and optional filter controls.

**Precondition:** Search view displayed.

**Input:** Render Search view.

**Expected:**
- Text input field present
- Retrieval mode selector with three options: Semantic, Keyword, Hybrid
- Filter controls: doc_type dropdown, lifecycle state multi-select, metadata filters
- Default mode is Hybrid

**Rationale:** The three controls map directly to the SAGE discover endpoint
parameters (query, mode, filters).

### TEST-APP-UI-033: Search execution returns ranked results

**Artifact:** App Spec v0.4, Section 9.4 (Results Display)
**Category:** search

**Decision:** Results are displayed as a ranked list with title, doc_type,
lifecycle state, relevance score, and snippet.

**Precondition:** Vault with indexed documents.

**Input:** Enter a query and execute search.

**Expected:**
- Results displayed as a ranked list (highest relevance first)
- Each result shows: title (as a link), doc_type, lifecycle state, relevance score
- Snippet text displayed with query terms highlighted
- Semantic abstract shown below snippet (if available)

**Rationale:** The ranked list with snippets provides enough context for the
user to decide which document to drill into.

### TEST-APP-UI-034: Clicking a search result navigates to Document Detail

**Artifact:** App Spec v0.4, Section 9.4 (Results Display)
**Category:** search, navigation

**Decision:** Document titles in search results are links to Document Detail.

**Precondition:** Search results displayed.

**Input:** Click a document title in the results list.

**Expected:**
- Navigation to Document Detail view for the clicked document
- Document Detail displays the full metadata, projection, and edges

**Rationale:** Search is the primary entry point for finding documents;
navigation to detail must be a single click.

### TEST-APP-UI-035: Filters narrow search results pre-retrieval

**Artifact:** App Spec v0.4, Section 9.3 (Filters)
**Category:** search

**Decision:** Filters are applied as pre-retrieval constraints, affecting both
the result set and ranking.

**Precondition:** Vault with documents of multiple doc_types and lifecycle states.

**Input:** Set doc_type filter to "design_spec" and execute a search.

**Expected:**
- Only design_spec documents appear in results
- Results are ranked within the filtered set
- Filter value is passed to the SAGE discover endpoint as a scope parameter

**Rationale:** Pre-retrieval filtering is more efficient and produces better
rankings than post-filtering (which wastes result slots on excluded documents).

### TEST-APP-UI-036: Multiple lifecycle states selectable as filter

**Artifact:** App Spec v0.4, Section 9.3 (Filters)
**Category:** search

**Decision:** Multiple lifecycle states can be selected simultaneously.

**Precondition:** Vault with documents in draft, active, and archived states.

**Input:** Select "active" and "draft" lifecycle states, then search.

**Expected:**
- Results include documents in active and draft states
- Results exclude documents in archived states

**Rationale:** Common use case: "show me everything that's current" (draft +
active) excluding historical versions.

### TEST-APP-UI-056: Long snippet content is truncated to first and last 100 words

**Artifact:** App Spec v0.4, Section 9.4 (Results Display)
**Category:** search

**Decision:** When chunk_content exceeds 200 words, the search result snippet
displays the first 100 words and the last 100 words separated by an ellipsis.
Content of 200 words or fewer displays in full.

**Precondition:** Vault with indexed documents, at least one containing chunk
content longer than 200 words.

**Input:** Execute a search that returns a result with chunk_content exceeding
200 words.

**Expected:**
- Snippet shows exactly the first 100 words of the original content
- Followed by a Unicode ellipsis character (U+2026)
- Followed by exactly the last 100 words of the original content
- Content of 200 words or fewer is displayed unchanged

**Rationale:** Full document content overwhelms the results list and makes
scanning impractical. Head and tail excerpts preserve the document opening
(typically title/abstract context) and the most recent content, giving the user
enough signal to decide whether to drill in.

---

## 7. Document Detail

### TEST-APP-UI-037: Metadata panel organized by tier

**Artifact:** App Spec v0.4, Section 10.1 (Metadata Panel)
**Category:** document_detail

**Decision:** Metadata is organized by tier: Tier 1 (core), Tier 2 (vault-
configured), Tier 3 (source-type-specific), plus a separate provenance section.

**Precondition:** Document with metadata across all three tiers.

**Input:** Navigate to Document Detail for a document with Tier 3 metadata.

**Expected:**
- Tier 1 section: id, title, doc_type, lifecycle state, created_at, updated_at
- Tier 2 section: project, version_label, tags, authority_scope (vault-configured)
- Tier 3 section: adapter-specific fields (e.g., heading_styles, page_count)
- Provenance section: source_path, source_content_hash, adapter_version, projected_at

**Rationale:** Tiered organization reflects the metadata model's structure and
helps users distinguish system-generated from domain-specific fields.

### TEST-APP-UI-038: Projection preview renders heading hierarchy

**Artifact:** App Spec v0.4, Section 10.2 (Projection Preview)
**Category:** document_detail

**Decision:** The stored projection_text is rendered with heading hierarchy
intact. This is the adapter's structured text output, not the original source.

**Precondition:** Document with projection_text containing multiple heading levels.

**Input:** Navigate to Document Detail.

**Expected:**
- Projection text rendered as structured content
- Heading hierarchy preserved (h1, h2, etc.)
- Content is readable and maintains logical structure

**Rationale:** The projection is what SAGE indexed. Showing it confirms
what the retrieval system "sees" for this document.

### TEST-APP-UI-039: "Open Source File" button present

**Artifact:** App Spec v0.4, Section 10.2 (Projection Preview)
**Category:** document_detail

**Decision:** An "Open Source File" button opens the original source file in the
default application for its file type.

**Precondition:** Document with a valid source_path.

**Input:** Render Document Detail.

**Expected:**
- "Open Source File" button is visible
- Button is associated with the document's source_path

**Rationale:** Users need access to the original source for editing or
reference, not just the SAGE projection.

### TEST-APP-UI-040: Edge list grouped by type with linked titles

**Artifact:** App Spec v0.4, Section 10.3 (Edge List)
**Category:** document_detail

**Decision:** All production edges involving this document are listed, grouped
by type. Related document titles are linked to their Document Detail views.

**Precondition:** Document with edges of multiple types.

**Input:** Navigate to Document Detail for a document with edges.

**Expected:**
- Edges grouped by type (e.g., supersedes, derived_from, covers)
- Each edge shows the related document's title as a clickable link
- Clicking a related document navigates to its Document Detail

**Rationale:** Edges are the document's graph neighborhood. Grouped display
with navigation supports graph exploration from any node.

### TEST-APP-UI-041: "View in Graph" button navigates to Graph Explorer

**Artifact:** App Spec v0.4, Section 10.3 (Edge List)
**Category:** document_detail, navigation

**Decision:** A "View in Graph" button navigates to the Graph Explorer centered
on the current document.

**Precondition:** Document Detail displayed.

**Input:** Click "View in Graph".

**Expected:**
- Navigation to Graph Explorer view
- Graph is centered on the current document
- Default traversal depth (2) applied

**Rationale:** Graph Explorer provides the visual neighborhood view that the
edge list cannot convey.

### TEST-APP-UI-042: Manual edge creation dialog

**Artifact:** App Spec v0.4, Section 10.4 (Manual Edge Creation)
**Category:** document_detail

**Decision:** Tier 3 edges and user-created edges are added through a dialog
with edge type selector and target document search.

**Precondition:** Document Detail displayed.

**Input:** Open the manual edge creation dialog.

**Expected:**
- Dialog presents an edge type selector
- Edge type options include: authoritative_for, depends_on, sync_target (Tier 3)
  plus other types for manual creation
- Target document search field allows searching within the vault
- Submitting creates the edge and updates the edge list

**Rationale:** Tier 3 edges encode human-asserted relationships (authority,
dependency, sync) that inference cannot produce.

---

## 8. Graph Explorer

### TEST-APP-UI-043: Graph renders as node-link diagram

**Artifact:** App Spec v0.4, Section 11.1 (Visualization)
**Category:** graph_explorer

**Decision:** Documents are nodes, edges are links. Default layout is
hierarchical. Rendered using vis.js.

**Precondition:** Graph Explorer opened from a document with edges.

**Input:** View Graph Explorer.

**Expected:**
- Interactive node-link diagram rendered
- Center node is the originating document
- Connected documents visible within default traversal depth (2 hops)
- Layout is hierarchical by default

**Rationale:** Hierarchical layout reflects directional relationships (version
lineage, derivation), which are the most common navigation patterns.

### TEST-APP-UI-044: Node shape encodes doc_type

**Artifact:** App Spec v0.4, Section 11.1 (Visualization)
**Category:** graph_explorer

**Decision:** Node shape encodes document type. The specific shape-to-type
mapping is an implementation decision.

**Precondition:** Graph with documents of different doc_types.

**Input:** View Graph Explorer.

**Expected:**
- Nodes of different doc_types rendered with different shapes
- Legend or tooltip identifies the shape-to-type mapping

**Rationale:** Shape is a pre-attentive visual channel. Users can scan for
document types (e.g., report drafts) without reading labels.

### TEST-APP-UI-045: Edge dash pattern encodes edge type

**Artifact:** App Spec v0.4, Section 11.1 (Visualization)
**Category:** graph_explorer

**Decision:** Dash pattern is the primary edge type encoding. Color provides
secondary reinforcement.

**Precondition:** Graph with edges of different types.

**Input:** View Graph Explorer.

**Expected:**
- Different edge types rendered with different dash patterns
- Color provides secondary distinction
- Supersedes, covers, derived_from, bundles_with visually distinguishable

**Rationale:** Dash pattern remains distinguishable in grayscale and color-blind
contexts. Color reinforces but is not required for readability.

### TEST-APP-UI-046: Node opacity encodes lifecycle state

**Artifact:** App Spec v0.4, Section 11.1 (Visualization)
**Category:** graph_explorer

**Decision:** Active documents at full opacity. Archived documents are dimmed.

**Precondition:** Graph with active and archived documents.

**Input:** View Graph Explorer.

**Expected:**
- Active documents rendered at full opacity
- Archived documents rendered with reduced opacity (dimmed)
- Archived documents rendered with reduced opacity (dimmed)

**Rationale:** Opacity creates a natural foreground/background distinction,
foregrounding the currently-relevant documents.

### TEST-APP-UI-047: Hover tooltip shows title, doc_type, lifecycle state

**Artifact:** App Spec v0.4, Section 11.2 (Interaction Model)
**Category:** graph_explorer

**Decision:** Hovering over a node displays a tooltip with title, doc_type,
and lifecycle state.

**Precondition:** Graph rendered.

**Input:** Hover over a node.

**Expected:**
- Tooltip appears showing document title, doc_type, and lifecycle state
- Tooltip disappears when hover ends

**Rationale:** Tooltips provide identification without requiring click
interaction, supporting rapid graph scanning.

### TEST-APP-UI-048: Click selects node and shows summary panel

**Artifact:** App Spec v0.4, Section 11.2 (Interaction Model)
**Category:** graph_explorer

**Decision:** Clicking a node selects it and displays a summary panel with
title, doc_type, lifecycle state, and edge count.

**Precondition:** Graph rendered.

**Input:** Click a node.

**Expected:**
- Node visually selected (highlight or border change)
- Summary panel appears alongside the graph
- Panel shows: title, doc_type, lifecycle state, edge count

**Rationale:** The summary panel provides more detail than the tooltip without
navigating away from the graph view.

### TEST-APP-UI-049: Double-click navigates to Document Detail

**Artifact:** App Spec v0.4, Section 11.2 (Interaction Model)
**Category:** graph_explorer, navigation

**Decision:** Double-clicking a node navigates to that document's Document
Detail view.

**Precondition:** Graph rendered.

**Input:** Double-click a node.

**Expected:**
- Navigation to Document Detail view for the double-clicked document

**Rationale:** Double-click is the standard "open" gesture. It provides a
direct path from visual exploration to full document inspection.

### TEST-APP-UI-050: Traversal depth slider controls graph scope

**Artifact:** App Spec v0.4, Section 11.3 (Controls)
**Category:** graph_explorer

**Decision:** A slider controls traversal depth (number of hops from center
node). Default is 2.

**Precondition:** Graph rendered with center node having neighbors at depths
1, 2, and 3.

**Input:** Move slider from 2 to 3.

**Expected:**
- Graph expands to include nodes at 3 hops from center
- Slider value displayed (e.g., "Depth: 3")

**Input:** Move slider from 3 to 1.

**Expected:**
- Graph contracts to show only 1-hop neighbors
- Nodes beyond 1 hop disappear

**Rationale:** Depth control lets users manage visual complexity. Shallow depth
for focused inspection, deeper for relationship discovery.

### TEST-APP-UI-051: Edge type filter hides edges and orphaned nodes

**Artifact:** App Spec v0.4, Section 11.3 (Controls)
**Category:** graph_explorer

**Decision:** Unchecking an edge type hides those edges and any nodes reachable
only through hidden edges.

**Precondition:** Graph with supersedes and derived_from edges, where some
nodes are reachable only via supersedes.

**Input:** Uncheck "supersedes" in the edge type filter.

**Expected:**
- Supersedes edges hidden
- Nodes reachable only via supersedes edges disappear
- Nodes reachable via other edge types remain

**Rationale:** Filtering by edge type isolates specific relationship categories.
Hiding orphaned nodes prevents visual clutter from disconnected nodes.

### TEST-APP-UI-052: Lifecycle state filter hides documents in unchecked states

**Artifact:** App Spec v0.4, Section 11.3 (Controls)
**Category:** graph_explorer

**Decision:** Unchecking a lifecycle state hides documents in that state.

**Precondition:** Graph with active and archived documents.

**Input:** Uncheck "archived" in the lifecycle state filter.

**Expected:**
- Archived documents disappear from graph
- Edges to/from hidden documents also hidden
- Active documents and their mutual edges remain

**Rationale:** Lifecycle filtering lets users focus on current documents without
historical version clutter.

### TEST-APP-UI-053: Layout toggle switches between hierarchical and force-directed

**Artifact:** App Spec v0.4, Section 11.3 (Controls)
**Category:** graph_explorer

**Decision:** A toggle switches between hierarchical (default) and
force-directed layout.

**Precondition:** Graph rendered in hierarchical layout.

**Input:** Click layout toggle to switch to force-directed.

**Expected:**
- Graph re-renders using force-directed layout
- Same nodes and edges, different spatial arrangement
- Toggle indicates current layout mode

**Input:** Click toggle again.

**Expected:**
- Graph returns to hierarchical layout

**Rationale:** Hierarchical layout suits directional relationships; force-directed
layout reveals clusters and lateral connections. Both are useful for different
exploration goals.

### TEST-APP-UI-054: Re-center button rebuilds graph from selected node

**Artifact:** App Spec v0.4, Section 11.3 (Controls)
**Category:** graph_explorer

**Decision:** Re-center rebuilds the graph with the currently selected node as
the new center.

**Precondition:** Graph rendered. A non-center node is selected (clicked).

**Input:** Click Re-center.

**Expected:**
- Graph rebuilds with the selected node as the new center
- Traversal depth applies from the new center
- Nodes outside the new traversal scope disappear
- New center node positioned centrally

**Rationale:** Re-centering allows exploring the graph outward from any node,
not just the original entry point.

### TEST-APP-UI-055: Pan and zoom via standard mouse/trackpad controls

**Artifact:** App Spec v0.4, Section 11.2 (Interaction Model)
**Category:** graph_explorer

**Decision:** Standard mouse/trackpad controls for pan and zoom.

**Precondition:** Graph rendered.

**Input:** Scroll to zoom; click-drag on background to pan.

**Expected:**
- Scroll wheel (or trackpad pinch) zooms in/out
- Click-drag on graph background pans the view
- Node positions maintained relative to each other

**Rationale:** Standard controls require no learning curve. vis.js provides
these natively.

---

## 9. Authentication Gate

The SPA runs an auth gate before loading any data. It keys on the three-way
discriminator from `GET /app/auth/me`: `200 {authenticated:true}` renders the
app, `200 {authenticated:false}` renders the sign-in interstitial (cloud
profile, no session), and `503 auth_not_configured` renders the app directly
(local profile, auth not in play). Implemented by `useSession` + `<SignIn>`,
sequenced ahead of `refreshVaults()` in `App.tsx`.

### TEST-APP-AUTH-001: Unauthenticated load shows the interstitial and skips the vault fetch

**Artifact:** CAS-ADR-042 (deployment profiles); BFF auth surface
**Category:** auth

**Decision:** When `GET /app/auth/me` reports `authenticated:false`, the app
renders the "Sign in with Microsoft" interstitial and does not call the vault
list — the shell never mounts before there is a session.

**Precondition:** Cloud profile, no live session.

**Input:** Load the app.

**Expected:**
- The interstitial sign-in screen renders.
- The vault selector (combobox) is absent.
- `listVaults` is never called.

**Rationale:** An unauthenticated user must get a sign-in affordance, not a
JSON-parse error from an unauthorized data call.

### TEST-APP-AUTH-002: Local profile renders the app with no sign-in and no sign-out

**Artifact:** CAS-ADR-042 (deployment profiles)
**Category:** auth

**Decision:** A `503 auth_not_configured` from `GET /app/auth/me` is the local
profile: render the app shell directly, with no interstitial and no sign-out
control (there is no session to end). This must be distinguished from
`authenticated:false`.

**Precondition:** Local profile (auth not configured).

**Input:** Load the app.

**Expected:**
- The app shell renders (vault selector present).
- No interstitial is shown.
- No "Sign out" control is shown.

**Rationale:** The local profile has no identity provider; forcing a sign-in
screen there would make the app unusable locally.

### TEST-APP-AUTH-003: "Sign in with Microsoft" begins the Entra authorization flow

**Artifact:** BFF auth surface
**Category:** auth

**Decision:** Clicking "Sign in with Microsoft" fetches the authorization URL
from `GET /app/auth/login` and navigates the browser to it. A failed challenge
fetch surfaces an error in place rather than dead-ending.

**Precondition:** Interstitial rendered.

**Input:** Click "Sign in with Microsoft".

**Expected:**
- The browser navigates to the returned `authorization_url`.
- On a challenge-fetch failure, an error message is shown and no navigation
  occurs.

**Rationale:** An interstitial (rather than an auto-redirect) gives
cancellation and callback errors a surface and avoids a redirect loop.

### TEST-APP-AUTH-004: A mid-session expiry returns the user to sign-in

**Artifact:** BFF session TTL
**Category:** auth

**Decision:** A `401 auth_required` from any data call mid-session re-triggers
the gate, returning the user to the interstitial. Signing out does the same
deliberately (ends the session, then re-gates).

**Precondition:** Authenticated session; the server-side session then lapses
(8h TTL) or the user clicks "Sign out".

**Input:** A data call returns `401 auth_required`, or the user clicks
"Sign out".

**Expected:**
- The interstitial sign-in screen is shown again.
- The vault selector is no longer present.

**Rationale:** An expired session should return the user to sign-in, not leave a
dead error banner on screen.
