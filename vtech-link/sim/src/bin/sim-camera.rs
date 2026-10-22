//! A simulated camera on loopback, driven over stdin, for testing the Python module end to end.
//!
//!   sim-camera [--wrong-password-alert] [--refuse-authkey]
//!
//! Prints the camera's UDP port on the first line, then reads commands, one per line:
//!
//!   push TYPE HEX     send an unsolicited IOCtrl (TYPE in hex) to every client
//!   close             close every session from the camera's side
//!   kill              go silent
//!   stats             print `key=value` counters on one line
//!   (end of input)    stop
//!
//! Credentials are fixed: UID `SIMCAM0123456789ABCD`, authKey `k3yAuth8`, password `simpass`. Replies:
//! the default script (DEVINFO, GET_PARAMS); `0x5000..=0x50ff` echoed as type + 1; `0x788` answered
//! with four `0x789` pages, page `i` being `i` repeated `100 + i` times.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, Write};
use std::sync::Arc;

use vtech_link_sim::{Camera, CameraConfig, WrongPassword, default_replies};

fn hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(s.get(i..i + 2)?, 16).ok())
        .collect()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut config = CameraConfig {
        responder: Arc::new(|typ, payload| match typ {
            0x5000..=0x50ff => vec![(typ + 1, payload.to_vec())],
            0x788 => (0u8..4).map(|i| (0x789, vec![i; 100 + usize::from(i)])).collect(),
            _ => default_replies(typ, payload),
        }),
        ..CameraConfig::new("SIMCAM0123456789ABCD", "k3yAuth8", "simpass")
    };
    for a in &args {
        match a.as_str() {
            "--wrong-password-alert" => config.wrong_password = WrongPassword::Alert,
            "--refuse-authkey" => config.iotc_result = 5,
            other => {
                eprintln!("unknown option {other}");
                std::process::exit(2);
            }
        }
    }
    let cam = Camera::start(config);
    let mut out = std::io::stdout();
    writeln!(out, "{}", cam.port()).unwrap();
    out.flush().unwrap();
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap();
        let words: Vec<&str> = line.split_whitespace().collect();
        match words.as_slice() {
            ["push", typ, body @ ..] => {
                let typ = u32::from_str_radix(typ, 16).expect("push TYPE is hex");
                let body = hex(body.first().unwrap_or(&"")).expect("push HEX is hex");
                cam.push(typ, &body);
            }
            ["close"] => cam.close_sessions(),
            ["kill"] => cam.kill(),
            ["stats"] => {
                let s = cam.stats();
                writeln!(
                    out,
                    "sessions={} logins={} ioctrls={} close_datagrams={} talk_logins={} talk_frames={} \
                     talk_undecryptable={} resets={}",
                    s.sessions,
                    s.logins,
                    s.ioctrls.len(),
                    s.close_datagrams,
                    s.talk_logins,
                    s.talk_frames.len(),
                    s.talk_undecryptable,
                    s.resets
                )
                .unwrap();
                out.flush().unwrap();
            }
            [] => {}
            _ => eprintln!("unknown command: {line}"),
        }
    }
}
