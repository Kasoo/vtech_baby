//! Request/reply matching and the push queue. The camera's replies carry no request id, so one
//! request per reply type is active at a time and later ones wait their turn, as `session.py` does with
//! one lock per reply opcode. Design: `docs/vtech-link-design/60-application.md`.

use std::collections::{HashMap, VecDeque};
use std::time::Instant;

use crate::error::LinkError;

pub type RequestId = u64;

#[derive(Debug, PartialEq, Eq)]
pub enum RequestEvent {
    /// Put this IOCtrl on the control stream: the request has become active.
    Send {
        id: RequestId,
        typ: u32,
        payload: Vec<u8>,
    },
    /// A reply. A single request is finished by it; an exchange stays open for more.
    Reply { id: RequestId, payload: Vec<u8> },
    /// The request is finished: an exchange closed (`Ok`), or a timeout or the link's error.
    Done {
        id: RequestId,
        result: Result<(), LinkError>,
    },
}

/// An inbound IOCtrl no request claimed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Push {
    pub typ: u32,
    pub payload: Vec<u8>,
    pub arrived: Instant,
}

struct Waiter {
    id: RequestId,
    typ: u32,
    payload: Vec<u8>,
    /// Takes every reply until closed, rather than one.
    exchange: bool,
    deadline: Instant,
}

pub struct Requests {
    next_id: RequestId,
    /// Per reply type: the head is active, the rest wait.
    by_reply: HashMap<u32, VecDeque<Waiter>>,
    pushes: VecDeque<Push>,
    push_capacity: usize,
    pushes_dropped: u64,
    events: VecDeque<RequestEvent>,
}

impl Requests {
    pub fn new(push_capacity: usize) -> Self {
        Self {
            next_id: 1,
            by_reply: HashMap::new(),
            pushes: VecDeque::new(),
            push_capacity: push_capacity.max(1),
            pushes_dropped: 0,
            events: VecDeque::new(),
        }
    }

    /// Send `typ` with `payload` and wait for `reply`, until `deadline` (which covers waiting for a turn
    /// too). With `exchange`, every `reply` until `close` belongs to it.
    pub fn request(
        &mut self,
        typ: u32,
        payload: &[u8],
        reply: u32,
        deadline: Instant,
        exchange: bool,
    ) -> RequestId {
        let id = self.next_id;
        self.next_id += 1;
        let queue = self.by_reply.entry(reply).or_default();
        queue.push_back(Waiter {
            id,
            typ,
            payload: payload.to_vec(),
            exchange,
            deadline,
        });
        if queue.len() == 1 {
            self.activate(reply);
        }
        id
    }

    fn activate(&mut self, reply: u32) {
        if let Some(w) = self.by_reply.get(&reply).and_then(VecDeque::front) {
            self.events.push_back(RequestEvent::Send {
                id: w.id,
                typ: w.typ,
                payload: w.payload.clone(),
            });
        }
    }

    /// Finish the head waiter for `reply` and start the next.
    fn finish_head(&mut self, reply: u32) -> Option<Waiter> {
        let queue = self.by_reply.get_mut(&reply)?;
        let done = queue.pop_front();
        if queue.is_empty() {
            self.by_reply.remove(&reply);
        } else {
            self.activate(reply);
        }
        done
    }

    /// End an exchange (or abandon a request) early.
    pub fn close(&mut self, id: RequestId) {
        let Some((&reply, pos)) = self
            .by_reply
            .iter()
            .find_map(|(r, q)| q.iter().position(|w| w.id == id).map(|p| (r, p)))
        else {
            return;
        };
        if pos == 0 {
            self.finish_head(reply);
        } else if let Some(q) = self.by_reply.get_mut(&reply) {
            q.remove(pos);
        }
        self.events.push_back(RequestEvent::Done { id, result: Ok(()) });
    }

    /// An inbound user IOCtrl: the active waiter for its type takes it, or it becomes a push.
    pub fn on_control(&mut self, now: Instant, typ: u32, payload: Vec<u8>) {
        let Some(head) = self.by_reply.get(&typ).and_then(VecDeque::front) else {
            if self.pushes.len() >= self.push_capacity {
                self.pushes.pop_front();
                self.pushes_dropped += 1;
            }
            self.pushes.push_back(Push {
                typ,
                payload,
                arrived: now,
            });
            return;
        };
        let (id, exchange) = (head.id, head.exchange);
        self.events.push_back(RequestEvent::Reply { id, payload });
        if !exchange {
            self.finish_head(typ);
        }
    }

    pub fn poll_timeout(&self) -> Option<Instant> {
        self.by_reply.values().flatten().map(|w| w.deadline).min()
    }

    /// Time out every waiter past its deadline, active or queued. An exchange that reaches its
    /// deadline ends normally: it has collected what came.
    pub fn handle_timeout(&mut self, now: Instant) {
        let replies: Vec<u32> = self.by_reply.keys().copied().collect();
        for reply in replies {
            while let Some(pos) = self
                .by_reply
                .get(&reply)
                .and_then(|q| q.iter().position(|w| now >= w.deadline))
            {
                let w = if pos == 0 {
                    self.finish_head(reply)
                } else {
                    self.by_reply.get_mut(&reply).and_then(|q| q.remove(pos))
                };
                let Some(w) = w else { break };
                let result = if w.exchange {
                    Ok(())
                } else {
                    Err(LinkError::Timeout)
                };
                self.events.push_back(RequestEvent::Done { id: w.id, result });
            }
        }
    }

    /// Fail every request with the link's error.
    pub fn fail_all(&mut self, error: &LinkError) {
        for (_, q) in self.by_reply.drain() {
            for w in q {
                self.events.push_back(RequestEvent::Done {
                    id: w.id,
                    result: Err(error.clone()),
                });
            }
        }
    }

    pub fn poll_event(&mut self) -> Option<RequestEvent> {
        self.events.pop_front()
    }

    pub fn poll_push(&mut self) -> Option<Push> {
        self.pushes.pop_front()
    }

    pub fn pushes_dropped(&self) -> u64 {
        self.pushes_dropped
    }
}
