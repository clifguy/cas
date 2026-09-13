# Releasing the Formal Substrate

CAS-ADR-008 fixes the version line. An annotated `vMAJOR.MINOR.0` tag fixes
`MAJOR.MINOR`, and `PATCH` is the commit distance from that tag. Every change is
a patch unless it crosses a minor boundary. A change that crosses one is followed
by a new minor release. This document is the procedure. Record format and
authoring rules are in `docs/fs/changes/README.md`. Which changes cross which
boundary is enumerated in *CAS Release Classification* (CAS vault, steering
document).

## Is a release due?

```sh
.venv/bin/python -m scripts.substrate_changes status
```

`status` reports:

- the last release tag;
- every unreleased change record and its classification;
- whether a minor release is due, which is the case when any record is `minor`;
- the contract comparison against the last tag;
- whether a landed release still lacks its tag.

Concurrent minor changes that land before a minor is cut share that release.

A patch needs no release act, because its version is derived. Patch records
accumulate until the next minor release folds them.

## Cutting a minor release

1. **Prepare**, on a branch from an up-to-date default branch with a clean tree:

   ```sh
   git switch -c release-2.4 origin/main
   .venv/bin/python -m scripts.substrate_changes release-prepare
   ```

   The step refuses, and changes nothing, when:
   - no record is `minor`;
   - the contract comparison finds minor-level change that no `minor` record
     covers;
   - a record is invalid;
   - an earlier release still lacks its tag.

   Otherwise it does four things:
   - folds every unreleased record, in file-name order, into a new
     `revision_history` entry `{release, date, changes}`;
   - sets each manifest artifact changed since the last tag to the new version,
     together with both OpenAPI specifications;
   - sets both specifications' `info.version`, editing that one line only;
   - deletes the record files.

   Pass `--major` only for a deliberate major. A major is an owner decision and is
   never the automatic consequence of a category.

2. **Land it.** Commit, open a pull request, and merge it like any other change.
   The change-record gate recognizes a release: it requires no record of its own,
   and it requires the release to fold exactly the records it deletes and to
   change no contract.

3. **Tag it**, on the default branch after the merge:

   ```sh
   git switch main && git pull --ff-only
   .venv/bin/python -m scripts.substrate_changes release-tag --push
   ```

   The tag goes on the first-parent commit that introduced the release entry, even
   if later changes have landed on top. Its message carries the release's
   caller-facing notes. Without `--push`, the tag is created locally only.

## Why the tag comes last

The contract-version gate (`test_openapi_info_version_matches_api_version`)
checks three versions against each other:
- the live OpenAPI document;
- both committed specifications;
- the `API_VERSION` that `git describe` derives from the tags.

A release change declares the next version in the specifications. The tag for
that version belongs on the commit the change becomes when it lands, and that
commit exists only after the merge.

The gate therefore makes one allowance. The specifications may declare the
manifest's newest release while that release's tag does not exist, and at no
other time. The release change lands green, and the tag follows. From then on the
check is as strict as before:
- a tag that exists but is not an ancestor of the build fails;
- a release behind the derived version fails;
- a specification ahead of both fails.

The rule is `contract_version_ok` in `scripts/substrate_changes.py`, which the
gate calls, so the tooling and the gate cannot disagree.

Between the merge and the tag, builds from the default branch still report the
previous minor. Tag promptly: `status` and the release step both flag a release
waiting for its tag, and the next release cannot be prepared until the tag exists.

Before this procedure existed, the order was the reverse: tag first, then a
change moving both specifications. That order made the pull request that moved
the version unable to go green until the tag was pushed. The procedure above
replaces it.
