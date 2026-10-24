# Workbook cell coverage

The XLSX adapter projects stored cells in workbook/sheet/XML order, including hidden
sheets, rows and columns. Every displayed cell carries its Excel coordinate and type.
Sheet names remain exact section addresses, including punctuation and markup. A section
may contain many passages; section and projection reads rejoin them in source order.
Stored dimensions are the observed cell envelope, not Excel's declared dimension.

Strings retain whitespace, Unicode, markup and line breaks. Numbers retain OOXML
lexical value text instead of being rounded through binary floating point. Booleans,
errors, dates/times (decoded with the workbook epoch), explicit blanks and absent cells
stay distinct. Merged ranges are orientation; their anchor value is emitted once.
Formula text is authored evidence. Array/data-table formula type, reference range and
other OOXML formula attributes remain generated display orientation; a data-table
formula with no expression has no invented authored formula text. A retained cached result is displayed as an
**unverified formula cache**, excluded from authored keyword and inference evidence.
No formula evaluation, external link fetching or macro execution occurs.

This is cell coverage, not a visual workbook rendition. Charts, drawings, images,
comments, text boxes, VBA and other non-cell objects remain in the retained source.
The original file and its digest remain authoritative.

## Limits and compatibility

Canonical projection is full by default. `preview_rows` remains a positive integer
accepted for compatibility but does not truncate canonical content. This immediate
caller adaptation claims the data-integrity exemption: the previous canonical preview
silently omitted authored cells. A UI preview may be shorter as presentation only.
`max_sheets: null` includes all sheets; a positive cap selects the first sheets and
reports limited projection/keyword coverage when any are omitted.

Package decompression limits and `discovery_facts` source-byte/XML-depth/XML-node
admission limits apply before allocation. For XLSX, `max_rows` bounds stored row
records across the package and `max_columns` bounds stored cells per row, not the
coordinate envelope. Thus a cell at XFD1048576 does not create millions of gaps.
Canonical output also has a fixed conservative 16 Mi Unicode-character admission
ceiling, including per-cell orientation allowances and repeated shared strings. It is
checked before building repeated display text; a small compressed package does not
permit unbounded shared-string expansion. An exceeded parser/output limit refuses
the source rather than claiming complete coverage
of an unread remainder. There is no new throughput guarantee.

Readable projection and keyword publication precede embedding. Keyword character
caps remain explicit and independent; sparse absent vectors are preserved. Existing
selection, cumulative inference allowances, provider backpressure and generation
fences continue to apply. Oversized cell values remain readable; selection may refuse
such a cell as inference evidence under its normal input/candidate limit.

## Existing vaults

Bootstrap adds nullable `chunks.keyword_content`; legacy null values continue to
index the unchanged stored text. The generated keyword vector must then be rebuilt
through the existing `migrate_vault` operation. This takes the existing transaction/
locking path and may rewrite the passage table and its indexes; schedule it as an
operator maintenance action. It is idempotent. It invalidates keyword coverage for
rewritten index generations without asserting complete source coverage.

Until that rebuild, a new canonical cell pipeline settles as failed with
`pipeline_error` beginning `keyword_index_migration_required`. The service retains
the source and any previously served generation. Both inline and queued REST/MCP
pipelines use this same failure protocol. After migration, explicitly recompute that
failed document under the normal recovery/allowance rules.

An adapter version change does not automatically reprocess historical XLSX previews.
Historical limited/unknown coverage remains as recorded. Generic passage/preamble
repairs do not upgrade workbook previews or erase separate authored evidence. An
operator who wants full coverage must explicitly request source reprocessing; no
bulk corpus reprocessing is implied by deploying this change or rebuilding vectors.

## Acceptance still requiring a live operator run

Use a disposable workbook with a hidden later sheet, unique text after row five,
XFD1048576, repeated numeric values and a deliberately stale formula cache. Record
source digest, served scope/generation, provider call/input/output tokens and vector
population. Verify REST, MCP and app projection/section/search results, including
negative generated-label/cache searches. Repeat with an explicit sheet/keyword cap
and a failed provider call. Local/cloud smoke initiation, service restart, deployment
and acceptance are separate owner actions; isolated automated integration results
do not establish hosted acceptance.
