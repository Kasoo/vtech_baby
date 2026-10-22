//! The core's log records, kept in a bounded ring for Python to collect with `next_logs`. Rust threads
//! never take the GIL; a Python thread drains the ring (`docs/vtech-link-design/70-runtime.md`).

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

const CAPACITY: usize = 1024;

/// One record: Python `logging` level, target, message.
pub type Record = (i32, String, String);

struct Ring {
    records: VecDeque<Record>,
    dropped: u64,
}

static RING: Mutex<Ring> = Mutex::new(Ring {
    records: VecDeque::new(),
    dropped: 0,
});
static READY: Condvar = Condvar::new();

fn ring() -> MutexGuard<'static, Ring> {
    RING.lock().unwrap_or_else(PoisonError::into_inner)
}

struct RingLog;

static LOGGER: RingLog = RingLog;

impl log::Log for RingLog {
    fn enabled(&self, _: &log::Metadata<'_>) -> bool {
        true
    }

    fn log(&self, r: &log::Record<'_>) {
        let level = match r.level() {
            log::Level::Error => 40,
            log::Level::Warn => 30,
            log::Level::Info => 20,
            log::Level::Debug => 10,
            log::Level::Trace => 5,
        };
        let record = (level, r.target().to_string(), r.args().to_string());
        let mut ring = ring();
        if ring.records.len() >= CAPACITY {
            ring.records.pop_front();
            ring.dropped += 1;
        }
        ring.records.push_back(record);
        READY.notify_all();
    }

    fn flush(&self) {}
}

/// Install the ring as the process's logger, at INFO. Does nothing if a logger is already set.
pub fn install() {
    if log::set_logger(&LOGGER).is_ok() {
        log::set_max_level(log::LevelFilter::Info);
    }
}

/// Set the most verbose level recorded, from a Python `logging` level.
pub fn set_level(level: i32) {
    let filter = match level {
        40.. => log::LevelFilter::Error,
        30..=39 => log::LevelFilter::Warn,
        20..=29 => log::LevelFilter::Info,
        10..=19 => log::LevelFilter::Debug,
        _ => log::LevelFilter::Trace,
    };
    log::set_max_level(filter);
}

/// Every queued record, waiting up to `timeout` for the first.
pub fn take(timeout: Duration) -> Vec<Record> {
    let deadline = Instant::now() + timeout;
    let mut ring = ring();
    while ring.records.is_empty() {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Vec::new();
        }
        ring = READY
            .wait_timeout(ring, left)
            .unwrap_or_else(PoisonError::into_inner)
            .0;
    }
    ring.records.drain(..).collect()
}

pub fn dropped() -> u64 {
    ring().dropped
}
