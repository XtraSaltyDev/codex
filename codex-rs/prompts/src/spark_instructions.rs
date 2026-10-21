use codex_protocol::models::BaseInstructions;
use codex_protocol::models::BaseInstructionsProvenance;
use sha2::Digest;
use sha2::Sha256;

// Keep released fingerprints so upgrades remain exact and never roll a prompt back.
const SPARK_PROMPT_REVISIONS: &[(u32, &str)] = &[
    (
        1,
        "ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807",
    ),
    (
        2,
        "0b853f0551d12087d05ae6aefed49aa4abadd61d28eaf7a7832514555c9bf63f",
    ),
];

fn revision(text: &str) -> Option<u32> {
    let fingerprint = format!("{:x}", Sha256::digest(text.as_bytes()));
    SPARK_PROMPT_REVISIONS
        .iter()
        .find_map(|(version, expected)| (*expected == fingerprint).then_some(*version))
}

pub fn upgrade_spark_model_instructions(
    provider: &str,
    model: &str,
    mut saved: BaseInstructions,
    current: &str,
) -> BaseInstructions {
    if provider != "spark"
        || !matches!(&saved.provenance, Some(BaseInstructionsProvenance::Model { model: owner }) if owner == model)
    {
        return saved;
    }
    if let (Some(previous), Some(next)) = (revision(&saved.text), revision(current))
        && next > previous
    {
        saved.text = current.to_owned();
    }
    saved
}

#[cfg(test)]
#[path = "spark_instructions_tests.rs"]
mod tests;
