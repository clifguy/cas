"""Report class-bounded calibration eligibility; never promotes a runtime check.

Labels and blinding are asserted evidence requiring independent owner acceptance.
Missing labels, duplicate samples, unknown safety or insufficient provider/source
coverage keep every class in recording posture. No vault or provider calls occur.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

from sage.abstraction_policy import implementation_digest
from sage.services import abstraction_publication as publication


def binomial_interval(errors: int, total: int) -> tuple[float, float]:
    """Two-sided exact 95% Clopper-Pearson interval, including empty populations."""
    if not 0 <= errors <= total:
        raise ValueError("invalid binomial population")
    if total == 0:
        return 0.0, 1.0

    def cdf(k: int, p: float) -> float:
        if p <= 0:
            return 1.0
        if p >= 1:
            return float(k >= total)
        return min(
            1.0,
            math.fsum(
                math.exp(
                    math.lgamma(total + 1)
                    - math.lgamma(i + 1)
                    - math.lgamma(total - i + 1)
                    + i * math.log(p)
                    + (total - i) * math.log1p(-p)
                )
                for i in range(k + 1)
            ),
        )

    def root(k: int, target: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if cdf(k, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    return (
        0.0 if errors == 0 else root(errors - 1, 0.975),
        1.0 if errors == total else root(errors, 0.025),
    )


def calibration_report(manifest: dict) -> dict:
    """Freeze, partition and score all declared samples; unknown never means passed."""
    cases = manifest.get("cases")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(cases, list)
        or not 1 <= len(cases) <= 10000
    ):
        raise ValueError("calibration requires schema v1 and 1..10000 cases")
    implementation = implementation_digest()
    issues = []
    if manifest.get("implementation_sha256") != implementation:
        issues.append("implementation_not_frozen_at_current_bytes")
    if manifest.get("candidate_stage") != "post_licensed_repairs":
        issues.append("candidate_stage_not_shared_publication_seam")
    if manifest.get("labels_status") != "independently_adjudicated":
        issues.append("independent_labels_missing")
    adjudicators = manifest.get("adjudicators", [])
    if (
        not isinstance(adjudicators, list)
        or not all(isinstance(a, str) and a.strip() for a in adjudicators)
        or len(set(adjudicators)) < 2
        or manifest.get("blinded_to_detector") is not True
    ):
        issues.append("independent_blinded_adjudication_missing")
    ids, pairs = set(), set()
    split_sources = {"development": set(), "held_out": set()}
    split_evidence = {"development": set(), "held_out": set()}
    split_families = {"development": set(), "held_out": set()}
    scored = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("invalid calibration case")
        for field in ("id", "source_id", "family", "template_group", "authored", "candidate"):
            if not isinstance(case.get(field), str) or (not case[field] and field != "candidate"):
                raise ValueError("invalid calibration case field: " + field)
        if len(case["authored"].encode()) > 131072 or len(case["candidate"].encode()) > 65536:
            raise ValueError("calibration text exceeds bounded evidence")
        if (
            case.get("provider") not in ("qwen", "anthropic")
            or case.get("split") not in split_sources
        ):
            raise ValueError("unknown provider or split")
        if case.get("origin") not in ("observed", "reconstructed", "mutated"):
            raise ValueError("unknown case provenance")
        expected = case.get("expected")
        if expected is not None and (
            not isinstance(expected, list)
            or len(set(expected)) != len(expected)
            or set(expected) - set(publication.RULES)
        ):
            raise ValueError("unknown or duplicate expected rule")
        for label in ("faithful", "fallback_safe", "fallback_useful", "served_safe"):
            if case.get(label) is not None and type(case[label]) is not bool:
                raise ValueError("calibration labels must be boolean or unknown")
        if case.get("faithful") is True and expected:
            raise ValueError("faithful control cannot be an expected violation")
        authored_digest = hashlib.sha256(case["authored"].encode()).hexdigest()
        # Repeated invocations, including stochastic variants, do not increase
        # the population. Keep one evidence unit per provider; still enumerate
        # all repeats when checking split leakage.
        pair = (case["provider"], authored_digest)
        if case["id"] in ids:
            raise ValueError("duplicate calibration case id")
        ids.add(case["id"])
        independent = pair not in pairs
        if not independent:
            issues.append("repeated_output_is_not_independent_evidence")
        pairs.add(pair)
        split_evidence[case["split"]].add(authored_digest)
        split_sources[case["split"]].add(case["source_id"])
        split_families[case["split"]].add(case["template_group"])
        scored.append(
            {
                **case,
                "independent": independent,
                "found": list(
                    publication.detect_publication_findings(case["candidate"], case["authored"])
                ),
            }
        )
    if split_sources["development"] & split_sources["held_out"]:
        issues.append("source_leakage_between_splits")
    if split_families["development"] & split_families["held_out"]:
        issues.append("template_leakage_between_splits")
    if split_evidence["development"] & split_evidence["held_out"]:
        issues.append("evidence_leakage_between_splits")
    held = [case for case in scored if case["split"] == "held_out" and case["independent"]]
    clean = [case for case in held if case.get("faithful") is True]
    if len(clean) < 200 or any(
        sum(c["provider"] == p for c in clean) < 100 for p in ("qwen", "anthropic")
    ):
        issues.append("held_out_faithful_population_below_200_or_provider_minimum")
    if (
        len({case["source_id"] for case in clean}) < 50
        or len({case["family"] for case in clean}) < 4
    ):
        issues.append("independent_source_or_content_family_minimum_not_met")
    if any(case.get("expected") is None or case.get("faithful") is None for case in held):
        issues.append("held_out_labels_unknown")
    roster = manifest.get("named_regressions", {})
    if not isinstance(roster, dict) or set(roster) - set(publication.RULES):
        raise ValueError("invalid named regression roster")
    by_id = {case["id"]: case for case in scored}
    for names in roster.values():
        if (
            not isinstance(names, list)
            or not all(isinstance(n, str) for n in names)
            or len(set(names)) != len(names)
            or any(n not in by_id for n in names)
        ):
            raise ValueError("unknown or duplicate named regression")
    if any(case.get("served_safe") is not True for case in clean):
        issues.append("faithful_final_composition_safety_unestablished")
    rules = {}
    for rule in publication.RULES:
        positives = [case for case in held if rule in (case.get("expected") or [])]
        blockers = list(dict.fromkeys(issues))
        named = [by_id[name] for name in roster.get(rule, [])]
        if not named:
            blockers.append("named_regression_roster_missing")
        if any(
            rule not in (c.get("expected") or []) or c.get("faithful") is not False for c in named
        ):
            blockers.append("named_regression_labels_unestablished")
        named_misses = [c["id"] for c in named if rule not in c["found"]]
        if named_misses:
            blockers.append("named_regression_not_detected")
        if any(
            c.get("fallback_safe") is not True
            or c.get("served_safe") is not True
            or c.get("fallback_useful") is None
            for c in named
        ):
            blockers.append("named_regression_final_outcome_unestablished")
        if len(positives) < 40 or any(
            sum(c["provider"] == p for c in positives) < 20 for p in ("qwen", "anthropic")
        ):
            blockers.append("positive_population_or_provider_minimum_not_met")
        if sum(c["origin"] == "observed" for c in positives) < max(20, (len(positives) + 1) // 2):
            blockers.append("observed_unmodified_failure_minimum_not_met")
        if any(
            c.get("fallback_safe") is not True or c.get("served_safe") is not True
            for c in positives
        ):
            blockers.append("fallback_or_final_composition_safety_unestablished")
        if any(c.get("fallback_useful") is None for c in positives):
            blockers.append("fallback_usefulness_unassessed")
        stats = {}
        for provider in ("qwen", "anthropic", "pooled"):
            controls = [c for c in clean if provider == "pooled" or c["provider"] == provider]
            defects = [c for c in positives if provider == "pooled" or c["provider"] == provider]
            fp = sum(rule in c["found"] for c in controls)
            missed = sum(rule not in c["found"] for c in defects)
            stats[provider] = {
                "faithful_controls": len(controls),
                "false_withholdings": fp,
                "false_withholding_95pct": binomial_interval(fp, len(controls)),
                "in_scope_positives": len(defects),
                "missed": missed,
                "miss_95pct": binomial_interval(missed, len(defects)),
                "fallback_useful": sum(c.get("fallback_useful") is True for c in defects),
                "fallback_loss": sum(c.get("fallback_useful") is False for c in defects),
                "fallback_usefulness_unknown": sum(
                    c.get("fallback_useful") is None for c in defects
                ),
            }
            if fp or missed:
                blockers.append("observed_false_withholding_or_in_scope_miss")
        rules[rule] = {
            "ready_for_owner_acceptance": not blockers,
            "blockers": sorted(set(blockers)),
            "statistics": stats,
            "named_regressions": [c["id"] for c in named],
            "named_in_scope_misses": named_misses,
        }
    return {
        "schema_version": 1,
        "policy": publication.POLICY_VERSION,
        "implementation_sha256": implementation,
        "cases": len(cases),
        "independent_pairs": len(pairs),
        "held_out": len(held),
        "rules": rules,
        "runtime_promotions": dict(publication.ACCEPTED_CALIBRATIONS),
        "limitations": [
            "Adjudication/blinding fields are assertions requiring independent evidence.",
            "Finite zero-error controls are not a universal verification guarantee.",
            "This tool never promotes a class or enables a runtime configuration.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("calibration manifest exceeds 32 MiB")
    manifest = json.loads(args.input.read_text())
    report = calibration_report(manifest)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {rule: item["ready_for_owner_acceptance"] for rule, item in report["rules"].items()}
        )
    )


if __name__ == "__main__":
    main()
