//! Tracking-number validation and normalization.
//!
//! The same shipment must map to the same row whether the user typed `ab-123` or ` AB-123 `,
//! so numbers are trimmed and upper-cased before they are stored or compared.

pub const MAX_LEN: usize = 100;

/// Returns the canonical form, or a human-readable reason the input is not acceptable.
/// The accepted alphabet matches `trackingNumber` in the message contract.
pub fn normalize(input: &str) -> Result<String, &'static str> {
    let value = input.trim().to_ascii_uppercase();
    if value.is_empty() {
        return Err("must not be empty");
    }
    if value.len() > MAX_LEN {
        return Err("must be at most 100 characters");
    }
    let mut chars = value.chars();
    let first = chars.next().expect("checked non-empty");
    if !first.is_ascii_alphanumeric() {
        return Err("must start with a letter or digit");
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '/' | '-')) {
        return Err("may contain only letters, digits and . _ / -");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trims_and_uppercases() {
        assert_eq!(normalize("  dummy-ok-1 ").unwrap(), "DUMMY-OK-1");
        assert_eq!(normalize("1Z999/aa.10_x").unwrap(), "1Z999/AA.10_X");
    }

    #[test]
    fn rejects_empty_and_blank() {
        assert!(normalize("").is_err());
        assert!(normalize("   ").is_err());
    }

    #[test]
    fn enforces_the_length_limit() {
        assert!(normalize(&"A".repeat(100)).is_ok());
        assert!(normalize(&"A".repeat(101)).is_err());
    }

    #[test]
    fn rejects_characters_outside_the_alphabet() {
        for bad in ["-ABC", "AB C", "AB;DROP", "AB\n1", "ÄB1", "AB'1", "AB%1"] {
            assert!(normalize(bad).is_err(), "{bad:?} should be rejected");
        }
    }
}
