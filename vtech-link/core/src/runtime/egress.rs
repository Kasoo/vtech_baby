//! The LAN-only rule: every datagram goes to the limited broadcast address or into a directly attached
//! IPv4 subnet, or not at all. Port of `tutk-host/global/egress.c`. The same interface list gives the
//! discovery broadcast targets.

use std::net::Ipv4Addr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, RwLock};
use std::time::{Duration, Instant};

/// How long the interface list is reused before it is read again (`egress.c:25`).
const REFRESH: Duration = Duration::from_secs(1);
const WARN_EVERY: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Interface {
    pub name: String,
    pub addr: Ipv4Addr,
    pub netmask: Ipv4Addr,
}

impl Interface {
    fn contains(&self, dst: Ipv4Addr) -> bool {
        let mask = u32::from(self.netmask);
        u32::from(dst) & mask == u32::from(self.addr) & mask
    }

    /// The subnet's directed broadcast address.
    pub fn broadcast(&self) -> Ipv4Addr {
        Ipv4Addr::from(u32::from(self.addr) | !u32::from(self.netmask))
    }
}

static CACHE: RwLock<Option<(Instant, Vec<Interface>)>> = RwLock::new(None);
static REFUSED_SENDS: AtomicU64 = AtomicU64::new(0);
static LAST_WARNING: Mutex<Option<Instant>> = Mutex::new(None);

fn read_interfaces() -> Vec<Interface> {
    let Ok(list) = if_addrs::get_if_addrs() else {
        return Vec::new();
    };
    list.into_iter()
        .filter_map(|i| match i.addr {
            if_addrs::IfAddr::V4(v4) => Some(Interface {
                name: i.name,
                addr: v4.ip,
                netmask: v4.netmask,
            }),
            if_addrs::IfAddr::V6(_) => None,
        })
        .collect()
}

/// The host's IPv4 interfaces, read at most once a second.
pub fn interfaces() -> Vec<Interface> {
    let now = Instant::now();
    if let Ok(cache) = CACHE.read() {
        if let Some((at, list)) = cache.as_ref() {
            if now.saturating_duration_since(*at) < REFRESH {
                return list.clone();
            }
        }
    }
    let list = read_interfaces();
    if let Ok(mut cache) = CACHE.write() {
        *cache = Some((now, list.clone()));
    }
    list
}

/// Whether `dst` is the limited broadcast address or inside an attached subnet.
pub fn allowed(dst: Ipv4Addr, interfaces: &[Interface]) -> bool {
    dst == Ipv4Addr::BROADCAST
        || interfaces
            .iter()
            .any(|i| !i.netmask.is_unspecified() && i.contains(dst))
}

/// Check a send; a refused one is counted and logged, at most once a minute.
pub fn check(dst: Ipv4Addr) -> bool {
    if allowed(dst, &interfaces()) {
        return true;
    }
    REFUSED_SENDS.fetch_add(1, Ordering::Relaxed);
    let now = Instant::now();
    if let Ok(mut last) = LAST_WARNING.lock() {
        if last.is_none_or(|t| now.saturating_duration_since(t) >= WARN_EVERY) {
            *last = Some(now);
            log::warn!("refused a send to {dst}: not on a directly attached subnet");
        }
    }
    false
}

/// Discovery targets: the directed broadcast of every up, non-loopback interface with a netmask,
/// optionally only the one named.
pub fn broadcast_targets(only: Option<&str>) -> Vec<Ipv4Addr> {
    let mut out: Vec<Ipv4Addr> = interfaces()
        .iter()
        .filter(|i| !i.addr.is_loopback() && !i.addr.is_unspecified() && !i.netmask.is_unspecified())
        .filter(|i| only.is_none_or(|name| i.name == name))
        .map(Interface::broadcast)
        .collect();
    out.dedup();
    out
}

/// `(refused_sends, refused_lookups)`. There is no name resolution anywhere, so lookups stay 0.
pub fn stats() -> (u64, u64) {
    (REFUSED_SENDS.load(Ordering::Relaxed), 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subnet_rule() {
        let ifs = [Interface {
            name: "lan".into(),
            addr: Ipv4Addr::new(192, 168, 1, 106),
            netmask: Ipv4Addr::new(255, 255, 255, 0),
        }];
        assert!(allowed(Ipv4Addr::new(192, 168, 1, 220), &ifs));
        assert!(allowed(Ipv4Addr::new(192, 168, 1, 255), &ifs));
        assert!(allowed(Ipv4Addr::BROADCAST, &ifs));
        assert!(!allowed(Ipv4Addr::new(192, 168, 2, 1), &ifs));
        assert!(!allowed(Ipv4Addr::new(8, 8, 8, 8), &ifs));
        assert_eq!(ifs[0].broadcast(), Ipv4Addr::new(192, 168, 1, 255));
    }
}
