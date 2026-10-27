//! `vtech-link`: read-only probes against the camera with no Python. Credentials come from the
//! environment (`VTECH_UID`, `VTECH_AUTHKEY`, `VTECH_PWD`), never the command line.
//!
//!   vtech-link connect [camera-ip]              connect, DEVINFO, close
//!   vtech-link watch   [camera-ip] [seconds]    connect, print pushes, GET_PARAMS every 60 s
//!   vtech-link stream  [camera-ip] [seconds]    live view: count, check and time the frames
//!                                               (`--per-second`: frames, keyframes and the longest
//!                                               gap each second, and each keyframe whose SPS/PPS
//!                                               differ from the last, with the SPS's size)
//!   vtech-link restart [camera-ip] [cycles]     live-view reset/START/STOP cycles on one link: each
//!                                               must start on a keyframe with SPS
//!
//! `--drop N` and `--per-second` may come in any position. `--drop N` drops every Nth inbound media or parity packet, to exercise parity
//! rebuilding and resends on a clean network. `VTECH_LOG=debug` for more. Nothing here changes a camera
//! setting or makes light or sound.

use std::net::Ipv4Addr;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use vtech_link_core::av::StreamKind;
use vtech_link_core::{Frame, Link, LinkConfig, LinkError};

const DEVINFO: u32 = 0x330;
const DEVINFO_REPLY: u32 = 0x331;
const GET_PARAMS: u32 = 0x733;
const GET_PARAMS_REPLY: u32 = 0x734;
const START: u32 = 0x1ff;
const STOP: u32 = 0x2ff;
const AUDIO_START: u32 = 0x300;
const AUDIO_STOP: u32 = 0x301;
const VIDEO_FULLRATE: u32 = 0x719;
const GET_STREAM_CTRL: u32 = 0x322;
const GET_STREAM_CTRL_REPLY: u32 = 0x323;
const CHANNEL_0: [u8; 8] = [0; 8];

struct StderrLog(log::LevelFilter, Instant);

impl log::Log for StderrLog {
    fn enabled(&self, m: &log::Metadata<'_>) -> bool {
        m.level() <= self.0
    }

    fn log(&self, r: &log::Record<'_>) {
        if self.enabled(r.metadata()) {
            eprintln!(
                "{:>7.3} {:5} {}: {}",
                self.1.elapsed().as_secs_f64(),
                r.level(),
                r.target(),
                r.args()
            );
        }
    }

    fn flush(&self) {}
}

fn env(name: &str) -> Result<String, String> {
    std::env::var(name).map_err(|_| format!("{name} not set"))
}

fn config(peer: Option<Ipv4Addr>, drop: u32) -> Result<LinkConfig, String> {
    let mut c = LinkConfig::new(&env("VTECH_UID")?, &env("VTECH_AUTHKEY")?, &env("VTECH_PWD")?);
    c.peer_hint = peer;
    c.drop_media_every = drop;
    Ok(c)
}

fn connect(peer: Option<Ipv4Addr>, drop: u32) -> Result<Link, String> {
    let t0 = Instant::now();
    let link = Link::connect(config(peer, drop)?).map_err(|e| format!("connect: {e}"))?;
    let info = link.info();
    println!("connected in {} ms: {info:?}", t0.elapsed().as_millis());
    Ok(link)
}

fn devinfo(link: &Link) -> Result<(), String> {
    let t0 = Instant::now();
    let payload = [0, 1, 0x12, 0x34, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    let r = link
        .request(DEVINFO, &payload, DEVINFO_REPLY, Duration::from_secs(5))
        .map_err(|e| format!("DEVINFO: {e}"))?;
    let text =
        |b: &[u8]| String::from_utf8_lossy(b.split(|&c| c == 0).next().unwrap_or_default()).into_owned();
    let model = text(r.get(..16).unwrap_or_default());
    let vendor = text(r.get(16..32).unwrap_or_default());
    println!(
        "DEVINFO in {} ms: model {model:?}, vendor {vendor:?}",
        t0.elapsed().as_millis()
    );
    Ok(())
}

fn watch(link: &Link, seconds: u64) -> Result<(), String> {
    let end = Instant::now() + Duration::from_secs(seconds);
    let mut next_get = Instant::now();
    while Instant::now() < end {
        if Instant::now() >= next_get {
            let t0 = Instant::now();
            let r = link.request(GET_PARAMS, &[0; 4], GET_PARAMS_REPLY, Duration::from_secs(5));
            match r {
                Ok(p) => println!("GET_PARAMS: {} bytes in {} ms", p.len(), t0.elapsed().as_millis()),
                Err(e) => println!("GET_PARAMS: {e}"),
            }
            next_get += Duration::from_secs(60);
        }
        match link.next_push(Duration::from_secs(1)) {
            Ok(Some((typ, payload))) => println!("push 0x{typ:x}: {} bytes", payload.len()),
            Ok(None) => {}
            Err(e) => return Err(format!("link lost: {e}")),
        }
    }
    print_stats(link);
    Ok(())
}

/// The resolution the camera will stream (read-only: `GET_STREAM_CTRL`, quality byte at 4).
fn resolution(link: &Link) -> Result<(), String> {
    let r = link
        .request(
            GET_STREAM_CTRL,
            &CHANNEL_0,
            GET_STREAM_CTRL_REPLY,
            Duration::from_secs(5),
        )
        .map_err(|e| format!("GET_STREAM_CTRL: {e}"))?;
    let name = match r.get(4) {
        Some(1) => "720p",
        Some(2) => "1080p",
        _ => "unknown",
    };
    println!("resolution: {name} (quality {:?})", r.get(4));
    Ok(())
}

/// The media receive counters on one line.
fn print_media(link: &Link) {
    let Ok(s) = link.stats(Duration::from_secs(2)) else {
        return;
    };
    let Some(av) = s.av else { return };
    let m = av.media;
    println!(
        "media: dropped on purpose {}, rebuilt from parity {}, NAKed {}, given up video {} audio {}, \
         late video {} audio {}, gaps skipped {}, behind {}, decrypt failed {}",
        m.packets_dropped_test,
        m.packets_recovered,
        m.packets_requested,
        m.video.given_up,
        m.audio.given_up,
        m.video.late,
        m.audio.late,
        m.gaps_skipped,
        m.fragments_behind,
        s.decrypt_failed_video + s.decrypt_failed_audio
    );
}

fn stream(link: &Link, seconds: u64, per_second: bool) -> Result<(), String> {
    resolution(link)?;
    let mut rx = link.media();
    let t0 = Instant::now();
    rx.start(Duration::from_secs(5))
        .map_err(|e| format!("key exchange: {e}"))?;
    println!("key exchange in {} ms", t0.elapsed().as_millis());
    rx.reset(Duration::from_secs(2))
        .map_err(|e| format!("reset: {e}"))?;
    let send = |typ, p: &[u8]| {
        link.send(typ, p, Duration::from_secs(5))
            .map_err(|e| format!("send 0x{typ:x}: {e}"))
    };
    send(AUDIO_START, &CHANNEL_0)?;
    send(VIDEO_FULLRATE, &[0; 8])?;
    send(START, &CHANNEL_0)?;
    let started = Instant::now();
    let end = started + Duration::from_secs(seconds);
    let mut each_second = per_second.then(|| PerSecond::new(started));
    let (mut video, mut audio, mut keys, mut lost, mut bytes) = (0u64, 0u64, 0u64, 0u64, 0u64);
    let mut first: Option<Duration> = None;
    let mut first_key: Option<Duration> = None;
    // The per-stream count (`+0x10`) of the first and last frame, and how often it didn't step by one.
    let mut seqs: [Option<(u32, u32)>; 2] = [None; 2];
    let mut seq_jumps = [0u64; 2];
    while Instant::now() < end {
        let next = rx.next(Duration::from_millis(500));
        if let Some(p) = each_second.as_mut() {
            p.tick(Instant::now());
        }
        match next {
            Ok(Some(f)) => {
                first.get_or_insert(started.elapsed());
                lost += u64::from(f.lost_before);
                bytes += f.data.len() as u64;
                let i = usize::from(f.stream == StreamKind::Audio);
                if let (Some(s), Some(jumps)) = (seqs.get_mut(i), seq_jumps.get_mut(i)) {
                    match s {
                        Some((_, last)) => {
                            if f.seq != last.wrapping_add(1) {
                                *jumps += 1;
                            }
                            *last = f.seq;
                        }
                        None => *s = Some((f.seq, f.seq)),
                    }
                }
                match f.stream {
                    StreamKind::Video => {
                        video += 1;
                        if f.key {
                            keys += 1;
                            first_key.get_or_insert(started.elapsed());
                        }
                        if let Some(p) = each_second.as_mut() {
                            p.video(&f);
                        }
                    }
                    StreamKind::Audio => audio += 1,
                }
            }
            Ok(None) => {}
            Err(e) => return Err(format!("stream: {e}")),
        }
    }
    let _ = send(STOP, &CHANNEL_0);
    let _ = send(AUDIO_STOP, &CHANNEL_0);
    rx.close();
    let secs = seconds.max(1) as f64;
    println!(
        "stream: {video} video ({keys} key, {:.1}/s), {audio} audio, {lost} lost, {:.0} kbit/s; first frame {:?}, first key {:?}",
        video as f64 / secs,
        bytes as f64 * 8.0 / secs / 1000.0,
        first,
        first_key
    );
    println!(
        "per-stream count: video {:?} ({} jumps), audio {:?} ({} jumps)",
        seqs[0], seq_jumps[0], seqs[1], seq_jumps[1]
    );
    print_media(link);
    print_stats(link);
    Ok(())
}

/// The H.264 NAL unit types in an Annex-B access unit.
/// `stream --per-second`: one line per second, and a line for each keyframe whose headers changed.
struct PerSecond {
    t0: Instant,
    second: u64,
    frames: u64,
    keys: u64,
    max_gap: Duration,
    last_frame: Instant,
    headers: Option<Vec<u8>>,
}

impl PerSecond {
    fn new(t0: Instant) -> Self {
        PerSecond {
            t0,
            second: 0,
            frames: 0,
            keys: 0,
            max_gap: Duration::ZERO,
            last_frame: t0,
            headers: None,
        }
    }

    /// Print the second that just ended, if one has.
    fn tick(&mut self, now: Instant) {
        let second = now.duration_since(self.t0).as_secs();
        if second == self.second {
            return;
        }
        println!(
            "[{:6.2}] {:3}s: frames {:2} keys {} max gap {} ms",
            now.duration_since(self.t0).as_secs_f64(),
            self.second,
            self.frames,
            self.keys,
            self.max_gap.as_millis()
        );
        (self.second, self.frames, self.keys, self.max_gap) = (second, 0, 0, Duration::ZERO);
    }

    fn video(&mut self, f: &Frame) {
        let now = Instant::now();
        self.frames += 1;
        self.max_gap = self.max_gap.max(now.duration_since(self.last_frame));
        self.last_frame = now;
        if !f.key {
            return;
        }
        self.keys += 1;
        let units = nal_units(&f.data);
        let headers: Vec<u8> = units
            .iter()
            .filter(|n| matches!(n.first().map(|b| b & 0x1f), Some(7 | 8)))
            .flat_map(|n| n.iter().copied().chain([0, 0, 1]))
            .collect();
        if self.headers.as_ref() == Some(&headers) {
            return;
        }
        let sps = units
            .iter()
            .find(|n| n.first().map(|b| b & 0x1f) == Some(7))
            .copied()
            .unwrap_or_default();
        let size = sps_size(sps).map_or_else(|| "?".to_owned(), |(w, h)| format!("{w}x{h}"));
        println!(
            "[{:6.2}] keyframe headers {}: sps {} ({} bytes) size {size}",
            now.duration_since(self.t0).as_secs_f64(),
            if self.headers.is_some() {
                "changed"
            } else {
                "first"
            },
            hex(sps.get(..16).unwrap_or(sps)),
            sps.len()
        );
        self.headers = Some(headers);
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// The NAL units of an Annex-B access unit, without start codes or trailing zero bytes.
fn nal_units(data: &[u8]) -> Vec<&[u8]> {
    let mut starts = Vec::new();
    let mut i = 0;
    while let Some(p) = data
        .get(i..)
        .and_then(|d| d.windows(3).position(|w| w == [0, 0, 1]))
    {
        i += p + 3;
        starts.push(i);
    }
    starts
        .iter()
        .enumerate()
        .filter_map(|(k, &a)| {
            let b = starts.get(k + 1).map_or(data.len(), |&n| n - 3);
            let unit = data.get(a..b)?;
            let end = unit.iter().rposition(|&x| x != 0).map_or(0, |p| p + 1);
            unit.get(..end)
        })
        .collect()
}

/// Reads an SPS's RBSP bit by bit (emulation-prevention bytes removed).
struct Bits {
    data: Vec<u8>,
    pos: usize,
}

impl Bits {
    fn new(nal_payload: &[u8]) -> Self {
        let mut data = Vec::with_capacity(nal_payload.len());
        let mut zeros = 0;
        for &b in nal_payload {
            if zeros >= 2 && b == 3 {
                zeros = 0;
                continue;
            }
            zeros = if b == 0 { zeros + 1 } else { 0 };
            data.push(b);
        }
        Bits { data, pos: 0 }
    }

    fn bit(&mut self) -> Option<u32> {
        let byte = self.data.get(self.pos / 8)?;
        let bit = (byte >> (7 - self.pos % 8)) & 1;
        self.pos += 1;
        Some(u32::from(bit))
    }

    fn bits(&mut self, n: u32) -> Option<u32> {
        (0..n).try_fold(0u32, |v, _| Some((v << 1) | self.bit()?))
    }

    /// Unsigned Exp-Golomb.
    fn ue(&mut self) -> Option<u32> {
        let mut zeros = 0;
        while self.bit()? == 0 {
            zeros += 1;
            if zeros > 31 {
                return None;
            }
        }
        Some((1u32 << zeros) - 1 + self.bits(zeros)?)
    }

    /// Signed Exp-Golomb.
    fn se(&mut self) -> Option<i64> {
        let k = i64::from(self.ue()?);
        Some(if k % 2 == 1 { (k + 1) / 2 } else { -(k / 2) })
    }
}

/// The picture size an H.264 SPS NAL unit declares, after cropping.
fn sps_size(sps: &[u8]) -> Option<(u32, u32)> {
    let mut b = Bits::new(sps.get(1..)?);
    let profile = b.bits(8)?;
    b.bits(16)?; // constraint flags, level
    b.ue()?; // seq_parameter_set_id
    let mut chroma = 1;
    if matches!(
        profile,
        44 | 83 | 86 | 100 | 110 | 118 | 122 | 128 | 134 | 135 | 138 | 139 | 244
    ) {
        chroma = b.ue()?;
        if chroma == 3 {
            b.bit()?; // separate_colour_plane_flag
        }
        b.ue()?; // bit depths
        b.ue()?;
        b.bit()?; // qpprime_y_zero_transform_bypass_flag
        if b.bit()? == 1 {
            for i in 0..if chroma == 3 { 12 } else { 8 } {
                if b.bit()? == 1 {
                    skip_scaling_list(&mut b, if i < 6 { 16 } else { 64 })?;
                }
            }
        }
    }
    b.ue()?; // log2_max_frame_num_minus4
    match b.ue()? {
        0 => {
            b.ue()?;
        }
        1 => {
            b.bit()?;
            b.se()?;
            b.se()?;
            for _ in 0..b.ue()? {
                b.se()?;
            }
        }
        _ => {}
    }
    b.ue()?; // max_num_ref_frames
    b.bit()?; // gaps_in_frame_num_value_allowed_flag
    let width_mbs = b.ue()?.checked_add(1)?;
    let height_units = b.ue()?.checked_add(1)?;
    let frame_mbs_only = b.bit()?;
    if frame_mbs_only == 0 {
        b.bit()?; // mb_adaptive_frame_field_flag
    }
    b.bit()?; // direct_8x8_inference_flag
    let (mut left, mut right, mut top, mut bottom) = (0, 0, 0, 0);
    if b.bit()? == 1 {
        (left, right, top, bottom) = (b.ue()?, b.ue()?, b.ue()?, b.ue()?);
    }
    let field = 2 - frame_mbs_only;
    let (crop_x, crop_y) = match chroma {
        1 => (2, 2 * field),
        2 => (2, field),
        _ => (1, field),
    };
    let width = width_mbs
        .checked_mul(16)?
        .checked_sub(crop_x * left.checked_add(right)?)?;
    let height = (field * height_units)
        .checked_mul(16)?
        .checked_sub(crop_y * top.checked_add(bottom)?)?;
    Some((width, height))
}

fn skip_scaling_list(b: &mut Bits, size: usize) -> Option<()> {
    let (mut last, mut next) = (8i64, 8i64);
    for _ in 0..size {
        if next != 0 {
            next = (last + b.se()?).rem_euclid(256);
        }
        if next != 0 {
            last = next;
        }
    }
    Some(())
}

fn nal_types(data: &[u8]) -> Vec<u8> {
    data.windows(4)
        .filter(|w| w.get(..3) == Some(&[0, 0, 1]))
        .filter_map(|w| w.get(3).map(|b| b & 0x1f))
        .collect()
}

/// `cycles` live views on one link, each after a media reset: the first video frame must be a keyframe
/// with SPS, and nothing may be lost.
fn restart(link: &Link, cycles: u64) -> Result<(), String> {
    const VIEW: Duration = Duration::from_secs(3);
    let send = |typ, p: &[u8]| {
        link.send(typ, p, Duration::from_secs(5))
            .map_err(|e| format!("send 0x{typ:x}: {e}"))
    };
    let mut rx = link.media();
    rx.start(Duration::from_secs(5))
        .map_err(|e| format!("key exchange: {e}"))?;
    let (mut good, mut lost_total) = (0u64, 0u64);
    for cycle in 1..=cycles {
        let t0 = Instant::now();
        rx.reset(Duration::from_secs(2))
            .map_err(|e| format!("reset: {e}"))?;
        send(AUDIO_START, &CHANNEL_0)?;
        send(VIDEO_FULLRATE, &[0; 8])?;
        send(START, &CHANNEL_0)?;
        let (mut video, mut audio, mut lost) = (0u64, 0u64, 0u64);
        let mut first: Option<(Duration, bool, Vec<u8>)> = None;
        while t0.elapsed() < VIEW {
            let f: Frame = match rx.next(Duration::from_millis(200)) {
                Ok(Some(f)) => f,
                Ok(None) => continue,
                Err(e) => return Err(format!("cycle {cycle}: {e}")),
            };
            lost += u64::from(f.lost_before);
            match f.stream {
                StreamKind::Video => {
                    video += 1;
                    first.get_or_insert_with(|| (t0.elapsed(), f.key, nal_types(&f.data)));
                }
                StreamKind::Audio => audio += 1,
            }
        }
        send(STOP, &CHANNEL_0)?;
        send(AUDIO_STOP, &CHANNEL_0)?;
        let ok = first
            .as_ref()
            .is_some_and(|(_, key, nals)| *key && nals.contains(&7) && nals.contains(&5))
            && lost == 0;
        good += u64::from(ok);
        lost_total += lost;
        match &first {
            Some((at, key, nals)) => println!(
                "cycle {cycle:2}: {} first video after {} ms, key {key}, NALs {nals:?}; {video} video, \
                 {audio} audio, {lost} lost",
                if ok { "ok  " } else { "FAIL" },
                at.as_millis()
            ),
            None => println!("cycle {cycle:2}: FAIL no video in {} s", VIEW.as_secs()),
        }
        std::thread::sleep(Duration::from_millis(1500));
    }
    rx.close();
    println!(
        "restart: {good}/{cycles} cycles started on a keyframe with SPS and lost nothing; {lost_total} lost"
    );
    print_media(link);
    print_stats(link);
    if good == cycles {
        Ok(())
    } else {
        Err(format!("{} of {cycles} cycles failed", cycles - good))
    }
}

fn print_stats(link: &Link) {
    match link.stats(Duration::from_secs(2)) {
        Ok(s) => println!("stats: {s:#?}"),
        Err(e) => println!("stats: {e}"),
    }
}

fn run(args: &[String]) -> Result<(), String> {
    // `--drop N` and `--per-second` may come anywhere; the rest are positional.
    let mut drop = 0;
    let mut per_second = false;
    let mut positional = Vec::new();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == "--drop" {
            let n = it.next().ok_or("--drop needs a number")?;
            drop = n.parse::<u32>().map_err(|e| format!("--drop: {e}"))?;
        } else if a == "--per-second" {
            per_second = true;
        } else {
            positional.push(a.clone());
        }
    }
    let args = positional;
    let cmd = args.first().map_or("connect", String::as_str);
    let peer = args
        .get(1)
        .map(|a| a.parse::<Ipv4Addr>())
        .transpose()
        .map_err(|e| format!("camera-ip: {e}"))?;
    let seconds = args
        .get(2)
        .map(|a| a.parse::<u64>())
        .transpose()
        .map_err(|e| format!("seconds: {e}"))?;
    if drop > 0 {
        println!("dropping every {drop}th inbound media or parity packet");
    }
    let link = connect(peer, drop)?;
    let result = match cmd {
        "connect" => devinfo(&link),
        "watch" => devinfo(&link).and_then(|()| watch(&link, seconds.unwrap_or(60))),
        "stream" => devinfo(&link).and_then(|()| stream(&link, seconds.unwrap_or(30), per_second)),
        "restart" => devinfo(&link).and_then(|()| restart(&link, seconds.unwrap_or(25))),
        other => Err(format!("unknown command {other}")),
    };
    let t0 = Instant::now();
    link.close();
    println!(
        "closed in {} ms ({:?})",
        t0.elapsed().as_millis(),
        link.closed().unwrap_or(LinkError::Timeout)
    );
    result
}

fn main() -> ExitCode {
    let level = match std::env::var("VTECH_LOG").as_deref() {
        Ok("debug") => log::LevelFilter::Debug,
        Ok("trace") => log::LevelFilter::Trace,
        _ => log::LevelFilter::Info,
    };
    if log::set_logger(Box::leak(Box::new(StderrLog(level, Instant::now())))).is_ok() {
        log::set_max_level(level);
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("FAIL {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn sps_sizes() {
        for (sps, size) in [
            (
                "6742c028d900780227e5c044000003000400000300083c60c920",
                (1920, 1080),
            ),
            (
                "67640028acd940780227e5c044000003000400000300083c60c658",
                (1920, 1080),
            ),
            ("6742c01fd9005005bb011000000300100000030020f1832480", (1280, 720)),
            (
                "6764001facd9405005bb011000000300100000030020f1831960",
                (1280, 720),
            ),
        ] {
            assert_eq!(sps_size(&unhex(sps)), Some(size), "{sps}");
        }
        assert_eq!(sps_size(&[0x67, 0x64]), None);
    }

    #[test]
    fn signed_exp_golomb() {
        // ue codes 0..=4 are 1, 010, 011, 00100, 00101: se 0, 1, -1, 2, -2.
        let mut b = Bits {
            data: vec![0b1010_0110, 0b0100_0010, 0b1000_0000],
            pos: 0,
        };
        let got: Vec<_> = (0..5).map(|_| b.se().unwrap()).collect();
        assert_eq!(got, [0, 1, -1, 2, -2]);
    }

    #[test]
    fn nal_units_split() {
        let au = [0, 0, 0, 1, 0x67, 1, 2, 0, 0, 1, 0x68, 3, 0, 0, 0, 1, 0x65, 4, 0];
        let units = nal_units(&au);
        assert_eq!(units, [&[0x67, 1, 2][..], &[0x68, 3], &[0x65, 4]]);
    }
}
