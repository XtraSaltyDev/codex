use super::*;
use pretty_assertions::assert_eq;

const V1: &str = include_str!("../../models-manager/prompt.md");

fn v2() -> String {
    let old = V1
        .lines()
        .find(|line| line.starts_with("- Use the `apply_patch` tool to edit files"))
        .unwrap();
    V1.replace(
        old,
        include_str!("../templates/spark/patch-guidance-v2.txt").trim_end(),
    )
}

fn saved(text: &str) -> BaseInstructions {
    BaseInstructions {
        text: text.to_owned(),
        provenance: Some(BaseInstructionsProvenance::Model {
            model: "spark-model".to_owned(),
        }),
    }
}

#[test]
fn released_fingerprints_are_versioned() {
    assert_eq!(revision(V1), Some(1));
    assert_eq!(revision(&v2()), Some(2));
}

#[test]
fn recognized_model_prompt_upgrades_without_mutating_the_saved_copy() {
    let original = saved(V1);
    let upgraded =
        upgrade_spark_model_instructions("spark", "spark-model", original.clone(), &v2());
    assert_eq!(upgraded.text, v2());
    assert_eq!(upgraded.provenance, original.provenance);
    assert_eq!(original.text, V1);
}

#[test]
fn custom_and_unknown_provenance_are_preserved_even_for_identical_text() {
    for provenance in [None, Some(BaseInstructionsProvenance::Custom)] {
        let original = BaseInstructions {
            text: V1.to_owned(),
            provenance,
        };
        assert_eq!(
            upgrade_spark_model_instructions("spark", "spark-model", original.clone(), &v2()),
            original
        );
    }
}

#[test]
fn modified_stock_prompt_is_preserved() {
    let original = saved(&(V1.to_owned() + "\nMy custom instruction."));
    assert_eq!(
        upgrade_spark_model_instructions("spark", "spark-model", original.clone(), &v2()),
        original
    );
}

#[test]
fn other_provider_and_model_are_preserved() {
    for (provider, model) in [("openai", "spark-model"), ("spark", "different-model")] {
        let original = saved(V1);
        assert_eq!(
            upgrade_spark_model_instructions(provider, model, original.clone(), &v2()),
            original
        );
    }
}

#[test]
fn unknown_target_and_downgrades_are_preserved() {
    for (previous, next) in [
        (v2(), V1.to_owned()),
        (V1.to_owned(), "Custom catalog prompt".to_owned()),
        (V1.to_owned(), String::new()),
    ] {
        let original = saved(&previous);
        assert_eq!(
            upgrade_spark_model_instructions("spark", "spark-model", original.clone(), &next),
            original
        );
    }
}

#[test]
fn current_revision_is_idempotent() {
    let original = saved(&v2());
    assert_eq!(
        upgrade_spark_model_instructions("spark", "spark-model", original.clone(), &v2()),
        original
    );
}
