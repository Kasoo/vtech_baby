//! The keyed whitening applied to every IOTC datagram except the connect record: 16-byte blocks are
//! rotated, XORed with the key and bit-permuted; a 2, 4 or 8-byte tail is XORed and reordered, and any
//! other tail is only XORed. Port of `tutk-host/iotc/wire.c:33–121`; wire authority
//! `tutk-kit/docs/iotc/15-wire-format.md` ("The scramble transform").

/// The fixed key: the first 16 bytes of "Charlie is the designer of P2P!!". Our licence has no
/// `keyMask`, so nothing is mixed in.
pub const KEY: [u8; 16] = *b"Charlie is the d";

const ROT: [u32; 4] = [1, 5, 9, 13];

/// The block bit permutation (`wire.c:33`).
const fn permute(b: [u32; 4]) -> [u32; 4] {
    let [b0, b1, b2, b3] = b;
    let t = (b0 >> 16 & 0xff) | (b0 & 0xff00) | b0 << 24;
    let u = (b1 >> 16 & 0xff) | (b1 >> 8 & 0xff0000) | (b1 << 8 & 0xffff) | (b0 & 0xff000000);
    [
        (b2 >> 24) << 29 | ((b2 & 0xff00 | (b2 & 0xff) << 16 | b2 >> 24 | b3 & 0xff000000) >> 3),
        (b3 >> 8) << 25
            | ((b3 >> 8 & 0xff | b2 >> 8 & 0xff00 | (b3 & 0xff) << 16 | (b3 & 0xff0000) << 8) >> 7),
        t << 21 | ((t | b1 << 8 & 0xff0000) >> 11),
        u.rotate_left(17),
    ]
}

/// `INVERSE[j]` is the input bit that `permute` moves to output bit `j`.
#[allow(clippy::indexing_slicing, clippy::cast_possible_truncation)] // i, j < 128
const INVERSE: [u8; 128] = {
    let mut map = [0u8; 128];
    let mut i = 0;
    while i < 128 {
        let mut b = [0u32; 4];
        b[i / 32] = 1 << (i % 32);
        let y = permute(b);
        let mut j = 0;
        while j < 128 {
            if y[j / 32] >> (j % 32) & 1 == 1 {
                map[j] = i as u8;
            }
            j += 1;
        }
        i += 1;
    }
    map
};

#[allow(clippy::indexing_slicing)] // j, s < 128
fn unpermute(y: [u32; 4]) -> [u32; 4] {
    let mut b = [0u32; 4];
    for j in 0..128 {
        if y[j / 32] >> (j % 32) & 1 == 1 {
            let s = INVERSE[j] as usize;
            b[s / 32] |= 1 << (s % 32);
        }
    }
    b
}

/// The tail byte order for the lengths that are reordered; other lengths keep their order.
fn tail_order(n: usize) -> Option<&'static [usize]> {
    match n {
        2 => Some(&[1, 0]),
        4 => Some(&[2, 3, 0, 1]),
        8 => Some(&[7, 4, 3, 2, 1, 6, 5, 0]),
        _ => None,
    }
}

fn load(block: &[u8]) -> [u32; 4] {
    let mut w = [0u32; 4];
    for (w, c) in w.iter_mut().zip(block.chunks_exact(4)) {
        *w = <[u8; 4]>::try_from(c).map_or(0, u32::from_le_bytes);
    }
    w
}

fn store(block: &mut [u8], w: [u32; 4]) {
    for (c, w) in block.chunks_exact_mut(4).zip(w) {
        c.copy_from_slice(&w.to_le_bytes());
    }
}

#[allow(clippy::indexing_slicing)] // evaluated at compile time; i < 4
const KEY_WORDS: [u32; 4] = {
    let mut w = [0u32; 4];
    let mut i = 0;
    while i < 4 {
        w[i] = u32::from_le_bytes([KEY[4 * i], KEY[4 * i + 1], KEY[4 * i + 2], KEY[4 * i + 3]]);
        i += 1;
    }
    w
};

/// Scramble the first `min(prefix, buf.len())` bytes of `buf` in place.
pub fn scramble(buf: &mut [u8], prefix: usize) {
    let Some(head) = buf.get_mut(..prefix.min(buf.len())) else {
        return;
    };
    let mut blocks = head.chunks_exact_mut(16);
    for block in &mut blocks {
        let mut b = load(block);
        for ((b, rot), k) in b.iter_mut().zip(ROT).zip(KEY_WORDS) {
            *b = b.rotate_right(rot) ^ k;
        }
        store(block, permute(b));
    }
    let tail = blocks.into_remainder();
    let mut tmp = [0u8; 16];
    for ((t, x), k) in tmp.iter_mut().zip(tail.iter()).zip(KEY) {
        *t = x ^ k;
    }
    match tail_order(tail.len()) {
        Some(order) => {
            for (x, &from) in tail.iter_mut().zip(order) {
                *x = tmp.get(from).copied().unwrap_or(0);
            }
        }
        None => {
            for (x, t) in tail.iter_mut().zip(tmp) {
                *x = t;
            }
        }
    }
}

/// Undo [`scramble`] over the same prefix.
pub fn unscramble(buf: &mut [u8], prefix: usize) {
    let Some(head) = buf.get_mut(..prefix.min(buf.len())) else {
        return;
    };
    let mut blocks = head.chunks_exact_mut(16);
    for block in &mut blocks {
        let mut b = unpermute(load(block));
        for ((b, rot), k) in b.iter_mut().zip(ROT).zip(KEY_WORDS) {
            *b = (*b ^ k).rotate_left(rot);
        }
        store(block, b);
    }
    let tail = blocks.into_remainder();
    let mut tmp = [0u8; 16];
    match tail_order(tail.len()) {
        Some(order) => {
            for (&x, &to) in tail.iter().zip(order) {
                if let Some(t) = tmp.get_mut(to) {
                    *t = x;
                }
            }
        }
        None => {
            for (t, &x) in tmp.iter_mut().zip(tail.iter()) {
                *t = x;
            }
        }
    }
    for ((x, t), k) in tail.iter_mut().zip(tmp).zip(KEY) {
        *x = t ^ k;
    }
}

#[cfg(test)]
#[allow(clippy::cast_possible_truncation)]
mod tests {
    use super::*;

    #[test]
    fn kit_worked_example() {
        // T-IOTC-1: tutk-kit/docs/iotc/15-wire-format.md, one block under a test key.
        let key: [u8; 16] = [
            0x44, 0x33, 0x22, 0x11, 0x88, 0x77, 0x66, 0x55, 0xcc, 0xbb, 0xaa, 0x99, 0x00, 0xff, 0xee, 0xdd,
        ];
        let plain: [u8; 16] = [4, 3, 2, 1, 8, 7, 6, 5, 0xc, 0xb, 0xa, 9, 0x10, 0xf, 0xe, 0xd];
        let mut b = load(&plain);
        for ((b, rot), k) in b.iter_mut().zip(ROT).zip(load(&key)) {
            *b = b.rotate_right(rot) ^ k;
        }
        let mut out = [0u8; 16];
        store(&mut out, permute(b));
        assert_eq!(
            out,
            [
                0xc3, 0x27, 0xb9, 0xf4, 0x5d, 0xe1, 0xdc, 0x2e, 0xe6, 0xc8, 0x78, 0x54, 0x2b, 0x22, 0x9c,
                0x60
            ]
        );
    }

    #[test]
    fn round_trip_every_length_and_prefix() {
        // T-IOTC-2.
        let orig: Vec<u8> = (0..=2048u32).map(|i| (i * 37 + 11) as u8).collect();
        for len in 0..=2048 {
            for prefix in [len, 0x40, 16, 0] {
                let mut b = orig[..len].to_vec();
                scramble(&mut b, prefix);
                if prefix.min(len) > 0 {
                    assert_ne!(b, orig[..len], "len {len} prefix {prefix} unchanged");
                }
                assert_eq!(
                    b[prefix.min(len)..],
                    orig[prefix.min(len)..len],
                    "past the prefix"
                );
                unscramble(&mut b, prefix);
                assert_eq!(b, orig[..len], "len {len} prefix {prefix}");
            }
        }
    }

    #[test]
    fn inverse_is_a_permutation() {
        let mut seen = [false; 128];
        for &i in &INVERSE {
            seen[i as usize] = true;
        }
        assert!(seen.iter().all(|&s| s));
    }
}
