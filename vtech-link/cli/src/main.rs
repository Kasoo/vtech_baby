//! `vtech-link`: read-only probes against the camera with no Python. Credentials come from the
//! environment (`VTECH_UID`, `VTECH_AUTHKEY`, `VTECH_PWD`), never the command line.
//!
//!   vtech-link connect [camera-ip]              connect, DEVINFO, close
//!   vtech-link watch   [camera-ip] [seconds]    connect, print pushes, GET_PARAMS every 60 s
//!   vtech-link stream  [camera-ip] [seconds]    live view: count, check and time the frames
//!
//! `VTECH_LOG=debug` for more. Nothing here changes a camera setting or makes light or sound.

use std::net::Ipv4Addr;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use vtech_link_core::av::StreamKind;
use vtech_link_core::{Link, LinkConfig, LinkError};

const DEVINFO: u32 = 0x330;
const DEVINFO_REPLY: u32 = 0x331;
const GET_PARAMS: u32 = 0x733;
const GET_PARAMS_REPLY: u32 = 0x734;
const START: u32 = 0x1ff;
const STOP: u32 = 0x2ff;
const AUDIO_START: u32 = 0x300;
const AUDIO_STOP: u32 = 0x301;
const VIDEO_FULLRATE: u32 = 0x719;
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

fn config(peer: Option<Ipv4Addr>) -> Result<LinkConfig, String> {
    let mut c = LinkConfig::new(&env("VTECH_UID")?, &env("VTECH_AUTHKEY")?, &env("VTECH_PWD")?);
    c.peer_hint = peer;
    Ok(c)
}

fn connect(peer: Option<Ipv4Addr>) -> Result<Link, String> {
    let t0 = Instant::now();
    let link = Link::connect(config(peer)?).map_err(|e| format!("connect: {e}"))?;
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

fn stream(link: &Link, seconds: u64) -> Result<(), String> {
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
    let (mut video, mut audio, mut keys, mut lost, mut bytes) = (0u64, 0u64, 0u64, 0u64, 0u64);
    let mut first: Option<Duration> = None;
    let mut first_key: Option<Duration> = None;
    // The per-stream count (`+0x10`) of the first and last frame, and how often it didn't step by one.
    let mut seqs: [Option<(u32, u32)>; 2] = [None; 2];
    let mut seq_jumps = [0u64; 2];
    while Instant::now() < end {
        match rx.next(Duration::from_millis(500)) {
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
    print_stats(link);
    Ok(())
}

fn print_stats(link: &Link) {
    match link.stats(Duration::from_secs(2)) {
        Ok(s) => println!("stats: {s:#?}"),
        Err(e) => println!("stats: {e}"),
    }
}

fn run(args: &[String]) -> Result<(), String> {
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
    let link = connect(peer)?;
    let result = match cmd {
        "connect" => devinfo(&link),
        "watch" => devinfo(&link).and_then(|()| watch(&link, seconds.unwrap_or(60))),
        "stream" => devinfo(&link).and_then(|()| stream(&link, seconds.unwrap_or(30))),
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
