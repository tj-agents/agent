import { expect, test } from "claude-code/testing";

// Runs the authored module in Claude's Mods engine. Process/session API replies
// are fixtures: CLI queue persistence and real recipient delivery belong to the
// router's integration suite. No process, credential or real agent is used here.
const source = "A1234567-1234-4abc-8def-1234567890AB";
const target = "B1234567-1234-4abc-8def-1234567890CD";
const request = {
  id: "request-1",
  session: "claude:" + source,
  target: "claude:" + target,
  text: "  Native message\nwith Unicode: 🌍  ",
};
const startEvent = { surface: "terminal", isInteractive: true, cwd: "/work" };

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function fixture(on, options = {}) {
  const state = {
    session: source, sends: [], runs: [], spawns: [], statuses: [], starts: 0, ends: 0,
    receipt: deferred(), notice: deferred(), finished: deferred(), release: deferred(),
    idCalls: 0, overrides: [],
  };
  on("session.id", async () => {
    state.idCalls++;
    if (options.identityError) return { deny: options.identityError };
    if (options.identity) return { value: await options.identity(state) };
    return { value: state.session };
  });
  on("session.start", (_$, event) => { state.starts++; return { cwd: event.cwd }; });
  on("classic.SessionStart", () => { state.starts++; return {}; });
  on("session.end", () => { state.ends++; return { sessionId: state.session }; });
  on("ui.status", (_$, event) => {
    state.statuses.push(event.text);
    state.notice.resolve();
    return { value: undefined };
  });
  on("session.send", (_$, event) => {
    state.sends.push(event);
    if (options.sendError) throw new Error(options.sendError);
    if (options.send) return options.send(event);
    return options.sendResult ?? { isDelivered: true };
  });
  on("session.authorize", () => {
    state.overrides.push("authorize");
    throw new Error("The adapter must not authorize sessions");
  });
  on("env.set", () => {
    state.overrides.push("env.set");
    throw new Error("The adapter must not change host policy");
  });
  on("process.run", (_$, event) => {
    state.runs.push(event);
    state.receipt.resolve();
    if (options.runError) return { deny: options.runError };
    return { value: options.runResult ?? { exitCode: 0, stdout: "", stderr: "" } };
  });
  on("process.spawn", async function* (_$, event) {
    state.spawns.push(event);
    try {
      if (options.spawnError) return { deny: options.spawnError };
      const chunks = options.batches?.[state.spawns.length - 1] ?? options.chunks ?? [stdout(request)];
      for (const chunk of chunks) {
        yield chunk;
      }
      await state.release.promise;
      return { value: options.exit ?? { code: 0, signal: null } };
    } finally {
      state.finished.resolve();
      if (options.closeError) throw new Error(options.closeError);
    }
  });
  return state;
}

function stdout(value) {
  return { stream: "stdout", text: JSON.stringify(value) + "\n" };
}

function receipt(state, index = 0) {
  return JSON.parse(state.runs[index].argv.at(-1));
}

async function end($, state) {
  await $.session.end({ reason: "other" });
  state.release.resolve();
  await state.finished.promise;
}

test("native engine sends to the exact session and records the native result", async ($, on) => {
  const state = fixture(on);
  await $.session.start(startEvent);
  await state.receipt.promise;
  expect(state.starts).toBe(1);
  expect(state.sends.length).toBe(1);
  expect(state.sends[0].to).toBe(target);
  expect(state.sends[0].text).toBe(request.text);
  expect(state.runs[0].argv).toEqual([
    "tj", "_message-receipt", "--session", request.session,
    "--request", request.id, "--result", JSON.stringify({ isDelivered: true }),
  ]);
  expect(state.spawns[0].argv).toEqual(["tj", "_message-host", "--session", request.session]);
  expect(state.spawns[0].env).toEqual({
    CLAUDECODE: "1", CLAUDE_CODE_SESSION_ID: source, CODEX_THREAD_ID: "",
  });
  expect(state.runs[0].init.env).toEqual(state.spawns[0].env);
  await end($, state);
  expect(state.statuses).toEqual([]);
  expect(state.overrides).toEqual([]);
});

test("split lines, CRLF, blank lines and stderr preserve both native refusals", async ($, on) => {
  const first = JSON.stringify(request);
  const second = JSON.stringify({ ...request, id: "request-2", text: "second" });
  const native = { isDelivered: false, reason: "Receiver holds messages; consent refused" };
  const twoReceipts = deferred();
  const state = fixture(on, {
    chunks: [
      { stream: "stdout", text: "\r\n" + first.slice(0, 70) },
      { stream: "stderr", text: "queue diagnostic\n" },
      { stream: "stdout", text: first.slice(70) + "\r\n\n" + second + "\n" },
    ],
    send: () => {
      if (state.sends.length === 2) twoReceipts.resolve();
      return native;
    },
  });
  await $.session.start(startEvent);
  await twoReceipts.promise;
  await end($, state);
  expect(state.sends.length).toBe(2);
  expect(state.runs.length).toBe(2);
  expect(receipt(state)).toEqual(native);
  expect(receipt(state, 1)).toEqual(native);
  expect(state.statuses).toEqual(["TJ native messaging: queue diagnostic"]);
  expect(state.overrides).toEqual([]);
});

test("a native send error is an unknown outcome, acknowledged once without resend", async ($, on) => {
  const state = fixture(on, { sendError: "transport lost after submission" });
  await $.session.start(startEvent);
  await state.receipt.promise;
  await $.classic.SessionStart({ source: "startup" });
  await end($, state);
  expect(state.sends.length).toBe(1);
  expect(state.runs.length).toBe(1);
  expect(receipt(state).error).toContain("session.send");
  expect(receipt(state).isDelivered).toBeUndefined();
});

for (const failure of ["exit", "reject"]) {
  test("receipt " + failure + " stops consumption without a second send or ack", async ($, on) => {
    const state = fixture(on, {
      batches: [[{ stream: "stdout", text: stdout(request).text + stdout({ ...request, id: "later" }).text }], []],
      ...(failure === "exit"
        ? { runResult: { exitCode: 1, stdout: "", stderr: "disk full" } }
        : { runError: "receipt unavailable" }),
    });
    await $.session.start(startEvent);
    await state.notice.promise;
    await $.classic.SessionStart({ source: "resume" });
    await end($, state);
    expect(state.spawns.length).toBe(2);
    expect(state.sends.length).toBe(1);
    expect(state.runs.length).toBe(1);
    expect(state.statuses.join(" ")).toContain(failure === "exit" ? "do not resend" : "receipt unavailable");
    expect(receipt(state)).toEqual({ isDelivered: true });
  });
}

for (const [name, text] of [
  ["JSON", "{broken}\n"],
  ["null", "null\n"],
  ["array", "[]\n"],
  ["wrong source", stdout({ ...request, session: "claude:other" }).text],
  ["invalid target", stdout({ ...request, target: "codex:other" }).text],
  ["non-string text", stdout({ ...request, text: {} }).text],
  ["empty text", stdout({ ...request, text: " " }).text],
  ["unsafe request id", stdout({ ...request, id: "../outside" }).text],
  ["oversized line", "x".repeat(4 * 1024 * 1024 + 1)],
]) {
  test("malformed queue: " + name + " cannot send or acknowledge", async ($, on) => {
    const state = fixture(on, { chunks: [{ stream: "stdout", text }] });
    await $.session.start(startEvent);
    await state.notice.promise;
    await end($, state);
    expect(state.starts).toBe(1);
    expect(state.sends).toEqual([]);
    expect(state.runs).toEqual([]);
    expect(state.statuses.length).toBeGreaterThan(0);
  });
}

test("unfinished queue frame and process exit code remain visible", async ($, on) => {
  const state = fixture(on, { chunks: [{ stream: "stdout", text: JSON.stringify(request) }], exit: { code: 9, signal: null } });
  await $.session.start(startEvent);
  state.release.resolve();
  await state.notice.promise;
  expect(state.sends).toEqual([]);
  expect(state.runs).toEqual([]);
  expect(state.statuses.join(" ")).toContain("delivery may be unknown");
});

test("startup identity failure reports in UI and still calls next", async ($, on) => {
  const state = fixture(on, { identityError: "identity unavailable" });
  await $.session.start(startEvent);
  await $.classic.SessionStart({ source: "startup" });
  expect(state.starts).toBe(2);
  expect(state.spawns).toEqual([]);
  expect(state.statuses.join(" ")).toContain("identity unavailable");
});

test("spawn failure reports in UI and does not block startup", async ($, on) => {
  const state = fixture(on, { spawnError: "tj missing" });
  await $.session.start(startEvent);
  await state.notice.promise;
  expect(state.starts).toBe(1);
  expect(state.statuses.join(" ")).toContain("tj missing");
  expect(state.sends).toEqual([]);
});

test("both startup events keep a single stream for the same session", async ($, on) => {
  const state = fixture(on, { chunks: [] });
  await Promise.all([$.session.start(startEvent), $.classic.SessionStart({ source: "startup" })]);
  await $.session.start(startEvent);
  await end($, state);
  expect(state.starts).toBe(3);
  expect(state.spawns.length).toBe(1);
  expect(state.ends).toBe(1);
});

test("session end invalidates an unresolved startup", async ($, on) => {
  const reading = deferred();
  const identity = deferred();
  const state = fixture(on, { identity: () => { reading.resolve(); return identity.promise; } });
  const starting = $.session.start(startEvent);
  await reading.promise;
  await $.session.end({ reason: "clear" });
  identity.resolve(source);
  await starting;
  expect(state.spawns).toEqual([]);
  expect(state.starts).toBe(1);
  expect(state.ends).toBe(1);
});

test("session change while checking identity cannot send the old queued request", async ($, on) => {
  const reading = deferred();
  const identity = deferred();
  const state = fixture(on, {
    batches: [[stdout(request)], []],
    identity: state => {
      if (state.idCalls !== 2) return state.session;
      reading.resolve();
      return identity.promise;
    },
  });
  await $.session.start(startEvent);
  await reading.promise;
  await $.session.end({ reason: "resume" });
  state.session = target;
  await $.classic.SessionStart({ source: "resume" });
  identity.resolve(source);
  await end($, state);
  expect(state.spawns.length).toBe(2);
  expect(state.spawns[1].argv.at(-1)).toBe("claude:" + target);
  expect(state.sends).toEqual([]);
  expect(state.runs).toEqual([]);
});

test("an in-flight send keeps its original session and outcome after session end", async ($, on) => {
  const sending = deferred();
  const outcome = deferred();
  const native = { isDelivered: false, reason: "Native consent refused" };
  const state = fixture(on, { send: () => { sending.resolve(); return outcome.promise; } });
  await $.session.start(startEvent);
  await sending.promise;
  await $.session.end({ reason: "resume" });
  state.session = target;
  outcome.resolve(native);
  await state.receipt.promise;
  state.release.resolve();
  expect(state.sends.length).toBe(1);
  expect(state.runs.length).toBe(1);
  expect(state.runs[0].argv[3]).toBe("claude:" + source);
  expect(receipt(state)).toEqual(native);
});

test("a restarted host consumes only new durable queue entries", async ($, on) => {
  const state = fixture(on, { batches: [[stdout(request)], []] });
  await $.session.start(startEvent);
  await state.receipt.promise;
  await end($, state);
  await $.classic.SessionStart({ source: "resume" });
  await $.session.end({ reason: "other" });
  expect(state.spawns.length).toBe(2);
  expect(state.sends.length).toBe(1);
  expect(state.runs.length).toBe(1);
});

test("a changed native session cannot consume a request for the old sender", async ($, on) => {
  const state = fixture(on, { identity: state => state.idCalls === 1 ? source : target });
  await $.session.start(startEvent);
  await state.receipt.promise;
  await end($, state);
  expect(state.sends).toEqual([]);
  expect(receipt(state)).toEqual({ isDelivered: false, reason: "The sending session changed" });
});

test("clean child exit releases the same-session startup guard", async ($, on) => {
  const state = fixture(on, { chunks: [] });
  await $.session.start(startEvent);
  state.release.resolve();
  await state.notice.promise;
  await $.classic.SessionStart({ source: "startup" });
  await $.session.end({ reason: "other" });
  expect(state.spawns.length).toBe(2);
  expect(state.statuses).toContain("TJ native messaging: queue host stopped");
});

test("shutdown failure cannot prevent session.end from reaching the host", async ($, on) => {
  const state = fixture(on, { chunks: [], closeError: "child shutdown failed" });
  await $.session.start(startEvent);
  await $.session.end({ reason: "other" });
  expect(state.ends).toBe(1);
  state.release.resolve();
  await state.finished.promise;
  expect(state.sends).toEqual([]);
});
