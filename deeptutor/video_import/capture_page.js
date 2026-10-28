// Injected into an open YouTube watch page by PlaywrightCapture.
// Records the in-page <video> element's audio during real-time playback and
// reports player state. Nothing is downloaded: audio is taken from the element's
// own output while it plays at 1x, and chunks are handed to the host through
// the exposed `window.__dtAudioChunk(base64)` function.
(() => {
  if (window.__dtCapture) return;

  const player = () => document.querySelector("#movie_player");
  const video = () =>
    document.querySelector("#movie_player video") || document.querySelector("video");

  let recorder = null;
  let pending = Promise.resolve();
  let chunks = 0;

  const toBase64 = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  };

  const send = (blob) => {
    if (!blob || !blob.size) return;
    pending = pending
      .then(() => blob.arrayBuffer())
      .then((buffer) => {
        chunks += 1;
        return window.__dtAudioChunk(toBase64(buffer));
      });
  };

  window.__dtCapture = {
    // Route the element's audio into a recordable stream and a GainNode at 0,
    // so recording gets full-volume audio while the speakers stay silent.
    async start() {
      const v = video();
      const ctx = new AudioContext();
      const source = ctx.createMediaElementSource(v);
      const sink = ctx.createMediaStreamDestination();
      const mute = ctx.createGain();
      mute.gain.value = 0;
      source.connect(sink);
      source.connect(mute);
      mute.connect(ctx.destination);
      await ctx.resume();
      recorder = new MediaRecorder(sink.stream, { mimeType: "audio/webm;codecs=opus" });
      recorder.ondataavailable = (event) => send(event.data);
      recorder.start(10000);
      await v.play().catch(() => {});
      return sink.stream.getAudioTracks().length;
    },

    pause() {
      if (recorder && recorder.state === "recording") recorder.pause();
    },

    resume() {
      if (recorder && recorder.state === "paused") recorder.resume();
    },

    async stop() {
      if (recorder && recorder.state !== "inactive") {
        await new Promise((resolve) => {
          recorder.onstop = resolve;
          recorder.stop();
        });
      }
      await pending;
      return chunks;
    },

    state() {
      const v = video();
      const p = player();
      // YouTube's in-player error overlay (e.g. "Something went wrong").
      const error = document.querySelector("#movie_player .ytp-error");
      if (v && v.paused && !v.ended && !error) v.play().catch(() => {});
      return {
        error: error && error.offsetParent !== null ? error.innerText.trim() : "",
        ad: !!(p && p.classList.contains("ad-showing")),
        t: v ? v.currentTime : 0,
        duration: v && Number.isFinite(v.duration) ? v.duration : 0,
        ended: !!(v && v.ended),
      };
    },
  };
})();
