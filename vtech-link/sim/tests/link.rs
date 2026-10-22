//! End-to-end tests: the real `Link` against the simulated camera over loopback UDP. They follow the T2
//! scenarios in `docs/vtech-link-design/85-testing.md` (S1–S12), shortened to wall-clock seconds.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]

use std::net::Ipv4Addr;
use std::sync::Arc;
use std::time::{Duration, Instant};

use vtech_link_core::av::media_rx::StreamKind;
use vtech_link_core::error::{AvAuthStage, CloseReason, IotcReject};
use vtech_link_core::{Frame, Link, LinkConfig, LinkError, MediaReceiver, Timing};
use vtech_link_sim::{
    Camera, CameraConfig, FrameTag, OP_DEVINFO, OP_DEVINFO_REPLY, OP_GET_PARAMS, OP_GET_PARAMS_REPLY,
    OP_SPEAKER_START, OP_SPEAKER_STOP, OP_START, OP_STOP, WrongPassword, default_replies, devinfo,
    drop_every, parse_frame,
};

const UID: &str = "SIMCAM0123456789ABCD";
const AUTHKEY: &str = "k3yAuth8";
const PASSWORD: &str = "simpass";
const SEC: Duration = Duration::from_secs(1);
const MS: Duration = Duration::from_millis(1);
const OP_PUSH: u32 = 0x1fff;

fn camera() -> CameraConfig {
    CameraConfig::new(UID, AUTHKEY, PASSWORD)
}

/// A link config aimed at the sim: unicast search to 127.0.0.1 only. Naming `lo` leaves no broadcast
/// target (loopback is skipped), so nothing leaves the host.
fn link_config(cam: &Camera) -> LinkConfig {
    LinkConfig {
        search_port: cam.port(),
        peer_hint: Some(Ipv4Addr::LOCALHOST),
        interface: Some("lo".to_string()),
        connect_timeout: 3 * SEC,
        av_timeout: 5 * SEC,
        ..LinkConfig::new(UID, AUTHKEY, PASSWORD)
    }
}

fn connect(cam: &Camera) -> Link {
    Link::connect(link_config(cam)).unwrap()
}

/// The per-session prep: the camera streams only once `DEVINFO` has been asked for.
fn prep(link: &Link) {
    let reply = link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap();
    assert_eq!(reply, devinfo());
}

fn connect_err(config: LinkConfig) -> (LinkError, Duration) {
    let t0 = Instant::now();
    let err = Link::connect(config).err().expect("connect should fail");
    (err, t0.elapsed())
}

/// Echo every request `typ` in the range as reply `typ + 1`, on top of the default script.
fn echo_camera() -> CameraConfig {
    CameraConfig {
        responder: Arc::new(|typ, payload| match typ {
            0x5000..=0x50ff => vec![(typ + 1, payload.to_vec())],
            _ => default_replies(typ, payload),
        }),
        ..camera()
    }
}

// ---- connect, requests, close ----

#[test]
fn s1_connect_requests_close() {
    let cam = Camera::start(echo_camera());
    let link = connect(&cam);
    let info = link.info().unwrap();
    assert_eq!(info.peer, cam.addr());
    assert_eq!(info.proto_version, 0x1b);
    assert_eq!(info.dtls_suite, Some(0xccac));
    assert!(info.partial_encryption);
    assert_eq!(
        link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
        devinfo()
    );
    assert_eq!(
        link.request(OP_GET_PARAMS, &[0; 4], OP_GET_PARAMS_REPLY, SEC)
            .unwrap()
            .len(),
        64
    );
    let t0 = Instant::now();
    for i in 0u32..100 {
        let body = i.to_le_bytes();
        assert_eq!(link.request(0x5010, &body, 0x5011, SEC).unwrap(), body);
    }
    eprintln!("100 sequential requests in {:?}", t0.elapsed());
    link.close();
    assert_eq!(link.closed(), Some(LinkError::Closed(CloseReason::Local)));
    assert!(cam.wait_for(SEC, |s| s.close_datagrams == 3));
    // Closing again sends nothing more.
    link.close();
    std::thread::sleep(50 * MS);
    assert_eq!(cam.stats().close_datagrams, 3);
    assert_eq!(cam.stats().sessions, 1);
    assert_eq!(
        link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC),
        Err(LinkError::Closed(CloseReason::Local))
    );
}

#[test]
fn send_completes_on_ack() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    let t0 = Instant::now();
    link.send(0x0999, b"no reply needed", SEC).unwrap();
    assert!(t0.elapsed() < 500 * MS);
    let got = cam.stats().ioctrls;
    assert_eq!(got.last(), Some(&(0x0999, b"no reply needed".to_vec())));
    // A request the camera never answers times out on its own deadline.
    let t0 = Instant::now();
    assert_eq!(
        link.request(0x0998, &[], 0x0997, 300 * MS),
        Err(LinkError::Timeout)
    );
    assert!(t0.elapsed() >= 300 * MS && t0.elapsed() < SEC);
    link.close();
}

// ---- connect failures (S2, S3) ----

#[test]
fn s2_iotc_rejections() {
    let cases = [
        (5, None, IotcReject::AuthKey),
        (3, None, IotcReject::MaxSessions),
        (4, None, IotcReject::Port),
        (6, None, IotcReject::NoKeyAuth),
        (9, None, IotcReject::Other(9)),
        (1, Some(0x16), IotcReject::NoKeyAuth),
    ];
    for (result, version, want) in cases {
        let mut config = camera();
        if result == 5 {
            config.authkey = "otherKey".to_string();
        } else {
            config.iotc_result = result;
        }
        if let Some(v) = version {
            config.proto_version = v;
        }
        let cam = Camera::start(config);
        let (err, took) = connect_err(link_config(&cam));
        assert_eq!(err, LinkError::IotcRejected(want), "result {result}");
        assert!(took < 500 * MS, "result {result}: {took:?}");
        // The camera opens a session only for an accepted result; the client then refuses the version.
        let want = u64::from(result == 1);
        assert!(cam.wait_for(SEC, |s| s.sessions == want), "result {result}");
    }
}

#[test]
fn s2_no_free_session() {
    let cam = Camera::start(CameraConfig {
        max_sessions: 1,
        ..camera()
    });
    let first = connect(&cam);
    let (err, _) = connect_err(link_config(&cam));
    assert_eq!(err, LinkError::IotcRejected(IotcReject::MaxSessions));
    first.close();
    // A closed session frees its slot.
    assert!(cam.wait_for(SEC, |s| s.close_datagrams >= 1));
    connect(&cam).close();
}

#[test]
fn s2_unreachable() {
    let cam = Camera::start(CameraConfig {
        answer_search: false,
        ..camera()
    });
    let config = LinkConfig {
        connect_timeout: SEC,
        ..link_config(&cam)
    };
    let (err, took) = connect_err(config);
    assert_eq!(err, LinkError::Unreachable);
    assert!(took >= SEC && took < 1500 * MS, "{took:?}");
    // The search went out every 100 ms or so.
    let probes = cam.stats().probes;
    assert!((8..=12).contains(&probes), "{probes} probes");
}

#[test]
fn s2_dtls_unanswered() {
    let cam = Camera::start(CameraConfig {
        answer_dtls: false,
        ..camera()
    });
    let config = LinkConfig {
        av_timeout: SEC,
        ..link_config(&cam)
    };
    let (err, took) = connect_err(config);
    assert_eq!(err, LinkError::Unreachable);
    assert!(took >= SEC && took < 2 * SEC, "{took:?}");
}

#[test]
fn s2_login_rejected() {
    let cam = Camera::start(CameraConfig {
        login_state: 3,
        ..camera()
    });
    let (err, took) = connect_err(link_config(&cam));
    assert_eq!(err, LinkError::AvAuthFailed(AvAuthStage::LoginRejected(3)));
    assert!(took < SEC, "{took:?}");
}

#[test]
fn s3_wrong_password_silence() {
    let cam = Camera::start(camera());
    let base = link_config(&cam);
    let config = LinkConfig {
        password: "wrongpass".to_string(),
        timing: Timing {
            dtls_finished_wait: 300 * MS,
            ..base.timing.clone()
        },
        ..base
    };
    let (err, took) = connect_err(config);
    assert_eq!(err, LinkError::AvAuthFailed(AvAuthStage::Dtls(None)));
    // The client waits for the camera's Finished (3 s by default).
    assert!(took >= 300 * MS && took < 1500 * MS, "{took:?}");
    assert_eq!(cam.stats().dtls_refused, 1);
    assert_eq!(cam.stats().logins, 0);
}

#[test]
fn s3_wrong_password_alert() {
    let cam = Camera::start(CameraConfig {
        wrong_password: WrongPassword::Alert,
        ..camera()
    });
    let config = LinkConfig {
        password: "wrongpass".to_string(),
        ..link_config(&cam)
    };
    let (err, took) = connect_err(config);
    assert_eq!(err, LinkError::AvAuthFailed(AvAuthStage::Dtls(Some(51))));
    assert!(took < SEC, "{took:?}");
}

// ---- requests and pushes (S10, S11) ----

#[test]
fn pushes_arrive_in_order() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    assert_eq!(link.next_push(50 * MS), Ok(None));
    for i in 0u8..5 {
        cam.push(OP_PUSH, &[i; 20]);
    }
    for i in 0u8..5 {
        assert_eq!(link.next_push(SEC), Ok(Some((OP_PUSH, vec![i; 20]))));
    }
    // A push of a reply type nobody is waiting for is a push too.
    cam.push(OP_DEVINFO_REPLY, b"unasked");
    assert_eq!(
        link.next_push(SEC),
        Ok(Some((OP_DEVINFO_REPLY, b"unasked".to_vec())))
    );
    link.close();
}

#[test]
fn s11_exchange_collects_pages() {
    let pages: Vec<Vec<u8>> = (0u8..4).map(|i| vec![i; 100 + usize::from(i)]).collect();
    let script = pages.clone();
    let cam = Camera::start(CameraConfig {
        responder: Arc::new(move |typ, payload| match typ {
            0x788 => script.iter().map(|p| (0x789, p.clone())).collect(),
            _ => default_replies(typ, payload),
        }),
        ..camera()
    });
    let link = connect(&cam);
    let ex = link.exchange(0x788, &[0; 8], 0x789, 5 * SEC).unwrap();
    for page in &pages {
        assert_eq!(ex.next(SEC).unwrap().as_ref(), Some(page));
    }
    assert_eq!(ex.next(200 * MS), Ok(None));
    ex.close();
    // With the exchange closed, the reply type is free again: a late page is a push.
    cam.push(0x789, b"late");
    assert_eq!(link.next_push(SEC), Ok(Some((0x789, b"late".to_vec()))));
    link.close();
}

#[test]
fn s10_concurrent_requests() {
    let cam = Camera::start(echo_camera());
    let link = connect(&cam);
    let t0 = Instant::now();
    let threads: Vec<_> = (0u32..8)
        .map(|t| {
            let link = link.clone();
            std::thread::spawn(move || {
                // Two threads share each reply type, so they queue behind each other.
                let typ = 0x5000 + 2 * (t % 4);
                for n in 0u32..50 {
                    let mut body = t.to_le_bytes().to_vec();
                    body.extend_from_slice(&n.to_le_bytes());
                    let reply = link.request(typ, &body, typ + 1, 10 * SEC).unwrap();
                    assert_eq!(reply, body, "thread {t} request {n}");
                }
            })
        })
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    eprintln!("8 x 50 concurrent requests in {:?}", t0.elapsed());
    assert_eq!(cam.stats().ioctrls.len(), 400);
    let stats = link.stats(SEC).unwrap();
    let control = stats.av.unwrap().control;
    assert_eq!(control.frames_sent, 400);
    assert_eq!(control.messages_in, 400);
    link.close();
}

// ---- media (S4, S5) ----

/// Collect frames until `video` video frames have arrived, or `timeout`.
fn collect(media: &mut MediaReceiver, video: usize, timeout: Duration) -> (Vec<Frame>, Vec<Frame>) {
    let deadline = Instant::now() + timeout;
    let (mut v, mut a) = (Vec::new(), Vec::new());
    while v.len() < video {
        let left = deadline.saturating_duration_since(Instant::now());
        let Some(f) = media.next(left).unwrap() else { break };
        match f.stream {
            StreamKind::Video => v.push(f),
            StreamKind::Audio => a.push(f),
        }
    }
    (v, a)
}

fn tag(f: &Frame) -> FrameTag {
    parse_frame(&f.data).expect("a synthetic frame, decrypted intact")
}

/// How a stream's frames came out: frames missing between the first and the last, and frames that
/// came after a later one.
#[derive(Debug, Default, PartialEq, Eq)]
struct Order {
    missing: u32,
    reordered: u32,
}

/// Check one stream: no frame twice, and each in-order frame's `lost_before` no more than the gap
/// before it.
fn check_order(frames: &[Frame], video: bool) -> Order {
    let mut order = Order::default();
    let mut seen = std::collections::BTreeSet::new();
    let mut prev: Option<u32> = None;
    for f in frames {
        let t = tag(f);
        assert_eq!(t.video, video);
        assert!(seen.insert(t.index), "frame {} twice", t.index);
        match prev {
            Some(p) if t.index < p => order.reordered += 1,
            Some(p) => {
                let gap = t.index - p - 1;
                assert!(
                    f.lost_before <= gap,
                    "lost_before {} over gap {gap}",
                    f.lost_before
                );
            }
            None => {}
        }
        prev = Some(prev.map_or(t.index, |p| p.max(t.index)));
    }
    if let (Some(first), Some(last)) = (seen.first(), seen.last()) {
        order.missing = last - first + 1 - u32::try_from(seen.len()).unwrap();
    }
    order
}

#[test]
fn s4_media_frames_in_order() {
    let cam = Camera::start(CameraConfig {
        media: vtech_link_sim::MediaSpec {
            video_fps: 25,
            video_bytes: 20_000,
            ..Default::default()
        },
        ..camera()
    });
    let link = connect(&cam);
    prep(&link);

    let mut media = link.media();
    media.start(SEC).unwrap();
    link.send(OP_START, &[0; 8], SEC).unwrap();
    let (video, audio) = collect(&mut media, 50, 5 * SEC);
    assert_eq!(video.len(), 50);
    assert!(audio.len() >= 40, "{} audio frames", audio.len());
    assert!(video[0].key, "the stream starts on a keyframe");
    assert_eq!(tag(&video[0]).index, 0);
    assert_eq!(check_order(&video, true), Order::default());
    assert_eq!(check_order(&audio, false), Order::default());
    assert!(video.iter().chain(&audio).all(|f| f.lost_before == 0));
    // Every 15th video frame is a keyframe.
    for f in &video {
        assert_eq!(f.key, tag(f).index % 15 == 0);
    }
    let stats = link.stats(SEC).unwrap();
    let m = stats.av.unwrap().media;
    assert_eq!(stats.decrypt_failed_video + stats.decrypt_failed_audio, 0);
    assert_eq!(m.video.given_up, 0);
    link.send(OP_STOP, &[0; 8], SEC).unwrap();
    media.close();
    link.close();
}

#[test]
fn s5_reset_cycles() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    prep(&link);

    let mut media = link.media();
    media.start(SEC).unwrap();
    for cycle in 1u32..=5 {
        media.reset(SEC).unwrap();
        link.send(OP_START, &[0; 8], SEC).unwrap();
        let (video, _) = collect(&mut media, 5, 3 * SEC);
        assert_eq!(video.len(), 5, "cycle {cycle}");
        // Nothing from before the reset, and the first frame after it is a keyframe.
        assert!(video.iter().all(|f| tag(f).generation == cycle), "cycle {cycle}");
        assert!(video[0].key, "cycle {cycle}");
        link.send(OP_STOP, &[0; 8], SEC).unwrap();
    }
    let stats = link.stats(SEC).unwrap().av.unwrap();
    assert_eq!(stats.resets, 5);
    assert_eq!(stats.reset_timeouts, 0);
    assert_eq!(cam.stats().resets, 5);
    link.close();
}

#[test]
fn no_media_before_devinfo() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    let mut media = link.media();
    media.start(SEC).unwrap();
    // START is acknowledged, but the camera sends nothing until DEVINFO (seen live 2026-10-22).
    link.send(OP_START, &[0; 8], SEC).unwrap();
    let (video, audio) = collect(&mut media, 1, 500 * MS);
    assert!(video.is_empty() && audio.is_empty());
    assert_eq!(cam.stats().video_frames_sent, 0);
    prep(&link);
    link.send(OP_START, &[0; 8], SEC).unwrap();
    let (video, _) = collect(&mut media, 3, 2 * SEC);
    assert_eq!(video.len(), 3);
    link.close();
}

// ---- loss ----

#[test]
fn loss_both_ways() {
    let cam = Camera::start(CameraConfig {
        loss_in: Some(drop_every(20)),
        loss_out: Some(drop_every(20)),
        responder: echo_camera().responder,
        media: vtech_link_sim::MediaSpec {
            video_fps: 15,
            video_bytes: 12_000,
            ..Default::default()
        },
        ..camera()
    });
    let link = connect(&cam);
    for i in 0u32..30 {
        let body = i.to_le_bytes();
        assert_eq!(link.request(0x5020, &body, 0x5021, 2 * SEC).unwrap(), body);
    }
    prep(&link);

    let mut media = link.media();
    media.start(2 * SEC).unwrap();
    link.send(OP_START, &[0; 8], 2 * SEC).unwrap();
    let (video, audio) = collect(&mut media, 45, 6 * SEC);
    assert_eq!(video.len(), 45);
    let video_order = check_order(&video, true);
    let audio_order = check_order(&audio, false);
    let stats = link.stats(SEC).unwrap();
    let m = stats.av.unwrap().media;
    let sim = cam.stats();
    eprintln!(
        "loss: video {video_order:?}, audio {audio_order:?}; NAKed {}, parity-rebuilt {}, resent {}, dropped in {} out {}",
        m.packets_requested, m.packets_recovered, sim.resent, sim.dropped_in, sim.dropped_out
    );
    assert!(sim.dropped_in > 0 && sim.dropped_out > 0);
    assert!(m.packets_recovered > 0, "parity rebuilt nothing");
    assert!(sim.resent > 0, "nothing was NAKed and resent");
    // Audio frames are one packet each, so a lost packet is a whole frame lost: the frames after it
    // wait for its resend (by the per-stream count) instead of overtaking it.
    assert_eq!(video_order, Order::default());
    assert_eq!(audio_order, Order::default());
    link.close();
}

#[test]
fn s12_dtls_flight_loss() {
    // In 0: our ClientHello; in 1: our final flight; out 0: the camera's first flight; out 1: its
    // Finished. Each is lost once and the handshake completes by retransmission.
    for (drop_in, drop_out) in [
        (vec![0], vec![]),
        (vec![1], vec![]),
        (vec![], vec![0]),
        (vec![], vec![1]),
    ] {
        let cam = Camera::start(CameraConfig {
            dtls_drop_in: drop_in.clone(),
            dtls_drop_out: drop_out.clone(),
            ..camera()
        });
        let t0 = Instant::now();
        let link = Link::connect(link_config(&cam))
            .unwrap_or_else(|e| panic!("drop in {drop_in:?} out {drop_out:?}: {e}"));
        eprintln!(
            "drop in {drop_in:?} out {drop_out:?}: connected in {:?}",
            t0.elapsed()
        );
        assert_eq!(
            link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
            devinfo()
        );
        link.close();
    }
}

// ---- talk-back (S8) ----

#[test]
fn s8_talkback() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    let talk = link.talkback(2 * SEC).unwrap();
    assert!(talk.channel >= 1);
    // Announcing the channel is the caller's job.
    link.send(OP_SPEAKER_START, &u32::from(talk.channel).to_le_bytes(), SEC)
        .unwrap();
    talk.accept(3 * SEC).unwrap();
    assert!(cam.wait_for(SEC, |s| s.talk_logins == 1));
    let frames: Vec<Vec<u8>> = (0u8..50).map(|i| vec![i; 160]).collect();
    let t0 = Instant::now();
    for f in &frames {
        talk.send(f, 2 * SEC).unwrap();
    }
    talk.close(true, 3 * SEC).unwrap();
    let took = t0.elapsed();
    assert!(cam.wait_for(SEC, |s| s.talk_frames.len() == 50));
    // 50 frames paced at 20 ms.
    assert!(took >= 900 * MS && took < 1300 * MS, "{took:?}");
    let sim = cam.stats();
    assert_eq!(sim.talk_frames, frames);
    assert_eq!(sim.talk_undecryptable, 0);
    link.send(OP_SPEAKER_STOP, &[0; 4], SEC).unwrap();
    // The link is still good.
    assert_eq!(
        link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
        devinfo()
    );
    link.close();
}

#[test]
fn talkback_close_without_drain() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    let talk = link.talkback(2 * SEC).unwrap();
    link.send(OP_SPEAKER_START, &u32::from(talk.channel).to_le_bytes(), SEC)
        .unwrap();
    talk.accept(3 * SEC).unwrap();
    for i in 0u8..10 {
        talk.send(&[i; 160], SEC).unwrap();
    }
    talk.close(false, SEC).unwrap();
    std::thread::sleep(200 * MS);
    let got = cam.stats().talk_frames.len();
    assert!(got < 10, "{got} frames after an undrained close");
    // A second channel opens once the first is gone.
    let again = link.talkback(2 * SEC).unwrap();
    link.send(OP_SPEAKER_START, &u32::from(again.channel).to_le_bytes(), SEC)
        .unwrap();
    again.accept(3 * SEC).unwrap();
    link.close();
}

// ---- the end of a link (S6, S7) ----

#[test]
fn s7_camera_close() {
    let cam = Camera::start(camera());
    let link = connect(&cam);
    let waiting = {
        let link = link.clone();
        std::thread::spawn(move || link.request(0x0999, &[], 0x099a, 10 * SEC))
    };
    std::thread::sleep(200 * MS);
    let t0 = Instant::now();
    cam.close_sessions();
    let remote = LinkError::Closed(CloseReason::RemoteClose);
    assert_eq!(waiting.join().unwrap(), Err(remote.clone()));
    assert!(t0.elapsed() < 500 * MS);
    assert_eq!(link.closed(), Some(remote.clone()));
    assert_eq!(link.next_push(SEC), Err(remote.clone()));
    assert_eq!(link.send(0x0999, &[], SEC), Err(remote));
}

#[test]
fn s6_camera_silent() {
    // The default is 60 ticks of 1 s; 10 of 100 ms keeps the test short.
    let cam = Camera::start(camera());
    let base = link_config(&cam);
    let link = Link::connect(LinkConfig {
        timing: Timing {
            alive_interval: 100 * MS,
            silence_limit: 10,
            ..base.timing.clone()
        },
        ..base
    })
    .unwrap();
    cam.kill();
    let t0 = Instant::now();
    let err = link.next_push(10 * SEC).unwrap_err();
    assert_eq!(err, LinkError::Closed(CloseReason::RemoteTimeout));
    let took = t0.elapsed();
    assert!(took >= 1000 * MS && took < 1500 * MS, "{took:?}");
}

#[test]
fn talkback_listen_times_out() {
    let cam = Camera::start(camera());
    let base = link_config(&cam);
    let link = Link::connect(LinkConfig {
        timing: Timing {
            talk_listen: 300 * MS,
            ..base.timing.clone()
        },
        ..base
    })
    .unwrap();
    // The channel is never announced, so the camera never logs in.
    let talk = link.talkback(2 * SEC).unwrap();
    let t0 = Instant::now();
    let err = talk.accept(5 * SEC).unwrap_err();
    let took = t0.elapsed();
    assert!(took < 800 * MS, "{took:?}: {err:?}");
    eprintln!("listen timeout: {err:?} after {took:?}");
    // The link is still good, and a new channel can open.
    assert_eq!(
        link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
        devinfo()
    );
    let talk = link.talkback(2 * SEC).unwrap();
    link.send(OP_SPEAKER_START, &u32::from(talk.channel).to_le_bytes(), SEC)
        .unwrap();
    talk.accept(3 * SEC).unwrap();
    link.close();
}

// ---- several links (S9) ----

#[test]
fn two_links_to_one_camera() {
    let cam = Camera::start(camera());
    let (a, b) = std::thread::scope(|s| {
        let a = s.spawn(|| connect(&cam));
        let b = s.spawn(|| connect(&cam));
        (a.join().unwrap(), b.join().unwrap())
    });
    assert_ne!(a.info().unwrap().local_port, b.info().unwrap().local_port);
    assert_eq!(cam.stats().sessions, 2);
    for link in [&a, &b] {
        assert_eq!(
            link.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
            devinfo()
        );
    }
    cam.push(OP_PUSH, b"both");
    for link in [&a, &b] {
        assert_eq!(link.next_push(SEC), Ok(Some((OP_PUSH, b"both".to_vec()))));
    }
    a.close();
    // Closing one leaves the other up.
    assert_eq!(
        b.request(OP_DEVINFO, &[0; 4], OP_DEVINFO_REPLY, SEC).unwrap(),
        devinfo()
    );
    assert_eq!(b.closed(), None);
    b.close();
}

#[test]
fn s9_links_to_several_cameras() {
    let cams: Vec<Camera> = (0..6).map(|_| Camera::start(camera())).collect();
    let links: Vec<Link> = std::thread::scope(|s| {
        let handles: Vec<_> = cams.iter().map(|c| s.spawn(|| connect(c))).collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    for (i, (cam, link)) in cams.iter().zip(&links).enumerate() {
        assert_eq!(link.info().unwrap().peer, cam.addr(), "link {i}");
        cam.push(OP_PUSH, &[u8::try_from(i).unwrap(); 4]);
    }
    for (i, link) in links.iter().enumerate() {
        assert_eq!(
            link.next_push(SEC),
            Ok(Some((OP_PUSH, vec![u8::try_from(i).unwrap(); 4])))
        );
    }
    // One camera closing ends only its link.
    cams[2].close_sessions();
    std::thread::sleep(200 * MS);
    for (i, link) in links.iter().enumerate() {
        let want = (i == 2).then_some(LinkError::Closed(CloseReason::RemoteClose));
        assert_eq!(link.closed(), want, "link {i}");
    }
    for link in &links {
        link.close();
    }
}
