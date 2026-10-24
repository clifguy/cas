"""Insufficient, leaking or unadjudicated evidence cannot license withholding."""

import pytest

from sage.abstraction_policy import implementation_digest
from sage.services import abstraction_publication
from scripts.evaluate_abstraction_publication import binomial_interval, calibration_report


def manifest():
    return {
        "schema_version": 1,
        "implementation_sha256": implementation_digest(),
        "candidate_stage": "post_licensed_repairs",
        "labels_status": "unadjudicated",
        "cases": [
            {
                "id": "one",
                "source_id": "source-one",
                "family": "narrative",
                "template_group": "template-one",
                "provider": "qwen",
                "split": "development",
                "origin": "observed",
                "authored": "Pruning documentation.",
                "candidate": "The text describes item06886.",
                "expected": None,
                "faithful": None,
            }
        ],
    }


def test_small_corpus_has_explicit_shortfalls_and_does_not_promote():
    report = calibration_report(manifest())
    assert report["cases"] == 1 and report["independent_pairs"] == 1
    for rule in report["rules"].values():
        assert not rule["ready_for_owner_acceptance"]
        assert "independent_labels_missing" in rule["blockers"]
        assert "positive_population_or_provider_minimum_not_met" in rule["blockers"]
    assert not abstraction_publication.ACCEPTED_CALIBRATIONS


def test_repeated_calls_and_split_leakage_are_not_independent_evidence():
    data = manifest()
    second = {**data["cases"][0], "id": "two", "split": "held_out"}
    data["cases"].append(second)
    report = calibration_report(data)
    assert report["independent_pairs"] == 1
    for rule in report["rules"].values():
        assert "repeated_output_is_not_independent_evidence" in rule["blockers"]
        assert "source_leakage_between_splits" in rule["blockers"]
        assert "template_leakage_between_splits" in rule["blockers"]


def test_stale_implementation_cannot_reuse_calibration():
    data = manifest()
    data["implementation_sha256"] = "0" * 64
    assert all(
        "implementation_not_frozen_at_current_bytes" in x["blockers"]
        for x in calibration_report(data)["rules"].values()
    )


@pytest.mark.parametrize(
    "errors,total,low,high", [(0, 0, 0, 1), (0, 100, 0, 0.0362167), (100, 100, 0.9637833, 1)]
)
def test_exact_intervals_preserve_uncertainty(errors, total, low, high):
    actual = binomial_interval(errors, total)
    assert actual[0] == pytest.approx(low, abs=0.000001)
    assert actual[1] == pytest.approx(high, abs=0.000001)


def test_malformed_or_contradictory_labels_are_refused():
    data = manifest()
    data["cases"][0].update(expected=["unsupported_identifier"], faithful=True)
    with pytest.raises(ValueError, match="faithful control"):
        calibration_report(data)


def complete_population():
    data = manifest()
    data.update(
        labels_status="independently_adjudicated",
        adjudicators=["first-reader", "second-reader"],
        blinded_to_detector=True,
    )
    for i in range(200):
        data["cases"].append(
            {
                "id": f"clean-{i}",
                "source_id": f"clean-source-{i}",
                "family": f"genre-{i % 4}",
                "template_group": f"clean-template-{i}",
                "provider": "qwen" if i % 2 else "anthropic",
                "split": "held_out",
                "origin": "observed",
                "authored": f"Pruning work case {i}.",
                "candidate": f"The text describes pruning work case {i}.",
                "expected": [],
                "faithful": True,
                "served_safe": True,
            }
        )
    for rule in abstraction_publication.RULES:
        for i in range(40):
            authored = (
                f"Orchard evidence variant {rule} {i}; item00000 is listed, Lateorchid is a name."
            )
            candidate = {
                "unsupported_identifier": "The source describes item06886.",
                "bounded_structure": "The excerpts list four departments.",
                "range_role": "The excerpts describe item00000 through Lateorchid.",
            }[rule]
            data["cases"].append(
                {
                    "id": f"{rule}-{i}",
                    "source_id": f"positive-{rule}-{i}",
                    "family": "structured",
                    "template_group": f"positive-template-{rule}-{i}",
                    "provider": "qwen" if i % 2 else "anthropic",
                    "split": "held_out",
                    "origin": "observed",
                    "authored": authored,
                    "candidate": candidate,
                    "expected": [rule],
                    "faithful": False,
                    "fallback_safe": True,
                    "fallback_useful": True,
                    "served_safe": True,
                }
            )
    data["named_regressions"] = {rule: [f"{rule}-0"] for rule in abstraction_publication.RULES}
    return data


def test_complete_population_is_eligible_only_for_owner_acceptance_not_runtime_promotion():
    report = calibration_report(complete_population())
    assert all(item["ready_for_owner_acceptance"] for item in report["rules"].values())
    assert not report["runtime_promotions"]
    for item in report["rules"].values():
        assert item["statistics"]["pooled"]["faithful_controls"] == 200
        assert item["statistics"]["qwen"]["in_scope_positives"] == 20


def test_false_withholding_and_unsafe_fallback_block_the_affected_classes():
    data = complete_population()
    data["cases"][1]["candidate"] = "The excerpts list four departments."
    data["cases"][-1]["fallback_safe"] = False
    report = calibration_report(data)
    assert not report["rules"]["bounded_structure"]["ready_for_owner_acceptance"]
    assert report["rules"]["bounded_structure"]["statistics"]["pooled"]["false_withholdings"] == 1
    assert not report["rules"]["range_role"]["ready_for_owner_acceptance"]
    assert (
        "fallback_or_final_composition_safety_unestablished"
        in report["rules"]["range_role"]["blockers"]
    )


def test_stochastic_repeats_do_not_inflate_the_independent_population():
    data = manifest()
    data["cases"].append(
        {**data["cases"][0], "id": "two", "candidate": "A different generated paraphrase."}
    )
    assert calibration_report(data)["independent_pairs"] == 1


def test_named_development_regression_cannot_be_hidden_by_held_out_success():
    data = complete_population()
    data["cases"].append(
        {
            **data["cases"][-1],
            "id": "named-development-failure",
            "source_id": "named-development-source",
            "template_group": "named-development-template",
            "split": "development",
            "authored": "Unique development exhibit about pruning.",
            "candidate": "The excerpts describe pruning.",
            "expected": ["unsupported_identifier"],
        }
    )
    data["named_regressions"]["unsupported_identifier"] = ["named-development-failure"]
    rule = calibration_report(data)["rules"]["unsupported_identifier"]
    assert not rule["ready_for_owner_acceptance"]
    assert rule["named_in_scope_misses"] == ["named-development-failure"]


def test_faithful_control_final_composition_requires_its_own_safety_label():
    data = complete_population()
    data["cases"][1]["served_safe"] = None
    assert all(
        "faithful_final_composition_safety_unestablished" in rule["blockers"]
        for rule in calibration_report(data)["rules"].values()
    )
