//! Parity recovery for class-0x0c media packets: the camera follows each group of `k` data packets
//! with one Reed-Solomon parity packet over GF(256), and with the parity and `k - 1` data packets the
//! missing one is rebuilt. Port of `tutk-host/av/fec.c`; wire authority `tutk-kit/docs/av/80-fec.md`
//! and `av/90-fifos.md` ("FEC envelope").

/// A shard: the media sub-header and payload, zero-padded to this length.
pub const BLOCK: usize = 1044;
/// The media (and parity) sub-header length.
pub const MEDIA_HEADER: usize = 0x14;

const MAX_K: usize = 25;
const SLOTS: usize = 4;
/// The first byte of a parity packet's sub-header (the data class).
const CLASS_DATA: u8 = 0x0c;

/// Generator row for `k = 20` (`fec.c:18`).
const ROW_20: [u8; 20] = [
    0x8f, 0xae, 0x5b, 0x70, 0xd0, 0xcd, 0x54, 0x43, 0x39, 0xa3, 0xc9, 0x58, 0x1b, 0xbb, 0xb3, 0x18, 0x1b,
    0x1c, 0x12, 0x14,
];
/// Generator row for `k = 25` (`fec.c:21`).
const ROW_25: [u8; 25] = [
    0x19, 0x19, 0x6f, 0x6f, 0x53, 0x53, 0x24, 0x24, 0xb0, 0xb0, 0xd0, 0xd0, 0x92, 0x92, 0xf3, 0xf3, 0x9e,
    0x9e, 0x89, 0x89, 0xf7, 0xf7, 0xe1, 0xe1, 0x01,
];

/// `exp` (doubled, so a sum of two logs needs no reduction) and `log` for GF(2^8), polynomial 0x11d,
/// generator 2.
#[allow(clippy::indexing_slicing, clippy::cast_possible_truncation)] // i < 255, x < 256
const TABLES: ([u8; 510], [u8; 256]) = {
    let mut exp = [0u8; 510];
    let mut log = [0u8; 256];
    let mut x: u32 = 1;
    let mut i = 0;
    while i < 255 {
        exp[i] = x as u8;
        exp[i + 255] = x as u8;
        log[x as usize] = i as u8;
        x <<= 1;
        if x & 0x100 != 0 {
            x ^= 0x11d;
        }
        i += 1;
    }
    (exp, log)
};

fn exp(i: usize) -> u8 {
    TABLES.0.get(i).copied().unwrap_or(0)
}

fn log(a: u8) -> usize {
    TABLES.1.get(usize::from(a)).copied().map_or(0, usize::from)
}

fn gf_mul(a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 { 0 } else { exp(log(a) + log(b)) }
}

fn row_for(k: u8) -> Option<&'static [u8]> {
    match k {
        20 => Some(&ROW_20),
        25 => Some(&ROW_25),
        _ => None,
    }
}

/// The FEC group header at B in a class-0x0c packet: u16 group, u8 index, u8 k (then u32 1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GroupHeader {
    pub group: u16,
    pub index: u8,
    pub k: u8,
}

impl GroupHeader {
    /// Parse the 8-byte group header; `None` if shorter than 8 bytes.
    pub fn parse(b: &[u8]) -> Option<Self> {
        if b.len() < 8 {
            return None;
        }
        Some(Self {
            group: u16::from_le_bytes([*b.first()?, *b.get(1)?]),
            index: *b.get(2)?,
            k: *b.get(3)?,
        })
    }
}

struct Group {
    used: bool,
    /// Complete, or its missing packet already rebuilt.
    done: bool,
    group: u16,
    k: u8,
    /// When the slot was taken, to drop the oldest.
    seq: u32,
    /// Bit i: data shard i; bit k: the parity.
    have: u32,
    /// `MAX_K + 1` shards of `BLOCK` bytes.
    block: Vec<u8>,
}

impl Group {
    fn new() -> Self {
        Self {
            used: false,
            done: false,
            group: 0,
            k: 0,
            seq: 0,
            have: 0,
            block: vec![0; (MAX_K + 1) * BLOCK],
        }
    }

    fn shard(&self, i: usize) -> &[u8] {
        self.block.chunks_exact(BLOCK).nth(i).unwrap_or_default()
    }

    /// Rebuild the one missing data shard from the parity and the other data shards.
    fn rebuild(&self, row: &[u8]) -> [u8; BLOCK] {
        let miss = self.have.trailing_ones() as usize;
        let mut out = [0u8; BLOCK];
        out.copy_from_slice(self.shard(usize::from(self.k)));
        for (i, &g) in row.iter().enumerate().take(usize::from(self.k)) {
            if i == miss {
                continue;
            }
            for (o, &d) in out.iter_mut().zip(self.shard(i)) {
                *o ^= gf_mul(g, d);
            }
        }
        let inv = exp(255 - log(row.get(miss).copied().unwrap_or(0)));
        for o in &mut out {
            *o = gf_mul(*o, inv);
        }
        out
    }
}

/// Four FEC groups in progress.
pub struct Fec {
    slots: Vec<Group>,
    seq: u32,
}

impl Default for Fec {
    fn default() -> Self {
        Self::new()
    }
}

impl Fec {
    pub fn new() -> Self {
        Self {
            slots: (0..SLOTS).map(|_| Group::new()).collect(),
            seq: 0,
        }
    }

    /// The slot for `group`; a new group takes an unused slot, else the oldest.
    fn slot_for(&mut self, group: u16, k: u8) -> usize {
        let mut oldest = 0;
        for (i, g) in self.slots.iter().enumerate() {
            if g.used && g.group == group && g.k == k {
                return i;
            }
            let o_seq = self.slots.get(oldest).map(|o| (o.used, o.seq));
            let older = o_seq.is_some_and(|(used, seq)| used && g.seq.wrapping_sub(seq) >= 1 << 31);
            if !g.used || older {
                oldest = i;
            }
        }
        let seq = self.seq;
        self.seq = seq.wrapping_add(1);
        if let Some(g) = self.slots.get_mut(oldest) {
            g.used = true;
            g.done = false;
            g.group = group;
            g.k = k;
            g.seq = seq;
            g.have = 0;
        }
        oldest
    }

    /// Feed one packet of an FEC group: `frame` is the bytes at f (the 0x14 media sub-header and
    /// payload for data; the 0x14 parity sub-header and 1044-byte parity block for parity, whose
    /// first byte is 0x0c). Returns the rebuilt data packet (its sub-header and `plen` bytes, as it
    /// would have arrived at f) when this packet completes a recovery.
    pub fn on_packet(&mut self, header: GroupHeader, frame: &[u8]) -> Option<Vec<u8>> {
        let GroupHeader { group, index, k } = header;
        let row = row_for(k)?;
        let parity = *frame.first()? == CLASS_DATA;
        if index > k || parity != (index == k) {
            return None;
        }
        let src = if parity {
            // The RS block follows the parity packet's own sub-header.
            let block = frame.get(MEDIA_HEADER..)?;
            block.get(..block.len().min(BLOCK))?
        } else {
            let plen = usize::from(u16::from_le_bytes([*frame.get(8)?, *frame.get(9)?]));
            let len = MEDIA_HEADER + plen;
            if len > BLOCK {
                return None;
            }
            frame.get(..len)?
        };
        if src.is_empty() {
            return None;
        }

        let slot = self.slot_for(group, k);
        let g = self.slots.get_mut(slot)?;
        if g.done || g.have & (1 << index) != 0 {
            return None;
        }
        let dst = g.block.chunks_exact_mut(BLOCK).nth(usize::from(index))?;
        let (head, tail) = dst.split_at_mut(src.len());
        head.copy_from_slice(src);
        tail.fill(0);
        g.have |= 1 << index;

        let all_data = (1u32 << k) - 1;
        if g.have & all_data == all_data {
            g.done = true;
            return None;
        }
        if g.have & (1 << k) == 0 || g.have.count_ones() != u32::from(k) {
            return None;
        }
        let out = g.rebuild(row);
        g.done = true;
        let plen = usize::from(u16::from_le_bytes([*out.get(8)?, *out.get(9)?]));
        out.get(..MEDIA_HEADER + plen).map(<[u8]>::to_vec)
    }

    /// Forget every group (the media reset).
    pub fn reset(&mut self) {
        for g in &mut self.slots {
            g.used = false;
            g.done = false;
            g.group = 0;
            g.k = 0;
            g.seq = 0;
            g.have = 0;
        }
        self.seq = 0;
    }
}

#[cfg(test)]
#[allow(clippy::cast_possible_truncation, clippy::needless_range_loop)] // synthetic small test values
mod tests {
    use super::*;

    /// A synthetic data shard: kind 5 or 7, packet number, count, index, `plen` payload bytes.
    fn shard(i: usize, k: usize, plen: usize) -> Vec<u8> {
        let mut f = vec![0u8; MEDIA_HEADER + plen];
        f[0] = if i % 3 == 0 { 7 } else { 5 };
        f[2..4].copy_from_slice(&(0x100 + i as u16).to_le_bytes());
        f[4..6].copy_from_slice(&(k as u16).to_le_bytes());
        f[6..8].copy_from_slice(&(i as u16).to_le_bytes());
        f[8..10].copy_from_slice(&(plen as u16).to_le_bytes());
        for (j, b) in f[MEDIA_HEADER..].iter_mut().enumerate() {
            *b = (i * 31 + j * 7 + 1) as u8;
        }
        f
    }

    fn group(k: usize) -> Vec<Vec<u8>> {
        (0..k)
            .map(|i| {
                let plen = if i == k - 1 {
                    357
                } else if i == 4 {
                    0
                } else {
                    1024 - (i % 3) * 100
                };
                shard(i, k, plen)
            })
            .collect()
    }

    /// The parity packet's frame: a class-0x0c sub-header and the parity block.
    fn parity(shards: &[Vec<u8>], row: &[u8]) -> Vec<u8> {
        let mut p = vec![0u8; BLOCK];
        for (s, &g) in shards.iter().zip(row) {
            for (o, &d) in p.iter_mut().zip(s) {
                *o ^= gf_mul(g, d);
            }
        }
        let mut f = vec![0u8; MEDIA_HEADER];
        f[0] = CLASS_DATA;
        f.extend(p);
        f
    }

    fn hdr(group: u16, index: usize, k: usize) -> GroupHeader {
        GroupHeader {
            group,
            index: index as u8,
            k: k as u8,
        }
    }

    fn shuffle<T>(v: &mut [T], mut seed: u32) {
        for i in (1..v.len()).rev() {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            v.swap(i, (seed >> 8) as usize % (i + 1));
        }
    }

    /// Feed every packet of a group but `drop`, in `order`; returns every rebuild.
    fn feed(
        fec: &mut Fec,
        gid: u16,
        k: usize,
        packets: &[Vec<u8>],
        order: &[usize],
        drop: usize,
    ) -> Vec<Vec<u8>> {
        order
            .iter()
            .filter(|&&i| i != drop)
            .filter_map(|&i| fec.on_packet(hdr(gid, i, k), &packets[i]))
            .collect()
    }

    fn rows() -> [(usize, &'static [u8]); 2] {
        [(20, &ROW_20), (25, &ROW_25)]
    }

    #[test]
    fn gf_tables() {
        for a in 1..=255u8 {
            assert_eq!(exp(log(a)), a);
            let inv = exp(255 - log(a));
            assert_eq!(gf_mul(a, inv), 1);
            assert_eq!(gf_mul(a, 0), 0);
        }
        assert_eq!(gf_mul(2, 0x80), 0x1d);
        assert_eq!(exp(255), 1);
    }

    #[test]
    fn rebuilds_every_missing_shard() {
        for (k, row) in rows() {
            let data = group(k);
            let mut packets = data.clone();
            packets.push(parity(&data, row));
            for m in 0..k {
                for (n, parity_first) in [(1, true), (2, false), (3, false)] {
                    let mut order: Vec<usize> = (0..k).collect();
                    shuffle(&mut order, (m * 7 + n) as u32);
                    if parity_first {
                        order.insert(0, k);
                    } else if n == 2 {
                        order.push(k);
                    } else {
                        order.insert(order.len() / 2, k);
                    }
                    let mut fec = Fec::new();
                    let out = feed(&mut fec, 9, k, &packets, &order, m);
                    assert_eq!(out, vec![data[m].clone()], "k {k} missing {m}");
                }
            }
        }
    }

    #[test]
    fn complete_group_does_not_rebuild() {
        for (k, row) in rows() {
            let data = group(k);
            let mut packets = data.clone();
            packets.push(parity(&data, row));
            let mut fec = Fec::new();
            let order: Vec<usize> = (0..=k).collect();
            assert!(feed(&mut fec, 1, k, &packets, &order, usize::MAX).is_empty());
            let mut order: Vec<usize> = (0..k).rev().collect();
            order.push(k);
            assert!(feed(&mut fec, 2, k, &packets, &order, usize::MAX).is_empty());
        }
    }

    #[test]
    fn duplicates_and_late_packets_do_nothing() {
        let k = 20;
        let data = group(k);
        let mut packets = data.clone();
        packets.push(parity(&data, &ROW_20));
        let mut fec = Fec::new();
        let order: Vec<usize> = (0..=k).collect();
        assert_eq!(feed(&mut fec, 5, k, &packets, &order, 3).len(), 1);
        assert!(feed(&mut fec, 5, k, &packets, &order, 3).is_empty());
        assert!(fec.on_packet(hdr(5, 3, k), &packets[3]).is_none());

        let mut fec = Fec::new();
        assert!(fec.on_packet(hdr(5, 0, k), &packets[0]).is_none());
        assert!(fec.on_packet(hdr(5, 0, k), &packets[0]).is_none());
        assert!(fec.on_packet(hdr(5, k, k), &packets[k]).is_none());
        assert!(fec.on_packet(hdr(5, k, k), &packets[k]).is_none());
    }

    #[test]
    fn rejects_bad_packets() {
        let k = 20;
        let data = group(k);
        let par = parity(&data, &ROW_20);
        let mut fec = Fec::new();
        // unknown k
        assert!(fec.on_packet(hdr(1, 0, 10), &data[0]).is_none());
        // index past k, parity at a data index, data at the parity index
        assert!(fec.on_packet(hdr(1, 21, k), &data[0]).is_none());
        assert!(fec.on_packet(hdr(1, 3, k), &par).is_none());
        assert!(fec.on_packet(hdr(1, k, k), &data[0]).is_none());
        // data longer than a block
        let mut big = shard(0, k, 1024);
        big[8..10].copy_from_slice(&1025u16.to_le_bytes());
        big.resize(MEDIA_HEADER + 1025, 0);
        assert!(fec.on_packet(hdr(1, 0, k), &big).is_none());
        // data shorter than its plen
        assert!(fec.on_packet(hdr(1, 0, k), &data[0][..100]).is_none());
        // no slot was taken by any of those: a full group still rebuilds
        let mut packets = data.clone();
        packets.push(par);
        let order: Vec<usize> = (0..=k).collect();
        assert_eq!(feed(&mut fec, 1, k, &packets, &order, 7), vec![data[7].clone()]);
        assert!(GroupHeader::parse(&[0; 7]).is_none());
        assert_eq!(
            GroupHeader::parse(&[0x34, 0x12, 5, 20, 1, 0, 0, 0]),
            Some(GroupHeader {
                group: 0x1234,
                index: 5,
                k: 20
            })
        );
    }

    #[test]
    fn fifth_group_evicts_the_oldest() {
        let k = 20;
        let data = group(k);
        let mut packets = data.clone();
        packets.push(parity(&data, &ROW_20));
        let mut fec = Fec::new();
        // Groups 1..=4 each get one shard; group 1 also gets all but one more.
        for gid in 1..=4u16 {
            fec.on_packet(hdr(gid, 0, k), &packets[0]);
        }
        fec.on_packet(hdr(5, 0, k), &packets[0]); // evicts group 1
        let order: Vec<usize> = (1..=k).collect();
        // Group 1 restarts empty: shard 0 is missing again, so with 1..=k minus 7 it cannot rebuild.
        assert!(feed(&mut fec, 1, k, &packets, &order, 7).is_empty());
        // Group 3 survived and rebuilds.
        assert_eq!(feed(&mut fec, 3, k, &packets, &order, 7).len(), 1);
    }

    #[test]
    fn reset_forgets_partial_groups() {
        let k = 25;
        let data = group(k);
        let mut packets = data.clone();
        packets.push(parity(&data, &ROW_25));
        let mut fec = Fec::new();
        let first: Vec<usize> = (0..10).collect();
        assert!(feed(&mut fec, 4, k, &packets, &first, usize::MAX).is_empty());
        fec.reset();
        let rest: Vec<usize> = (10..=k).collect();
        assert!(feed(&mut fec, 4, k, &packets, &rest, 12).is_empty());
        // Without a reset the same split rebuilds.
        let mut fec = Fec::new();
        feed(&mut fec, 4, k, &packets, &first, usize::MAX);
        assert_eq!(feed(&mut fec, 4, k, &packets, &rest, 12).len(), 1);
    }
}
