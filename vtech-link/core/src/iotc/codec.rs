//! IOTC datagram builders and the inbound decoder. Layouts: `docs/vtech-link-design/20-iotc.md`
//! ("Outbound datagrams, byte by byte", "The `0x602` reply"); reference `tutk-host/iotc/handshake.c`,
//! `wire.c`, `inbound.c`.

use hmac::{Hmac, KeyInit, Mac};
use sha1::Sha1;

use super::scramble::{scramble, unscramble};

pub const MAGIC: u16 = 0x0204;
pub const OUR_VERSION: u8 = 0x1d;
/// The lowest camera protocol version that does key authentication.
pub const KEY_AUTH_MIN_VERSION: u8 = 0x17;
pub const FLAG_PE: u8 = 0x01;
pub const FLAG_SCRAMBLED: u8 = 0x02;
pub const FLAG_SUBHEADER: u8 = 0x08;
/// Bytes scrambled when partial encryption is on.
pub const PE_PREFIX: usize = 0x40;
pub const HEADER_LEN: usize = 16;
pub const SUBHEADER_LEN: usize = 12;
/// The most channel payload one data datagram carries.
pub const MAX_PAYLOAD: usize = 0x56c;
/// The largest datagram accepted.
pub const MAX_DATAGRAM: usize = 2048;
pub const SDK_VERSION: u32 = 0x0403_08c8;

pub const OP_PROBE: u16 = 0x0601;
pub const OP_LAN_REPLY: u16 = 0x0602;
pub const OP_CONNECT_ACK: u16 = 0x0402;
pub const OP_DATA_OUT: u16 = 0x0407;
pub const OP_DATA_IN: u16 = 0x0408;
pub const OP_CLOSE_OUT: u16 = 0x0417;
pub const OP_CLOSE_IN: u16 = 0x0418;
pub const OP_ALIVE_OUT: u16 = 0x0427;
pub const OP_ALIVE_IN: u16 = 0x0428;

pub const PROBE_LEN: usize = 0x58;
pub const RECORD_LEN: usize = 0x34;
pub const CONNECT_ACK_LEN: usize = 0x34;
pub const TOKEN_DATAGRAM_LEN: usize = HEADER_LEN + 8;

const RECORD_MAGIC: u16 = 0xcc51;
const REPLY_SHORT_MIN: u16 = 0x24;
const REPLY_LONG_MIN: u16 = 0xb8;
const REPLY_UID_AT: usize = 0x10;
const REPLY_TOKEN_AT: usize = 0xbc;
const REPLY_RESULT_AT: usize = 0xc4;

/// What identifies us to the camera on one connect.
#[derive(Clone)]
pub struct Ids {
    /// Uppercased, NUL-padded.
    pub uid: [u8; 20],
    pub authkey: [u8; 8],
    /// Random per connect.
    pub token: [u8; 8],
    pub sdk_version: u32,
}

fn put16(buf: &mut [u8], at: usize, v: u16) {
    if let Some(dst) = buf.get_mut(at..at + 2) {
        dst.copy_from_slice(&v.to_le_bytes());
    }
}

fn put32(buf: &mut [u8], at: usize, v: u32) {
    if let Some(dst) = buf.get_mut(at..at + 4) {
        dst.copy_from_slice(&v.to_le_bytes());
    }
}

fn put(buf: &mut [u8], at: usize, src: &[u8]) {
    if let Some(dst) = buf.get_mut(at..at + src.len()) {
        dst.copy_from_slice(src);
    }
}

fn get16(buf: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(buf.get(at..at + 2)?.try_into().ok()?))
}

fn get32(buf: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(buf.get(at..at + 4)?.try_into().ok()?))
}

fn get8(buf: &[u8], at: usize) -> Option<[u8; 8]> {
    buf.get(at..at + 8)?.try_into().ok()
}

/// The 16-byte header with our version (`wire.c:134` `iotc_put_header`).
fn put_header(pkt: &mut [u8], flags: u8, length: u16, opcode: u16, field_a: u16) {
    put16(pkt, 0, MAGIC);
    put(pkt, 2, &[OUR_VERSION, flags]);
    put16(pkt, 4, length);
    put16(pkt, 8, opcode);
    put16(pkt, 10, field_a);
}

/// Mark a built datagram scrambled and scramble it: the first `PE_PREFIX` bytes when its PE flag is
/// set, else all of it (`handshake.c:43`).
pub fn seal(pkt: &mut [u8]) {
    let Some(flags) = pkt.get_mut(3) else { return };
    *flags |= FLAG_SCRAMBLED;
    let prefix = if *flags & FLAG_PE != 0 {
        PE_PREFIX
    } else {
        pkt.len()
    };
    scramble(pkt, prefix);
}

/// The 88-byte connect-form discovery probe, before sealing.
pub fn probe(ids: &Ids) -> [u8; PROBE_LEN] {
    let mut p = [0u8; PROBE_LEN];
    put_header(&mut p, 0, 0x48, OP_PROBE, 0x0021);
    p[0x0f] = 1;
    put(&mut p, 0x10, &ids.uid);
    put32(&mut p, 0x34, ids.sdk_version);
    put(&mut p, 0x38, &ids.token);
    put32(&mut p, 0x40, 1);
    put(&mut p, 0x4a, &ids.authkey);
    p
}

/// The 52-byte LAN connect record, sent as is (never scrambled). Its HMAC-SHA1 is keyed by the UID
/// padded to 20 bytes followed by the authKey.
pub fn record(ids: &Ids) -> [u8; RECORD_LEN] {
    let mut r = [0u8; RECORD_LEN];
    put16(&mut r, 0, RECORD_MAGIC);
    put(&mut r, 4, &[2, 0x10]);
    put16(&mut r, 6, 0x28);
    put(&mut r, 0x10, &ids.token);
    put32(&mut r, 0x18, ids.sdk_version);
    r[0x1c] = OUR_VERSION;
    let mut key = [0u8; 28];
    key[..20].copy_from_slice(&ids.uid);
    key[20..].copy_from_slice(&ids.authkey);
    // HMAC accepts a key of any length, so this never fails.
    if let Ok(mut mac) = Hmac::<Sha1>::new_from_slice(&key) {
        mac.update(&r[..0x20]);
        put(&mut r, 0x20, &mac.finalize().into_bytes());
    }
    r
}

/// The 52-byte `0x0402` connect ack, before sealing.
pub fn connect_ack(ids: &Ids) -> [u8; CONNECT_ACK_LEN] {
    let mut a = [0u8; CONNECT_ACK_LEN];
    put_header(&mut a, 0, 0x24, OP_CONNECT_ACK, 0x0033);
    put(&mut a, 0x10, &ids.uid);
    put(&mut a, 0x24, &ids.token);
    put32(&mut a, 0x30, ids.sdk_version);
    a
}

/// A `0x0407` data datagram carrying `payload` (at most `MAX_PAYLOAD` bytes), before sealing.
pub fn data(token: &[u8; 8], channel: u8, datatype: u8, seq: u16, pe: bool, payload: &[u8]) -> Vec<u8> {
    let mut p = vec![0u8; HEADER_LEN + SUBHEADER_LEN];
    let flags = FLAG_SUBHEADER | if pe { FLAG_PE } else { 0 };
    let length = u16::try_from(SUBHEADER_LEN + payload.len()).unwrap_or(u16::MAX);
    put_header(&mut p, flags, length, OP_DATA_OUT, 0x0021);
    put16(&mut p, 6, seq);
    put(&mut p, 0x0c, &token[..2]);
    put(&mut p, 0x0e, &[channel, datatype]);
    put32(&mut p, 0x10, 12);
    put(&mut p, 0x14, token);
    p.extend_from_slice(payload);
    p
}

/// A keepalive (`OP_ALIVE_OUT`) or close (`OP_CLOSE_OUT`) datagram: the header and the token, before
/// sealing.
pub fn token_datagram(token: &[u8; 8], opcode: u16) -> [u8; TOKEN_DATAGRAM_LEN] {
    let mut p = [0u8; TOKEN_DATAGRAM_LEN];
    put_header(&mut p, FLAG_SUBHEADER, 8, opcode, 0x0021);
    put(&mut p, HEADER_LEN, token);
    p
}

/// How an inbound datagram names its session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenRef {
    /// The full token, when the datagram has the sub-header flag and is long enough to carry it.
    Full([u8; 8]),
    /// Otherwise the token's first two bytes, from header `+0x0c`.
    Low16([u8; 2]),
}

impl TokenRef {
    pub fn matches(&self, token: &[u8; 8]) -> bool {
        match self {
            Self::Full(t) => t == token,
            Self::Low16(t) => t[..] == token[..2],
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum LanReply {
    /// The authenticated answer to our probe.
    Long {
        token: [u8; 8],
        result: u8,
        proto_version: u8,
        iotc_version: u32,
        pe_capable: bool,
    },
    /// The no-auth "device found" reply.
    Short { uid: [u8; 20], proto_version: u8 },
}

#[derive(Debug, PartialEq, Eq)]
pub enum Inbound<'a> {
    LanReply(LanReply),
    Data {
        token: TokenRef,
        channel: u8,
        datatype: u8,
        seq: u16,
        payload: &'a [u8],
    },
    Alive(TokenRef),
    Close(TokenRef),
    /// A valid header with an opcode we don't handle.
    Other(u16),
}

/// Why `decode` dropped a datagram.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Drop {
    /// Under 16 or over 2048 bytes.
    Size,
    /// No `0x0204` magic once unscrambled.
    Magic,
    /// A known opcode whose length fields don't fit the datagram.
    Malformed,
}

fn token_ref(pkt: &[u8], token_at: usize) -> Option<TokenRef> {
    let flags = *pkt.get(3)?;
    if flags & FLAG_SUBHEADER != 0 {
        if let Some(t) = get8(pkt, token_at) {
            return Some(TokenRef::Full(t));
        }
    }
    Some(TokenRef::Low16(pkt.get(0x0c..0x0e)?.try_into().ok()?))
}

fn lan_reply(pkt: &[u8]) -> Option<LanReply> {
    let body = get16(pkt, 4)?;
    let proto_version = *pkt.get(2)?;
    if body >= REPLY_LONG_MIN && pkt.len() > REPLY_RESULT_AT {
        Some(LanReply::Long {
            token: get8(pkt, REPLY_TOKEN_AT)?,
            result: *pkt.get(REPLY_RESULT_AT)?,
            proto_version,
            iotc_version: get32(pkt, 0x30)?,
            pe_capable: pkt.get(3)? & FLAG_SCRAMBLED != 0,
        })
    } else if body >= REPLY_SHORT_MIN && pkt.len() >= REPLY_UID_AT + 20 {
        Some(LanReply::Short {
            uid: pkt.get(REPLY_UID_AT..REPLY_UID_AT + 20)?.try_into().ok()?,
            proto_version,
        })
    } else {
        None
    }
}

/// Unscramble a received datagram in place and decode it (`inbound.c` `iotc_on_datagram`).
pub fn decode(pkt: &mut [u8]) -> Result<Inbound<'_>, Drop> {
    if pkt.len() < HEADER_LEN || pkt.len() > MAX_DATAGRAM {
        return Err(Drop::Size);
    }
    let (header, body) = pkt.split_at_mut(HEADER_LEN);
    unscramble(header, HEADER_LEN);
    if get16(header, 0) != Some(MAGIC) {
        return Err(Drop::Magic);
    }
    // The scramble works block by block from offset 0, so the rest can be done on its own.
    let flags = header.get(3).copied().unwrap_or(0);
    let prefix = if flags & FLAG_PE != 0 {
        PE_PREFIX
    } else {
        HEADER_LEN + body.len()
    };
    unscramble(body, prefix.saturating_sub(HEADER_LEN));
    let pkt: &[u8] = pkt;
    let mut hdr = [0u8; HEADER_LEN];
    hdr.copy_from_slice(pkt.get(..HEADER_LEN).ok_or(Drop::Size)?);
    let opcode = u16::from_le_bytes([hdr[8], hdr[9]]);
    Ok(match opcode {
        OP_LAN_REPLY => Inbound::LanReply(lan_reply(pkt).ok_or(Drop::Malformed)?),
        OP_DATA_IN => {
            let token = token_ref(pkt, HEADER_LEN + 4).ok_or(Drop::Malformed)?;
            let skip = if hdr[3] & FLAG_SUBHEADER != 0 {
                SUBHEADER_LEN
            } else {
                0
            };
            let length = usize::from(u16::from_le_bytes([hdr[4], hdr[5]]));
            let n = length.checked_sub(skip).ok_or(Drop::Malformed)?;
            let start = HEADER_LEN + skip;
            Inbound::Data {
                token,
                channel: hdr[0x0e],
                datatype: hdr[0x0f] & 3,
                seq: u16::from_le_bytes([hdr[6], hdr[7]]),
                payload: pkt.get(start..start + n).ok_or(Drop::Malformed)?,
            }
        }
        OP_ALIVE_IN => Inbound::Alive(token_ref(pkt, HEADER_LEN).ok_or(Drop::Malformed)?),
        OP_CLOSE_IN => Inbound::Close(token_ref(pkt, HEADER_LEN).ok_or(Drop::Malformed)?),
        other => Inbound::Other(other),
    })
}
