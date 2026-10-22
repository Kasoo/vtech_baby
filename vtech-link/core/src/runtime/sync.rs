//! The two hand-offs between the reactor and caller threads: a one-shot `Completion`, and a bounded
//! `Queue`. Callers block on them with a timeout; the reactor never blocks on either.

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::error::LinkError;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    // A panic while holding one of these locks leaves plain data behind; carry on with it.
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// One result, completed once by the reactor and waited for by one caller. A caller that gives up
/// leaves it behind; the later `complete` finds no one waiting.
pub struct Completion<T> {
    slot: Mutex<Option<Result<T, LinkError>>>,
    cv: Condvar,
}

impl<T> Default for Completion<T> {
    fn default() -> Self {
        Self {
            slot: Mutex::new(None),
            cv: Condvar::new(),
        }
    }
}

impl<T> Completion<T> {
    /// Store the result if none is stored yet, and wake the waiter.
    pub fn complete(&self, result: Result<T, LinkError>) {
        let mut slot = lock(&self.slot);
        if slot.is_none() {
            *slot = Some(result);
            self.cv.notify_all();
        }
    }

    /// Wait up to `timeout` for the result; `Timeout` if it doesn't come.
    pub fn wait(&self, timeout: Duration) -> Result<T, LinkError> {
        let deadline = Instant::now() + timeout;
        let mut slot = lock(&self.slot);
        loop {
            if let Some(result) = slot.take() {
                return result;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(LinkError::Timeout);
            }
            slot = self
                .cv
                .wait_timeout(slot, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
    }
}

struct QState<T> {
    items: VecDeque<T>,
    /// `Some(Ok)` after a normal end, `Some(Err)` once the link has failed.
    closed: Option<Result<(), LinkError>>,
    dropped: u64,
}

/// A bounded FIFO between the reactor and a caller.
pub struct Queue<T> {
    state: Mutex<QState<T>>,
    cv: Condvar,
    cap: usize,
}

impl<T> Queue<T> {
    pub fn new(cap: usize) -> Self {
        Self {
            state: Mutex::new(QState {
                items: VecDeque::new(),
                closed: None,
                dropped: 0,
            }),
            cv: Condvar::new(),
            cap: cap.max(1),
        }
    }

    /// Add an item, dropping the oldest when full (the reactor's side). Ignored once closed.
    pub fn push_drop_oldest(&self, item: T) {
        let mut s = lock(&self.state);
        if s.closed.is_some() {
            return;
        }
        if s.items.len() >= self.cap {
            s.items.pop_front();
            s.dropped += 1;
        }
        s.items.push_back(item);
        self.cv.notify_all();
    }

    /// Add an item, waiting up to `timeout` for room (a caller's side: back-pressure).
    pub fn push_wait(&self, item: T, timeout: Duration) -> Result<(), LinkError> {
        let deadline = Instant::now() + timeout;
        let mut s = lock(&self.state);
        loop {
            match &s.closed {
                Some(Err(e)) => return Err(e.clone()),
                Some(Ok(())) => return Err(LinkError::Closed(crate::error::CloseReason::Local)),
                None => {}
            }
            if s.items.len() < self.cap {
                s.items.push_back(item);
                self.cv.notify_all();
                return Ok(());
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Err(LinkError::Timeout);
            }
            s = self
                .cv
                .wait_timeout(s, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
    }

    /// Take the oldest item without waiting (the reactor's side of the talk-back input).
    pub fn try_pop(&self) -> Option<T> {
        let mut s = lock(&self.state);
        let item = s.items.pop_front();
        if item.is_some() {
            self.cv.notify_all();
        }
        item
    }

    /// Wait up to `timeout` for an item. `Ok(None)` on timeout or after a normal end; the link's error
    /// once it has failed and the queue is empty.
    pub fn pop_wait(&self, timeout: Duration) -> Result<Option<T>, LinkError> {
        let deadline = Instant::now() + timeout;
        let mut s = lock(&self.state);
        loop {
            if let Some(item) = s.items.pop_front() {
                self.cv.notify_all();
                return Ok(Some(item));
            }
            match &s.closed {
                Some(Err(e)) => return Err(e.clone()),
                Some(Ok(())) => return Ok(None),
                None => {}
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Ok(None);
            }
            s = self
                .cv
                .wait_timeout(s, left)
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .0;
        }
    }

    /// End the queue: what is in it can still be taken, then `result`.
    pub fn close(&self, result: Result<(), LinkError>) {
        let mut s = lock(&self.state);
        if s.closed.is_none() {
            s.closed = Some(result);
        }
        self.cv.notify_all();
    }

    pub fn is_closed(&self) -> bool {
        lock(&self.state).closed.is_some()
    }

    pub fn clear(&self) {
        let mut s = lock(&self.state);
        s.items.clear();
        self.cv.notify_all();
    }

    pub fn len(&self) -> usize {
        lock(&self.state).items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn dropped(&self) -> u64 {
        lock(&self.state).dropped
    }
}
