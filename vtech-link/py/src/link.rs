//! `Link` and the objects it hands out, over the core's handles. Every call that waits releases the GIL.

use std::sync::{Arc, Mutex, PoisonError, Weak};
use std::time::{Duration, Instant};

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use vtech_link_core::av::StreamKind;
use vtech_link_core::runtime::{Exchange as CoreExchange, LinkStats};
use vtech_link_core::{Link as CoreLink, LinkConfig, MediaReceiver as CoreMedia, TalkBack as CoreTalk};

use crate::error::{describe, detached, err, invalid, secs};

/// How long `stats()` and `info()`-style calls wait for the reactor.
const STATS_WAIT: Duration = Duration::from_secs(1);

/// The live links, for `links()`.
static LINKS: Mutex<Vec<Weak<Entry>>> = Mutex::new(Vec::new());

struct Entry {
    link: CoreLink,
    /// `time.monotonic()` when the link came up.
    up_since: f64,
}

fn monotonic(py: Python<'_>) -> PyResult<f64> {
    py.import("time")?.getattr("monotonic")?.call0()?.extract()
}

/// Build a dict from `key => value` pairs.
macro_rules! dict {
    ($py:expr, { $($k:literal => $v:expr),* $(,)? }) => {{
        let d = PyDict::new($py);
        $( d.set_item($k, $v)?; )*
        d
    }};
}

#[allow(clippy::too_many_arguments)]
pub fn connect(
    py: Python<'_>,
    uid: &str,
    authkey: &str,
    password: &str,
    account: &str,
    connect_timeout: f64,
    av_timeout: f64,
    search_port: u16,
    peer_hint: Option<&str>,
    interface: Option<String>,
) -> PyResult<Link> {
    let mut config = LinkConfig::new(uid, authkey, password);
    config.account = account.to_string();
    config.connect_timeout = secs(py, Some(connect_timeout))?;
    config.av_timeout = secs(py, Some(av_timeout))?;
    config.search_port = search_port;
    config.peer_hint = peer_hint
        .map(str::parse)
        .transpose()
        .map_err(|_| invalid(py, "peer_hint must be an IPv4 address"))?;
    config.interface = interface;
    let link = detached(py, move || CoreLink::connect(config))?;
    let entry = Arc::new(Entry {
        link,
        up_since: monotonic(py)?,
    });
    let mut live = LINKS.lock().unwrap_or_else(PoisonError::into_inner);
    live.retain(|w| w.strong_count() > 0);
    live.push(Arc::downgrade(&entry));
    Ok(Link { entry })
}

/// `info()` of every live link.
pub fn links(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    let live: Vec<Arc<Entry>> = LINKS
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .iter()
        .filter_map(Weak::upgrade)
        .collect();
    live.iter().map(|e| info(py, e)).collect()
}

fn info<'py>(py: Python<'py>, entry: &Entry) -> PyResult<Bound<'py, PyDict>> {
    let close_reason = entry.link.closed().map(|e| {
        let (kind, detail, _) = describe(&e);
        if detail.is_empty() {
            kind.to_string()
        } else {
            detail
        }
    });
    let d = dict!(py, {
        "mode" => "lan",
        "up_since" => entry.up_since,
        "close_reason" => close_reason,
    });
    if let Some(i) = entry.link.info() {
        d.set_item("peer", i.peer.to_string())?;
        d.set_item("local_port", i.local_port)?;
        d.set_item("proto_version", i.proto_version)?;
        d.set_item("iotc_version", i.iotc_version)?;
        d.set_item("dtls_suite", i.dtls_suite)?;
        d.set_item("partial_encryption", i.partial_encryption)?;
    }
    Ok(d)
}

fn link_stats(py: Python<'_>, link: &CoreLink) -> PyResult<LinkStats> {
    detached(py, || link.stats(STATS_WAIT))
}

fn media_stats<'py>(py: Python<'py>, s: &LinkStats) -> PyResult<Bound<'py, PyDict>> {
    let m = s.av.map(|av| av.media).unwrap_or_default();
    let stream = |c: vtech_link_core::av::media_rx::StreamCounters| -> PyResult<Bound<'py, PyDict>> {
        Ok(dict!(py, {
            "frames" => c.frames,
            "bytes" => c.bytes,
            "given_up" => c.given_up,
            "late" => c.late,
        }))
    };
    Ok(dict!(py, {
        "video" => stream(m.video)?,
        "audio" => stream(m.audio)?,
        "packets_requested" => m.packets_requested,
        "packets_recovered" => m.packets_recovered,
        "packets_duplicate" => m.packets_duplicate,
        "packets_stale" => m.packets_stale,
        "fragments_dropped" => m.fragments_dropped,
        "gaps_skipped" => m.gaps_skipped,
        "fragments_behind" => m.fragments_behind,
        "packets_dropped_test" => m.packets_dropped_test,
        "video_overflow" => s.video_overflow,
        "audio_overflow" => s.audio_overflow,
        "decrypt_failed_video" => s.decrypt_failed_video,
        "decrypt_failed_audio" => s.decrypt_failed_audio,
    }))
}

fn talk_stats<'py>(py: Python<'py>, s: &LinkStats) -> PyResult<Option<Bound<'py, PyDict>>> {
    s.talk
        .map(|t| {
            Ok(dict!(py, {
                "frames_sent" => t.frames_sent,
                "bytes_sent" => t.bytes_sent,
                "frames_dropped_queue" => t.frames_dropped_queue,
                "dtls_refused" => t.dtls_refused,
                "logins" => t.logins,
                "packets_ignored" => t.packets_ignored,
            }))
        })
        .transpose()
}

/// One LAN-direct connection to a camera.
#[pyclass(frozen, module = "_vtech_link")]
pub struct Link {
    entry: Arc<Entry>,
}

#[pymethods]
impl Link {
    /// Send an IOCtrl; returns once the camera has acknowledged it.
    #[pyo3(signature = (r#type, payload = b"".as_slice(), timeout = 5.0))]
    fn send(&self, py: Python<'_>, r#type: u32, payload: &[u8], timeout: f64) -> PyResult<()> {
        let timeout = secs(py, Some(timeout))?;
        let payload = payload.to_vec();
        detached(py, || self.entry.link.send(r#type, &payload, timeout))
    }

    /// Send an IOCtrl and return the payload of the reply of type `reply`.
    #[pyo3(signature = (r#type, payload, reply, timeout = 5.0))]
    fn request<'py>(
        &self,
        py: Python<'py>,
        r#type: u32,
        payload: &[u8],
        reply: u32,
        timeout: f64,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let timeout = secs(py, Some(timeout))?;
        let payload = payload.to_vec();
        let body = detached(py, || self.entry.link.request(r#type, &payload, reply, timeout))?;
        Ok(PyBytes::new(py, &body))
    }

    /// Send an IOCtrl and collect every reply of type `reply` until closed or `timeout`.
    #[pyo3(signature = (r#type, payload, reply, timeout = 15.0))]
    fn exchange(
        &self,
        py: Python<'_>,
        r#type: u32,
        payload: &[u8],
        reply: u32,
        timeout: f64,
    ) -> PyResult<Exchange> {
        let timeout = secs(py, Some(timeout))?;
        let payload = payload.to_vec();
        let inner = detached(py, || self.entry.link.exchange(r#type, &payload, reply, timeout))?;
        Ok(Exchange { inner })
    }

    /// The next IOCtrl no request claimed, as `(type, payload)`; `None` on timeout.
    fn next_push<'py>(&self, py: Python<'py>, timeout: f64) -> PyResult<Option<(u32, Bound<'py, PyBytes>)>> {
        let timeout = secs(py, Some(timeout))?;
        let push = detached(py, || self.entry.link.next_push(timeout))?;
        Ok(push.map(|(t, body)| (t, PyBytes::new(py, &body))))
    }

    /// The media receiver. Nothing is queued until `start`.
    fn media(&self) -> MediaReceiver {
        MediaReceiver {
            link: self.entry.link.clone(),
            rx: Mutex::new(self.entry.link.media()),
        }
    }

    /// Open the talk-back channel. Announce `channel` to the camera, then `accept`.
    #[pyo3(signature = (timeout = 10.0))]
    fn talkback(&self, py: Python<'_>, timeout: f64) -> PyResult<TalkBack> {
        let timeout = secs(py, Some(timeout))?;
        let inner = detached(py, || self.entry.link.talkback(timeout))?;
        Ok(TalkBack {
            link: self.entry.link.clone(),
            inner,
        })
    }

    fn info<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        info(py, &self.entry)
    }

    /// Transport counters, RTT and the ACK latency histogram.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = link_stats(py, &self.entry.link)?;
        let iotc = dict!(py, {
            "bad_size" => s.iotc.bad_size,
            "bad_magic" => s.iotc.bad_magic,
            "malformed" => s.iotc.malformed,
            "foreign" => s.iotc.foreign,
            "closed_channel" => s.iotc.closed_channel,
            "ignored" => s.iotc.ignored,
        });
        let d = dict!(py, {
            "iotc" => iotc,
            "dtls_rejected" => s.dtls.rejected,
            "datagrams_in" => s.datagrams_in,
            "datagrams_out" => s.datagrams_out,
            "send_errors" => s.send_errors,
            "refused_sends" => s.refused_sends,
            "pushes_dropped" => s.pushes_dropped,
            "media" => media_stats(py, &s)?,
            "talk" => talk_stats(py, &s)?,
        });
        if let Some(av) = s.av {
            let c = av.control;
            d.set_item(
                "control",
                dict!(py, {
                    "frames_sent" => c.frames_sent,
                    "retransmits" => c.retransmits,
                    "ack_latency" => c.ack_latency.to_vec(),
                    "messages_in" => c.messages_in,
                    "multi_fragment_in" => c.multi_fragment_in,
                    "fragments_dropped" => c.fragments_dropped,
                    "fragments_duplicate" => c.fragments_duplicate,
                    "fragments_stale" => c.fragments_stale,
                }),
            )?;
            d.set_item(
                "ack_latency_buckets_ms",
                vtech_link_core::av::control::LATENCY_BUCKETS_MS.to_vec(),
            )?;
            d.set_item("packets_ignored", av.packets_ignored)?;
            d.set_item("resets", av.resets)?;
            d.set_item("reset_timeouts", av.reset_timeouts)?;
            d.set_item("rtt_ms", av.rtt_ms)?;
        }
        Ok(d)
    }

    /// Close the link. Idempotent; safe from any thread.
    fn close(&self, py: Python<'_>) {
        py.detach(|| self.entry.link.close());
    }

    /// Why the link ended, once it has.
    #[getter]
    fn closed(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.entry
            .link
            .closed()
            .map(|e| err(py, &e).into_value(py).into_any())
    }
}

/// The replies to one `exchange`.
#[pyclass(frozen, module = "_vtech_link")]
pub struct Exchange {
    inner: CoreExchange,
}

#[pymethods]
impl Exchange {
    /// The next reply; `None` on timeout or once the exchange has ended.
    fn next<'py>(&self, py: Python<'py>, timeout: f64) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let timeout = secs(py, Some(timeout))?;
        let page = detached(py, || self.inner.next(timeout))?;
        Ok(page.map(|p| PyBytes::new(py, &p)))
    }

    /// Free the reply type for the next request.
    fn close(&self) {
        self.inner.close();
    }
}

/// A decrypted media frame.
#[pyclass(frozen, get_all, module = "_vtech_link")]
pub struct Frame {
    /// `"video"` or `"audio"`.
    kind: &'static str,
    /// Video kinds 5 and 6.
    key: bool,
    number: u32,
    /// The per-stream frame count.
    seq: u32,
    /// `time.monotonic()` when its first fragment arrived.
    arrived: f64,
    /// Frames of this stream lost just before this one.
    lost_before: u32,
    /// An Annex-B access unit, or A-law.
    data: Py<PyBytes>,
}

#[pymethods]
impl Frame {
    fn __repr__(&self) -> String {
        format!(
            "Frame({} #{}{}, {} bytes, lost_before={})",
            self.kind,
            self.number,
            if self.key { " key" } else { "" },
            Python::attach(|py| self.data.bind(py).as_bytes().len()),
            self.lost_before
        )
    }
}

/// The live view's frames.
#[pyclass(frozen, module = "_vtech_link")]
pub struct MediaReceiver {
    link: CoreLink,
    rx: Mutex<CoreMedia>,
}

#[pymethods]
impl MediaReceiver {
    /// Run the live-view key exchange if needed, and start queueing frames.
    #[pyo3(signature = (timeout = 5.0))]
    fn start(&self, py: Python<'_>, timeout: f64) -> PyResult<()> {
        let timeout = secs(py, Some(timeout))?;
        detached(py, || self.lock().start(timeout))
    }

    /// Drop queued media and restart the packet numbering with the camera.
    #[pyo3(signature = (timeout = 2.0))]
    fn reset(&self, py: Python<'_>, timeout: f64) -> PyResult<()> {
        let timeout = secs(py, Some(timeout))?;
        detached(py, || self.lock().reset(timeout))
    }

    /// The next frame, decrypted and in order; `None` on timeout.
    fn next(&self, py: Python<'_>, timeout: f64) -> PyResult<Option<Frame>> {
        let timeout = secs(py, Some(timeout))?;
        let Some(f) = detached(py, || self.lock().next(timeout))? else {
            return Ok(None);
        };
        // Python's monotonic clock and `Instant` are both CLOCK_MONOTONIC; convert by the age.
        let now = Instant::now();
        let arrived = monotonic(py)? - now.saturating_duration_since(f.arrived).as_secs_f64();
        Ok(Some(Frame {
            kind: match f.stream {
                StreamKind::Video => "video",
                StreamKind::Audio => "audio",
            },
            key: f.key,
            number: f.number,
            seq: f.seq,
            arrived,
            lost_before: f.lost_before,
            data: PyBytes::new(py, &f.data).unbind(),
        }))
    }

    /// The receive counters.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        media_stats(py, &link_stats(py, &self.link)?)
    }

    /// Stop queueing frames and drop what is queued.
    fn close(&self, py: Python<'_>) {
        py.detach(|| self.lock().close());
    }
}

impl MediaReceiver {
    fn lock(&self) -> std::sync::MutexGuard<'_, CoreMedia> {
        self.rx.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// The talk-back channel.
#[pyclass(frozen, module = "_vtech_link")]
pub struct TalkBack {
    link: CoreLink,
    inner: CoreTalk,
}

#[pymethods]
impl TalkBack {
    /// The channel to announce with `SPEAKERSTART` (`0x350`).
    #[getter]
    fn channel(&self) -> u8 {
        self.inner.channel
    }

    /// Wait for the camera to log in to the channel.
    #[pyo3(signature = (timeout = 10.0))]
    fn accept(&self, py: Python<'_>, timeout: f64) -> PyResult<()> {
        let timeout = secs(py, Some(timeout))?;
        detached(py, || self.inner.accept(timeout))
    }

    /// Queue one 160-byte A-law frame; blocks while the queue is full, up to `timeout` (`None`: a day).
    #[pyo3(signature = (alaw, timeout = None))]
    fn send(&self, py: Python<'_>, alaw: &[u8], timeout: Option<f64>) -> PyResult<()> {
        let timeout = secs(py, timeout)?;
        let alaw = alaw.to_vec();
        detached(py, || self.inner.send(&alaw, timeout))
    }

    /// Close the channel: after sending what is queued (`drain`), or at once.
    #[pyo3(signature = (drain = true, timeout = 2.0))]
    fn close(&self, py: Python<'_>, drain: bool, timeout: f64) -> PyResult<()> {
        let timeout = secs(py, Some(timeout))?;
        detached(py, || self.inner.close(drain, timeout))
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        talk_stats(py, &link_stats(py, &self.link)?)
    }
}
