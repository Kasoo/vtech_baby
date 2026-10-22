//! Randomness behind a trait, so a whole link can run deterministically under test.

pub trait Rng: Send {
    fn fill(&mut self, buf: &mut [u8]);

    fn u32(&mut self) -> u32 {
        let mut b = [0u8; 4];
        self.fill(&mut b);
        u32::from_le_bytes(b)
    }

    fn array<const N: usize>(&mut self) -> [u8; N]
    where
        Self: Sized,
    {
        let mut b = [0u8; N];
        self.fill(&mut b);
        b
    }
}

/// The operating system's generator.
pub struct OsRng;

impl Rng for OsRng {
    fn fill(&mut self, buf: &mut [u8]) {
        // getrandom fails only if the OS has no entropy source at all, which a running Linux host
        // always has; there is nothing better to fall back to.
        #[allow(clippy::panic)]
        if let Err(e) = getrandom::fill(buf) {
            panic!("no OS randomness: {e}");
        }
    }
}
