# JSON source fidelity

Structured-data adapter `0.4.0` projects JSON from an ordered occurrence tree.
Each object member survives in authored order, including repeated decoded names
at any nesting depth. Arrays retain their order. Numeric tokens retain their exact
source spelling, including large integers, long decimals, exponent signs/case,
trailing zeros and negative zero; projection never converts them through a binary
float. Pretty-printing and blank lines between container records may change.
Concatenating stored passage content in passage order reproduces the projection.

Strings and keys decode JSON escapes into searchable Unicode, then use valid JSON
escaping when emitted. Equivalent spellings such as `"note"` and `"\u006eote"`
remain separate member occurrences with the same decoded name. Escaped quotes,
backslashes and control characters remain valid JSON. Valid surrogate pairs become
Unicode characters; unpaired surrogate code units remain `\uXXXX` escapes so output
and native evidence can travel as strict UTF-8. Retained source bytes and their hash,
rather than whitespace or escape spelling in the projection, establish source identity.
JSON Lines keeps its existing lexical passage text and record separation, and now
uses the same occurrence-preserving native inspection parser.

Native JSON/JSONL inspection uses the same tree. Numbers are distinct `JsonNumber`
lexical tokens, not rounded Python floats; ordinary string values remain decoded strings. Native JSON string renderings use
JSON string tokens (including quotes), keeping numeric tokens and escaped surrogate
units distinct from lookalike string content during selection. JSON key labels in
contexts also use quoted JSON tokens, so controls and literal escape text stay
distinct and safe in stored evidence. Orientation titles escape NUL and unpaired
surrogates for storage; the lexical projection/evidence retain the exact string value.
Numeric syntax can supply typed inventory observations, not a fabricated numeric
quotation. Duplicate member locations add `{member:N}` (zero-based member ordinal)
to the decoded-key path; descendants inherit that occurrence identity. Field paths
still group equivalent authored fields. Title derivation preserves historical metadata precedence: use the last title
occurrence if it is a non-empty string, else the last name, else the last info
object's last title. A numeric token never becomes a title. This is orientation metadata, not a claim
that other title occurrences are absent.

Inspection, selection and abstraction evidence retain their existing independent
budgets. Numeric inventory does not require model calls. Preserved text supplies
bounded authored excerpts; selection may omit occurrences with honest limitations.
No abstract-faithfulness guarantee follows from lexical fidelity. Adapter version,
source hash and frozen selection configuration distinguish the new evidence.
Existing source-byte, parser depth/node and inference ceilings remain unchanged.
Strict JSON refuses NaN and Infinity; their previous acceptance was a permissive
Python-parser behavior that could produce invalid JSON. This correction uses the
data-integrity exemption from deprecation.

Keyword indexing consumes authored readable passages, independently of embeddings.
Under complete keyword coverage both values of a repeated member can admit a match,
including late nested occurrences, even after sparse or failed embedding. Explicit
keyword-character limits can exclude later passages while readable text remains
complete. PostgreSQL's simple text parser and tsquery matching determine numeric
search behavior; intact numeric content does not promise arbitrary byte-substring
matching. Derived abstracts and inventory cannot independently admit a result.

## Existing records and operator action

Deploying this adapter does not repair or restamp historical projections, inspection,
abstracts or coverage. Older adapter identities remain historical evidence; a
`complete` projection/keyword scope records the old attempt's extent and is not a
retroactive guarantee of occurrence or number fidelity. To repair selected records,
an operator must explicitly authorize retained-source reprojection and keyword
reindexing through the ordinary reprocessing procedure. Confirm retained bytes and
hashes, and inspect the new adapter identity, readable passages and coverage afterward.
Changed selection identities retain cumulative inference controls; authorize any
needed scope migration separately instead of resetting consumption. An embedding-only
reindex cannot restore content already lost from stored passages. No corpus-wide
reprocessing is implied by this change.
