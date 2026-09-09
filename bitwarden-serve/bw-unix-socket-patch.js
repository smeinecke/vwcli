const net = require("node:net");
const fs = require("node:fs");
const path = require("node:path");

const originalListen = net.Server.prototype.listen;

const SOCKET_PATH =
  process.env.BW_SERVE_SOCKET ||
  `/run/user/${process.getuid()}/bw.sock`;

function ensureParentDir(socketPath) {
  fs.mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o700 });
}

function cleanupStaleSocket(socketPath) {
  try {
    const st = fs.statSync(socketPath);
    if (st.isSocket()) {
      fs.unlinkSync(socketPath);
    }
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
}

net.Server.prototype.listen = function patchedListen(...args) {
  // Common cases:
  //   listen(port)
  //   listen(port, host)
  //   listen(options)
  //
  // Rewrite only TCP-style listen calls into a Unix socket path.
  let shouldRewrite = false;

  if (typeof args[0] === "number") {
    shouldRewrite = true;
  } else if (
    args[0] &&
    typeof args[0] === "object" &&
    Object.prototype.hasOwnProperty.call(args[0], "port")
  ) {
    shouldRewrite = true;
  }

  if (!shouldRewrite) {
    return originalListen.apply(this, args);
  }

  ensureParentDir(SOCKET_PATH);
  cleanupStaleSocket(SOCKET_PATH);

  const lastArg = args[args.length - 1];
  const cb = typeof lastArg === "function" ? lastArg : undefined;

  const result = cb
    ? originalListen.call(this, SOCKET_PATH, cb)
    : originalListen.call(this, SOCKET_PATH);

  this.once("listening", () => {
    try {
      fs.chmodSync(SOCKET_PATH, 0o600);
    } catch {}
  });

  this.once("close", () => {
    try {
      fs.unlinkSync(SOCKET_PATH);
    } catch {}
  });

  return result;
};