const net = require("node:net");
const fs = require("node:fs");
const path = require("node:path");

const originalListen = net.Server.prototype.listen;

const SOCKET_PATH =
  process.env.BW_SERVE_SOCKET ||
  `/run/user/${process.getuid()}/bw.sock`;

// The current Bitwarden CLI validates the HTTP Host header against the
// configured hostname/port. When listening on a Unix socket the hostname is
// meaningless, but a bare "Host: localhost" header (which Python's
// http.client.HTTPConnection sends by default) must still be accepted.
// Force "localhost" and the default HTTP port (80) so the socket service
// works without requiring every client to include a port in the Host header.
const serveIdx = process.argv.indexOf("serve");
if (serveIdx !== -1) {
  const cleanArgs = [];
  let skipNext = false;
  for (let i = 0; i < process.argv.length; i++) {
    if (skipNext) {
      skipNext = false;
      continue;
    }
    if (process.argv[i] === "--hostname" || process.argv[i] === "--port") {
      skipNext = true;
      continue;
    }
    cleanArgs.push(process.argv[i]);
  }
  cleanArgs.splice(serveIdx + 1, 0, "--hostname", "localhost", "--port", "80");
  process.argv = cleanArgs;
}

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
