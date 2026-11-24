//! Envelope encryption for stored secrets (AES-256-GCM).
//!
//! Each secret is encrypted with its own random 256-bit *data key*. The data key is then
//! encrypted ("wrapped") with the *master key*, which exists only in the Engine's
//! environment and never in the database. Someone who obtains the database alone cannot
//! read any secret, and changing the master key later only requires re-wrapping the small
//! data keys, not re-encrypting every secret.
//!
//! Both encryptions are authenticated and bound to the record they belong to (the
//! "associated data"), so a ciphertext copied onto another credential row fails to decrypt.

use aes_gcm::{
    Aes256Gcm, Key, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use rand::{TryRng, rngs::SysRng};

pub const KEY_LEN: usize = 32;
pub const NONCE_LEN: usize = 12;

#[derive(Debug, thiserror::Error)]
pub enum CryptoError {
    #[error("the master key must be 64 hexadecimal characters (32 bytes)")]
    BadMasterKey,
    #[error("the stored secret cannot be decrypted with this master key")]
    Decrypt,
    #[error(
        "the secret was stored with master key version {stored}, but version {current} is configured"
    )]
    KeyVersion { stored: i32, current: i32 },
}

/// The master key. Its `Debug` output never shows the key.
#[derive(Clone)]
pub struct MasterKey {
    key: [u8; KEY_LEN],
    pub version: i32,
}

impl std::fmt::Debug for MasterKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "MasterKey(version {}, [redacted])", self.version)
    }
}

/// A secret as stored: nothing here is readable without the master key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sealed {
    pub key_version: i32,
    pub wrapped_key: Vec<u8>,
    pub key_nonce: [u8; NONCE_LEN],
    pub ciphertext: Vec<u8>,
    pub nonce: [u8; NONCE_LEN],
}

fn os_random<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    SysRng
        .try_fill_bytes(&mut bytes)
        .expect("the operating system random generator is unavailable");
    bytes
}

impl MasterKey {
    pub fn from_hex(hex_key: &str, version: i32) -> Result<Self, CryptoError> {
        let bytes = hex::decode(hex_key.trim()).map_err(|_| CryptoError::BadMasterKey)?;
        let key: [u8; KEY_LEN] = bytes.try_into().map_err(|_| CryptoError::BadMasterKey)?;
        Ok(MasterKey { key, version })
    }

    /// A new random master key, as the text to put into `CREDENTIAL_MASTER_KEY`.
    pub fn generate_hex() -> String {
        hex::encode(os_random::<KEY_LEN>())
    }

    /// Encrypts `plaintext`. `context` identifies the record (it is not secret and not
    /// stored here); the same value must be supplied to [`MasterKey::open`].
    pub fn seal(&self, plaintext: &[u8], context: &[u8]) -> Sealed {
        let data_key = os_random::<KEY_LEN>();
        let nonce = os_random::<NONCE_LEN>();
        let key_nonce = os_random::<NONCE_LEN>();
        let ciphertext = encrypt(&data_key, &nonce, plaintext, context);
        let wrapped_key = encrypt(&self.key, &key_nonce, &data_key, context);
        Sealed {
            key_version: self.version,
            wrapped_key,
            key_nonce,
            ciphertext,
            nonce,
        }
    }

    pub fn open(&self, sealed: &Sealed, context: &[u8]) -> Result<Vec<u8>, CryptoError> {
        if sealed.key_version != self.version {
            return Err(CryptoError::KeyVersion {
                stored: sealed.key_version,
                current: self.version,
            });
        }
        let data_key = decrypt(&self.key, &sealed.key_nonce, &sealed.wrapped_key, context)?;
        let data_key: [u8; KEY_LEN] = data_key.try_into().map_err(|_| CryptoError::Decrypt)?;
        decrypt(&data_key, &sealed.nonce, &sealed.ciphertext, context)
    }
}

fn encrypt(key: &[u8; KEY_LEN], nonce: &[u8; NONCE_LEN], msg: &[u8], aad: &[u8]) -> Vec<u8> {
    Aes256Gcm::new(&Key::<Aes256Gcm>::from(*key))
        .encrypt(&Nonce::from(*nonce), Payload { msg, aad })
        .expect("AES-GCM encryption of an in-memory buffer cannot fail")
}

fn decrypt(
    key: &[u8; KEY_LEN],
    nonce: &[u8; NONCE_LEN],
    msg: &[u8],
    aad: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    Aes256Gcm::new(&Key::<Aes256Gcm>::from(*key))
        .decrypt(&Nonce::from(*nonce), Payload { msg, aad })
        .map_err(|_| CryptoError::Decrypt)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> MasterKey {
        MasterKey::from_hex(&MasterKey::generate_hex(), 1).unwrap()
    }

    #[test]
    fn a_secret_survives_a_round_trip() {
        let master = key();
        let sealed = master.seal(b"api-key-123", b"record-1");
        assert_eq!(master.open(&sealed, b"record-1").unwrap(), b"api-key-123");
    }

    #[test]
    fn the_stored_form_does_not_contain_the_secret_and_differs_every_time() {
        let master = key();
        let secret = b"a-very-recognizable-secret-value";
        let (a, b) = (master.seal(secret, b"r"), master.seal(secret, b"r"));
        assert_ne!(a, b, "fresh data key and nonces for every record");
        assert!(!a.ciphertext.windows(secret.len()).any(|w| w == secret));
    }

    #[test]
    fn another_master_key_cannot_open_it() {
        let sealed = key().seal(b"secret", b"r");
        assert!(matches!(
            key().open(&sealed, b"r"),
            Err(CryptoError::Decrypt)
        ));
    }

    #[test]
    fn a_ciphertext_moved_to_another_record_is_rejected() {
        let master = key();
        let sealed = master.seal(b"secret", b"record-1");
        assert!(matches!(
            master.open(&sealed, b"record-2"),
            Err(CryptoError::Decrypt)
        ));
    }

    #[test]
    fn any_modified_byte_is_detected() {
        let master = key();
        let sealed = master.seal(b"secret", b"r");
        let mut ciphertext = sealed.clone();
        ciphertext.ciphertext[0] ^= 1;
        let mut wrapped = sealed.clone();
        wrapped.wrapped_key[0] ^= 1;
        let mut nonce = sealed.clone();
        nonce.nonce[0] ^= 1;
        for tampered in [ciphertext, wrapped, nonce] {
            assert!(matches!(
                master.open(&tampered, b"r"),
                Err(CryptoError::Decrypt)
            ));
        }
    }

    #[test]
    fn a_different_key_version_is_reported_clearly() {
        let hex_key = MasterKey::generate_hex();
        let sealed = MasterKey::from_hex(&hex_key, 1).unwrap().seal(b"s", b"r");
        let newer = MasterKey::from_hex(&hex_key, 2).unwrap();
        assert!(matches!(
            newer.open(&sealed, b"r"),
            Err(CryptoError::KeyVersion {
                stored: 1,
                current: 2
            })
        ));
    }

    #[test]
    fn malformed_master_keys_are_refused_and_the_key_is_never_printed() {
        assert!(MasterKey::from_hex("abc", 1).is_err());
        assert!(MasterKey::from_hex(&"zz".repeat(32), 1).is_err());
        assert!(MasterKey::from_hex(&"ab".repeat(31), 1).is_err());
        let hex_key = MasterKey::generate_hex();
        let printed = format!("{:?}", MasterKey::from_hex(&hex_key, 1).unwrap());
        assert!(!printed.contains(&hex_key));
    }
}
