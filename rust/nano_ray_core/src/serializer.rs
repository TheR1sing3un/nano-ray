//! Serialization utilities.
//!
//! Phase 2: Pass-through for Python pickle/cloudpickle bytes.
//! The actual serialization is done in Python. This module exists
//! as a placeholder for future optimizations:
//!
//! - numpy array fast path (Phase 3+): detect numpy arrays and
//!   serialize them as raw dtype + shape + data bytes, bypassing pickle
//! - Arrow-compatible format for tabular data
//!
//! In Ray, serialization uses a custom format based on Apache Arrow
//! for zero-copy deserialization of numpy arrays and other data types.
//! nano-ray will add the numpy fast path in Phase 3 as an optimization.

use bytes::Bytes;

/// Pass-through serialization: wrap raw bytes.
#[allow(dead_code)]
pub fn wrap_bytes(data: &[u8]) -> Bytes {
    Bytes::copy_from_slice(data)
}

/// Pass-through deserialization: extract raw bytes.
#[allow(dead_code)]
pub fn unwrap_bytes(data: &Bytes) -> &[u8] {
    data.as_ref()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_roundtrip() {
        let original = b"hello, pickle bytes!";
        let wrapped = wrap_bytes(original);
        let unwrapped = unwrap_bytes(&wrapped);
        assert_eq!(unwrapped, original);
    }
}
