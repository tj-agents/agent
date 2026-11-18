// Claude owns native execution. TJ owns durable requests and recovery after adapter loss.
let active = null;
let generation = 0;
const maxLineLength = 4 * 1024 * 1024;

function status($, message) {
  try {
    $.ui.status("TJ native bridge: " + message);
  } catch {
    // A closing/unloaded UI must not prevent the host event from continuing.
  }
}

function stop($, bridge) {
  if (!bridge || bridge.stopped) return;
  bridge.stopped = true;
  if (active === bridge) active = null;
  try {
    // Do not hold session.end while an in-flight pull/send finishes. Claude also
    // kills the child on module unload; return() ends it on a session change.
    Promise.resolve(bridge.stream.return()).catch(error => status($, String(error)));
  } catch (error) {
    status($, String(error));
  }
}

async function acknowledge($, source, request, result) {
  const saved = await $.process.run([
    "tj", "_native-receipt", "--session", source,
    "--request", request.id, "--lease", request.lease, "--result", JSON.stringify(result),
  ], { env: { CLAUDECODE: "1", CLAUDE_CODE_SESSION_ID: source.slice(7), CODEX_THREAD_ID: "" } });
  if (saved.exitCode !== 0) {
    throw new Error("Native outcome was not recorded; do not resend: " + saved.stderr);
  }
}

async function reloadPlugins($, bridge, request) {
  let result;
  try {
    const output = await $.command.run({ command: "reload-plugins" });
    result = { completed: true, output };
  } catch (error) {
    result = { error: String(error) };
  }
  // Unload can retire this continuation. Only its still-owning session may acknowledge it.
  if (active !== bridge) return;
  const current = "claude:" + await $.session.id();
  if (active !== bridge) return;
  if (current !== bridge.source) {
    status($, "session changed; reload outcome remains unverified");
    return;
  }
  await acknowledge($, bridge.source, request, result);
}

async function dispatch($, bridge, request) {
  const source = bridge.source;
  if (!request || Array.isArray(request) || typeof request !== "object"
      || typeof request.id !== "string" || !/^[A-Za-z0-9_-]+$/.test(request.id)
      || typeof request.lease !== "string" || !/^[A-Fa-f0-9]{32}$/.test(request.lease)
      || request.session !== source) {
    throw new Error("Invalid TJ native request");
  }
  if (request.operation === "message") {
    if (typeof request.target !== "string" || !/^claude:[A-Za-z0-9_-]+$/.test(request.target)
        || typeof request.text !== "string" || !request.text.trim()) {
      throw new Error("Invalid TJ native message request");
    }
  } else if (request.operation === "reload_plugins") {
    if (typeof request.revision !== "string" || !request.revision.trim()) {
      throw new Error("Invalid TJ native reload revision");
    }
  } else {
    throw new Error("Unknown TJ native operation");
  }
  if (active !== bridge) return;
  if (bridge.requests.has(request.id)) return;
  bridge.requests.add(request.id);
  const current = "claude:" + await $.session.id();
  if (active !== bridge) return;
  if (current !== source) {
    if (request.operation === "message") {
      await acknowledge($, source, request, { isDelivered: false, reason: "The sending session changed" });
    } else {
      status($, "session changed; reload was not started");
    }
    return;
  }
  if (request.operation === "reload_plugins") {
    // A queued reload waits for idle; messages must still flow to an active turn.
    const completion = reloadPlugins($, bridge, request)
      .catch(error => {
        stop($, bridge);
        status($, "unavailable: " + String(error));
      })
      .finally(() => bridge.reloads.delete(completion));
    bridge.reloads.add(completion);
    return;
  }
  let result;
  try {
    result = await $.session.send({ to: { sessionId: request.target.slice(7) }, text: request.text });
  } catch (error) {
    result = { error: String(error) };
  }
  // isDelivered means native queue submission. Receiver hold/refusal/read are not proved by this API.
  await acknowledge($, source, request, result);
}

async function consume($, bridge) {
  const stream = bridge.stream;
  let pending = "";
  for await (const chunk of stream) {
    if (active !== bridge) return;
    if (chunk.stream === "stderr") {
      status($, chunk.text.trim());
      continue;
    }
    pending += chunk.text;
    let newline;
    while ((newline = pending.indexOf("\n")) >= 0) {
      if (newline > maxLineLength) throw new Error("TJ native request exceeds the adapter limit");
      const line = pending.slice(0, newline);
      pending = pending.slice(newline + 1);
      if (active !== bridge) return;
      if (line.trim()) await dispatch($, bridge, JSON.parse(line));
    }
    if (pending.length > maxLineLength) throw new Error("TJ native request exceeds the adapter limit");
  }
  if (active !== bridge) return;
  const ended = await stream.result;
  if (active !== bridge) return;
  if (pending || ended.code !== 0) throw new Error("TJ native adapter stopped; outcome may be unknown");
}

async function start($, starting) {
  const source = "claude:" + await $.session.id();
  if (starting !== generation || active?.source === source) return;
  stop($, active);
  const stream = $.process.spawn({
    argv: ["tj", "_native-host", "--session", source],
    env: { CLAUDECODE: "1", CLAUDE_CODE_SESSION_ID: source.slice(7), CODEX_THREAD_ID: "" },
  });
  const bridge = { source, stream, requests: new Set(), reloads: new Set() };
  active = bridge;
  // result can reject before the consumer reaches it (or when return kills it).
  // The consumer reports failures; this handler also observes cancelled results.
  stream.result.catch(() => {});
  consume($, bridge)
    .then(() => {
      const ended = active === bridge;
      stop($, bridge);
      if (ended) status($, "queue host stopped");
    }, error => {
      stop($, bridge);
      status($, "unavailable: " + String(error));
    });
}

async function startup($, event, next) {
  const starting = ++generation;
  const started = await next(event);
  if (starting !== generation) return started;
  try {
    await start($, starting);
  } catch (error) {
    status($, "unavailable: " + String(error));
  }
  return started;
}

export function register(on) {
  on("session.start", startup);
  on("classic.SessionStart", startup);
  on("session.end", ($, event, next) => {
    ++generation;
    stop($, active);
    return next(event);
  });
}
