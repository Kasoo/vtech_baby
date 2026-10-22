//! AV packet codecs: the 8-byte header and the per-class bodies (login, login reply, ACK, report, RTT,
//! the FEC envelope and the control frame). Layouts: `docs/vtech-link-design/40-av-client.md`;
//! reference `tutk-host/av/session.c` (`build_login`), `av/transport.c`, `av/ioctrl.c`.

pub const CLASS_LOGIN: u8 = 0x00;
pub const CLASS_ACK: u8 = 0x09;
pub const CLASS_REPORT: u8 = 0x0a;
pub const CLASS_RTT: u8 = 0x0b;
pub const CLASS_DATA: u8 = 0x0c;

pub const VERSION: u16 = 0x000c;
/// Header flag: an 8-byte stats block follows the header.
pub const FLAG_STATS: u8 = 0x08;

pub const LOGIN_REPLY: u8 = 0x21;
/// The login width for a camera protocol version of 9 or more (`av/session.c:229`).
pub const LOGIN_WIDTH: usize = 0x101;

/// Control classes at frame `+0x01`.
pub const CTRL_USER: u8 = 0x70;
pub const CTRL_INTERNAL: u8 = 0x10;
/// Bytes before a control frame's payload, from the frame's start.
pub const CTRL_HEADER: usize = 0x18;
/// Where the frame starts in a class-0x0c packet without a stats block: header + group header.
pub const FRAME_OFFSET: usize = 0x10;
/// The most payload a single-fragment control frame carries.
pub const CTRL_MAX_PAYLOAD: usize = 1020;
/// The media and parity sub-header length; also the shortest frame accepted.
pub const SUBHEADER: usize = 0x14;
/// The most NAK entries in one ACK.
pub const MAX_NAKS: usize = 32;

/// The opcode-support block every login advertises (`av/session.c` `av_opcode_support`).
pub const OPCODE_SUPPORT: [u8; 20] = [
    0x04, 0, 0, 0, 0xfb, 0x07, 0x1f, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x03, 0,
];

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

pub fn get16(buf: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(buf.get(at..at + 2)?.try_into().ok()?))
}

pub fn get32(buf: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(buf.get(at..at + 4)?.try_into().ok()?))
}

fn header(pkt: &mut [u8], class: u8, flags: u8) {
    put(pkt, 0, &[class, flags]);
    put16(pkt, 2, VERSION);
}

/// Write the sender's running packet count, which goes on classes 0x09, 0x0a and 0x0c only
/// (`av/session.c:37`).
pub fn stamp(pkt: &mut [u8], counter: u16) -> bool {
    if matches!(pkt.first(), Some(&(CLASS_ACK | CLASS_REPORT | CLASS_DATA))) {
        put16(pkt, 4, counter);
        true
    } else {
        false
    }
}

/// Login packet `n` (0 or 1) with credential mode 0: the password goes in clear inside DTLS
/// (`av/session.c` `build_login`). `account` and `password` are truncated to `LOGIN_WIDTH - 1` bytes.
pub fn login(n: u8, nonce: u32, account: &str, password: &str, hs: u16) -> Vec<u8> {
    let w = LOGIN_WIDTH;
    let extra = 2 * usize::from(n.min(1));
    let mut p = vec![0u8; 2 * w + 0x38 + extra];
    header(&mut p, CLASS_LOGIN, if n == 0 { 0 } else { 0x20 });
    put16(
        &mut p,
        0x10,
        u16::try_from(2 * w + 0x20 + extra).unwrap_or(u16::MAX),
    );
    put16(&mut p, 0x12, hs);
    put32(&mut p, 0x14, nonce.wrapping_add(u32::from(n)));
    let field = |s: &str| {
        s.as_bytes()
            .get(..s.len().min(w - 1))
            .unwrap_or_default()
            .to_vec()
    };
    put(&mut p, 0x18, &field(account));
    put(&mut p, 0x18 + w, &field(password));
    put16(&mut p, 0x18 + 2 * w, hs);
    put(&mut p, 0x1c + 2 * w, &OPCODE_SUPPORT);
    put32(&mut p, 0x30 + 2 * w, 0);
    put32(&mut p, 0x34 + 2 * w, 1);
    p
}

/// The state byte of a login reply to `nonce` (class 0, sub-type 0x21, carrying `nonce` or
/// `nonce + 1`); `None` if `pkt` is not one.
pub fn login_reply(pkt: &[u8], nonce: u32) -> Option<u8> {
    if pkt.len() < 0x1c || pkt.first() != Some(&CLASS_LOGIN) || pkt.get(1) != Some(&LOGIN_REPLY) {
        return None;
    }
    let echoed = get32(pkt, 0x14)?;
    (echoed == nonce || echoed == nonce.wrapping_add(1)).then(|| pkt.get(0x18).copied())?
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ack<'a> {
    /// Every media packet up to here received (`0xffff` before the first).
    pub pn_cum: u16,
    pub pn_high: u16,
    /// The camera's highest delivered control frame (`0xffff` before the first).
    pub ctrl_in: u16,
    pub urgent: bool,
    pub seq: u16,
    pub clock: u16,
    /// Missing packets as offsets from `pn_cum`; at most `MAX_NAKS` are sent.
    pub naks: &'a [u16],
}

/// An ACK (class 0x09): 24 bytes, or `0x16 + 2n` with `n` NAKs (`av/transport.c:114`).
pub fn ack(a: &Ack<'_>) -> Vec<u8> {
    let naks = a.naks.get(..a.naks.len().min(MAX_NAKS)).unwrap_or_default();
    let mut p = vec![0u8; (0x16 + 2 * naks.len()).max(24)];
    header(&mut p, CLASS_ACK, 0);
    put16(&mut p, 0x08, a.pn_cum);
    put16(&mut p, 0x0a, a.pn_high);
    put16(&mut p, 0x0c, a.ctrl_in);
    put16(&mut p, 0x0e, u16::try_from(naks.len()).unwrap_or(0));
    put16(&mut p, 0x10, u16::from(a.urgent));
    put16(&mut p, 0x12, a.seq);
    put16(&mut p, 0x14, a.clock);
    for (i, &nak) in naks.iter().enumerate() {
        put16(&mut p, 0x16 + 2 * i, nak);
    }
    p
}

/// A report (class 0x0a, flags 0x08), 16 bytes (`av/transport.c:141`).
pub fn report(clock: u16, rtt_ms: u16) -> [u8; 16] {
    let mut p = [0u8; 16];
    header(&mut p, CLASS_REPORT, FLAG_STATS);
    put16(&mut p, 0x08, clock);
    put16(&mut p, 0x0a, rtt_ms);
    p
}

/// The answer to a report (class 0x0b), 20 bytes, echoing its timestamp (`av/transport.c:152`).
pub fn rtt(echo: u16, rtt_ms: u16) -> [u8; 20] {
    let mut p = [0u8; 20];
    header(&mut p, CLASS_RTT, 0);
    put16(&mut p, 0x08, echo);
    put16(&mut p, 0x0a, rtt_ms);
    put16(&mut p, 0x0c, rtt_ms);
    p
}

/// A single-fragment control frame (class 0x0c) carrying `typ` and `payload` (at most
/// `CTRL_MAX_PAYLOAD` bytes; longer payloads are cut).
pub fn ctrl_frame(class: u8, packet_number: u16, frame_number: u32, typ: u32, payload: &[u8]) -> Vec<u8> {
    let payload = payload
        .get(..payload.len().min(CTRL_MAX_PAYLOAD))
        .unwrap_or_default();
    let mut p = vec![0u8; FRAME_OFFSET + CTRL_HEADER + payload.len()];
    header(&mut p, CLASS_DATA, 0);
    let f = FRAME_OFFSET;
    put(&mut p, f + 1, &[class]);
    put16(&mut p, f + 0x02, packet_number);
    put16(&mut p, f + 0x04, 1);
    put16(&mut p, f + 0x06, 0);
    put16(&mut p, f + 0x08, u16::try_from(payload.len() + 4).unwrap_or(0));
    put32(&mut p, f + 0x0c, frame_number);
    put32(&mut p, f + 0x14, typ);
    put(&mut p, f + CTRL_HEADER, payload);
    p
}

/// Where an inbound packet's body starts: after the header, and the stats block when flagged.
pub fn body_offset(pkt: &[u8]) -> Option<usize> {
    let flags = *pkt.get(1)?;
    let b = if flags & FLAG_STATS != 0 { 0x10 } else { 0x08 };
    (pkt.len() >= b).then_some(b)
}

/// A class-0x0c packet split into its FEC group header and its frame (`av/transport.c:60`).
pub struct Envelope<'a> {
    pub group: &'a [u8],
    pub frame: &'a [u8],
}

pub fn envelope(pkt: &[u8]) -> Option<Envelope<'_>> {
    let b = body_offset(pkt)?;
    let group = pkt.get(b..b + 8)?;
    let frame = pkt.get(b + 8..)?;
    (frame.len() >= SUBHEADER).then_some(Envelope { group, frame })
}

/// One fragment of an inbound control message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CtrlFragment<'a> {
    pub class: u8,
    /// The sender's per-fragment counter; only a duplicate gate.
    pub packet_number: u16,
    pub count: u16,
    pub index: u16,
    /// The message's number, shared by all its fragments.
    pub frame_number: u32,
    /// Fragment 0: the type and payload; later fragments: a raw chunk of payload.
    pub body: &'a [u8],
}

/// Parse a control frame (`frame[0] == 0`). `None` if its fields don't fit or contradict.
pub fn ctrl_fragment(frame: &[u8]) -> Option<CtrlFragment<'_>> {
    if frame.first() != Some(&0) {
        return None;
    }
    let count = get16(frame, 0x04)?;
    let index = get16(frame, 0x06)?;
    let len = usize::from(get16(frame, 0x08)?);
    if count == 0 || index >= count || (index == 0 && len < 4) {
        return None;
    }
    Some(CtrlFragment {
        class: *frame.get(1)?,
        packet_number: get16(frame, 0x02)?,
        count,
        index,
        frame_number: get32(frame, 0x0c)?,
        body: frame.get(SUBHEADER..SUBHEADER + len)?,
    })
}
