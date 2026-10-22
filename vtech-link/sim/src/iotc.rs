//! The camera's side of IOTC: parsing what the client sends and building what the camera answers. The
//! client's own codec does the scrambling and the header checks; the camera's datagrams are the client's
//! builders with the inbound opcode swapped in.

use vtech_link_core::iotc::codec::{self, Inbound};

/// The connect record's magic, `0xcc51` little-endian.
const RECORD_MAGIC: [u8; 2] = [0x51, 0xcc];
/// The long `0x602` reply: the body length the client checks for, and the whole datagram.
const LAN_REPLY_BODY: u16 = 0xb8;
const LAN_REPLY_LEN: usize = 0xc8;

/// One datagram from a client, unscrambled and decoded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FromClient {
    /// The unscrambled LAN connect record; carries nothing the camera needs beyond the probe.
    Record,
    Probe {
        uid: [u8; 20],
        token: [u8; 8],
        authkey: [u8; 8],
    },
    ConnectAck {
        token: [u8; 8],
    },
    Data {
        token: [u8; 8],
        channel: u8,
        datatype: u8,
        payload: Vec<u8>,
    },
    Alive {
        token: [u8; 8],
    },
    Close {
        token: [u8; 8],
    },
}

fn array<const N: usize>(buf: &[u8], at: usize) -> Option<[u8; N]> {
    buf.get(at..at + N)?.try_into().ok()
}

/// Decode a client datagram in place; `None` for anything malformed or unexpected.
pub(crate) fn parse(buf: &mut [u8]) -> Option<FromClient> {
    if buf.len() == codec::RECORD_LEN && buf.get(..2) == Some(&RECORD_MAGIC[..]) {
        return Some(FromClient::Record);
    }
    // The core's decoder unscrambles in place and checks size and magic; the client's opcodes are
    // all "other" to it, so the fields are read here.
    let opcode = match codec::decode(buf) {
        Ok(Inbound::Other(op)) => op,
        _ => return None,
    };
    let buf: &[u8] = buf;
    match opcode {
        codec::OP_PROBE => Some(FromClient::Probe {
            uid: array(buf, 0x10)?,
            token: array(buf, 0x38)?,
            authkey: array(buf, 0x4a)?,
        }),
        codec::OP_CONNECT_ACK => Some(FromClient::ConnectAck {
            token: array(buf, 0x24)?,
        }),
        codec::OP_DATA_OUT => {
            let length = usize::from(u16::from_le_bytes(array(buf, 4)?));
            let start = codec::HEADER_LEN + codec::SUBHEADER_LEN;
            let n = length.checked_sub(codec::SUBHEADER_LEN)?;
            Some(FromClient::Data {
                token: array(buf, 0x14)?,
                channel: *buf.get(0x0e)?,
                datatype: buf.get(0x0f)? & 3,
                payload: buf.get(start..start + n)?.to_vec(),
            })
        }
        codec::OP_ALIVE_OUT => Some(FromClient::Alive {
            token: array(buf, codec::HEADER_LEN)?,
        }),
        codec::OP_CLOSE_OUT => Some(FromClient::Close {
            token: array(buf, codec::HEADER_LEN)?,
        }),
        _ => None,
    }
}

/// Swap in the camera's opcode and protocol version, then scramble.
fn as_camera(mut pkt: Vec<u8>, opcode: u16, version: u8) -> Vec<u8> {
    pkt[2] = version;
    pkt[8..10].copy_from_slice(&opcode.to_le_bytes());
    codec::seal(&mut pkt);
    pkt
}

/// The long `0x602` answer to a probe. Sealing sets the scrambled flag, which tells the client the
/// camera allows partial encryption.
pub(crate) fn lan_reply(
    uid: &[u8; 20],
    token: &[u8; 8],
    result: u8,
    version: u8,
    iotc_version: u32,
) -> Vec<u8> {
    let mut p = vec![0u8; LAN_REPLY_LEN];
    p[0..2].copy_from_slice(&codec::MAGIC.to_le_bytes());
    p[4..6].copy_from_slice(&LAN_REPLY_BODY.to_le_bytes());
    p[0x10..0x24].copy_from_slice(uid);
    p[0x30..0x34].copy_from_slice(&iotc_version.to_le_bytes());
    p[0xbc..0xc4].copy_from_slice(token);
    p[0xc4] = result;
    as_camera(p, codec::OP_LAN_REPLY, version)
}

/// A `0x408` data datagram to the client, scrambled over its first 64 bytes as the camera does once
/// partial encryption is on.
pub(crate) fn data(
    token: &[u8; 8],
    channel: u8,
    datatype: u8,
    seq: u16,
    payload: &[u8],
    version: u8,
) -> Vec<u8> {
    let pkt = codec::data(token, channel, datatype, seq, true, payload);
    as_camera(pkt, codec::OP_DATA_IN, version)
}

/// A keepalive answer (`0x428`) or close (`0x418`).
pub(crate) fn token_datagram(token: &[u8; 8], opcode: u16, version: u8) -> Vec<u8> {
    let pkt = codec::token_datagram(token, codec::OP_ALIVE_OUT).to_vec();
    as_camera(pkt, opcode, version)
}
