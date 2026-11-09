// Hold-to-talk card for a VTech baby monitor's speaker.
//
//   type: custom:vtech-talk-card
//   entity: media_player.<camera>_speaker
//   name: Hold to talk          (optional)
//
// Hold the button (pointer, touch, or Space/Enter) and talk: the microphone streams to the camera as you
// speak, over Home Assistant's websocket (command `vtech_baby/talk`, then binary messages of 16-bit mono
// PCM at the browser's own sample rate). Release to stop; the button stays busy until the camera has
// played what was sent. The microphone is opened per press and closed on release.

const MAX_TALK_MS = 115000;          // the integration cuts a talk off at 120 s
const END_WAIT_MS = 30000;           // after release, give up waiting for the camera's "end"
const LABELS = {
  idle: "Hold to talk",
  opening: "Connecting…",
  talking: "Talking… release to stop",
  finishing: "Finishing…",
};

// Turns the microphone into 20 ms blocks of 16-bit PCM, posted to the page.
const WORKLET = `
class VtechCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 50);
    this.buf = new Int16Array(this.size);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        const s = Math.max(-1, Math.min(1, ch[i]));
        this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.size) {
          this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
          this.buf = new Int16Array(this.size);
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("vtech-capture", VtechCapture);
`;
let workletUrl;

class VtechTalkCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("media_player.")) {
      throw new Error("entity: the camera's speaker (media_player.…) is required");
    }
    this._config = config;
    this._state = "idle";
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 2;
  }

  getGridOptions() {
    return { columns: 12, rows: 2, min_rows: 2 };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass.entities || {}).find(
      (e) => e.platform === "vtech_baby" && e.entity_id.startsWith("media_player."),
    );
    return { entity: entity ? entity.entity_id : "media_player." };
  }

  connectedCallback() {
    this._onHidden = () => document.hidden && this._release();
    document.addEventListener("visibilitychange", this._onHidden);
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onHidden);
    this._release();
  }

  _render() {
    if (this._root) return;
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        ha-card { padding: 12px; display: flex; flex-direction: column; gap: 8px; height: 100%; box-sizing: border-box; justify-content: center; }
        button {
          display: flex; align-items: center; justify-content: center; gap: 10px;
          width: 100%; min-height: 64px; border: none; border-radius: 32px;
          font: inherit; font-size: 1.1em; font-weight: 500; cursor: pointer;
          color: var(--text-primary-color, #fff); background: var(--primary-color);
          touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
        }
        button:disabled { cursor: default; opacity: 0.5; }
        button.talking { background: var(--error-color, #db4437); animation: pulse 1.2s ease-in-out infinite; }
        button.opening, button.finishing { opacity: 0.8; cursor: progress; }
        @keyframes pulse { 50% { filter: brightness(1.2); } }
        .status { min-height: 1.2em; text-align: center; font-size: 0.9em; color: var(--secondary-text-color); }
        .status.error { color: var(--error-color, #db4437); }
      </style>
      <ha-card>
        <button type="button"><ha-icon icon="mdi:microphone"></ha-icon><span class="label"></span></button>
        <div class="status" role="status"></div>
      </ha-card>`;
    const button = this._root.querySelector("button");
    button.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      button.setPointerCapture(e.pointerId);
      this._press();
    });
    for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) {
      button.addEventListener(ev, () => this._release());
    }
    button.addEventListener("keydown", (e) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        this._press();
      }
    });
    button.addEventListener("keyup", (e) => {
      if (e.key === " " || e.key === "Enter") this._release();
    });
    button.addEventListener("contextmenu", (e) => e.preventDefault());
    this._update();
  }

  _update() {
    if (!this._root) return;
    const button = this._root.querySelector("button");
    const stateObj = this._hass && this._hass.states[this._config.entity];
    const supported = !!(navigator.mediaDevices && window.AudioWorkletNode);
    const available = stateObj && stateObj.state !== "unavailable";
    button.className = this._state;
    button.disabled = !supported || !available || this._state === "finishing";
    button.setAttribute("aria-pressed", String(this._state === "talking"));
    const idle = this._config.name || LABELS.idle;
    this._root.querySelector(".label").textContent = this._state === "idle" ? idle : LABELS[this._state];
    if (!supported) this._status("The microphone needs HTTPS and a current browser", true);
    else if (!stateObj) this._status(`${this._config.entity} not found`, true);
    else if (!available) this._status("Camera unavailable", true);
    else if (this._statusFromAvailability) this._status("");
  }

  _status(text, error = false, clearAfterMs = 0) {
    const el = this._root.querySelector(".status");
    el.textContent = text;
    el.classList.toggle("error", error);
    this._statusFromAvailability = error && !clearAfterMs;
    clearTimeout(this._statusTimer);
    if (clearAfterMs) this._statusTimer = setTimeout(() => this._status(""), clearAfterMs);
  }

  _setState(state) {
    this._state = state;
    this._update();
  }

  async _press() {
    if (this._state !== "idle" || this._root.querySelector("button").disabled) return;
    const talk = { held: true, handlerId: null, queue: [], ended: false };
    this._talk = talk;
    this._status("");
    this._setState("opening");
    try {
      await this._openMicrophone(talk);
      if (!talk.held) return this._finish(talk);          // released while the microphone opened
      talk.unsub = await this._hass.connection.subscribeMessage(
        (ev) => this._onEvent(talk, ev),
        { type: "vtech_baby/talk", entity_id: this._config.entity, sample_rate: talk.ctx.sampleRate },
        { resubscribe: false },
      );
    } catch (err) {
      this._closeMicrophone(talk);
      this._talk = null;
      this._setState("idle");
      this._status(this._describe(err), true, 10000);
      return;
    }
    if (!talk.held) return this._stopTalking(talk);       // released while connecting
    this._setState("talking");
    talk.limit = setTimeout(() => this._release(), MAX_TALK_MS);
  }

  async _openMicrophone(talk) {
    talk.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    // The context runs at the microphone's own rate: Firefox can't connect a microphone to a context
    // at another rate, and the integration resamples anyway.
    talk.ctx = new AudioContext();
    workletUrl = workletUrl || URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
    await talk.ctx.audioWorklet.addModule(workletUrl);
    const source = talk.ctx.createMediaStreamSource(talk.stream);
    const capture = new AudioWorkletNode(talk.ctx, "vtech-capture");
    const mute = talk.ctx.createGain();
    mute.gain.value = 0;                                   // keeps the graph running, silently
    source.connect(capture).connect(mute).connect(talk.ctx.destination);
    capture.port.onmessage = (e) => this._send(talk, new Uint8Array(e.data));
  }

  _closeMicrophone(talk) {
    if (talk.stream) talk.stream.getTracks().forEach((t) => t.stop());
    if (talk.ctx && talk.ctx.state !== "closed") talk.ctx.close();
    talk.stream = talk.ctx = null;
  }

  _send(talk, pcm) {
    if (talk.ended) return;
    if (talk.handlerId === null) {
      talk.queue.push(pcm);                                // until the integration is ready
      return;
    }
    const msg = new Uint8Array(1 + pcm.byteLength);
    msg[0] = talk.handlerId;
    msg.set(pcm, 1);
    this._hass.connection.socket.send(msg);
  }

  _endAudio(talk) {
    if (talk.ended || talk.handlerId === null) return;
    talk.ended = true;
    this._hass.connection.socket.send(new Uint8Array([talk.handlerId]));   // empty: end of audio
  }

  _onEvent(talk, ev) {
    if (ev.type === "ready") {
      talk.handlerId = ev.handler_id;
      for (const pcm of talk.queue.splice(0)) this._send(talk, pcm);
      if (!talk.held) this._endAudio(talk);
    } else if (ev.type === "end") {
      this._finish(talk, "Played");
    } else if (ev.type === "error") {
      this._finish(talk, `Not played: ${ev.message}`, true);
    }
  }

  _release() {
    const talk = this._talk;
    if (!talk || !talk.held) return;
    talk.held = false;
    clearTimeout(talk.limit);
    this._closeMicrophone(talk);
    if (talk.unsub) this._stopTalking(talk);               // else still connecting: _press finishes it
  }

  _stopTalking(talk) {
    this._endAudio(talk);
    this._setState("finishing");
    talk.watchdog = setTimeout(() => this._finish(talk, "No answer from the camera", true), END_WAIT_MS);
  }

  _finish(talk, message = "", error = false) {
    if (this._talk !== talk) return;
    this._talk = null;
    clearTimeout(talk.limit);
    clearTimeout(talk.watchdog);
    this._closeMicrophone(talk);
    if (talk.unsub) talk.unsub().catch(() => {});        // the integration may have closed it already
    this._setState("idle");
    if (message) this._status(message, error, error ? 10000 : 3000);
  }

  _describe(err) {
    if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
      return "Microphone blocked: allow it for this site";
    }
    if (err && err.code === "busy") return "Someone else is talking";
    return `Can't talk: ${(err && err.message) || err}`;
  }
}

if (!customElements.get("vtech-talk-card")) {
  customElements.define("vtech-talk-card", VtechTalkCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "vtech-talk-card",
    name: "VTech hold to talk",
    description: "Hold and talk to stream your voice live through a VTech baby monitor's speaker.",
  });
}
