const backend = Bun.spawn(["bun", "--watch", "server/index.ts"], {
  stdout: "inherit",
  stderr: "inherit",
  env: { ...process.env, NODE_ENV: "development", DASHBOARD_PORT: "8790" },
});
const frontend = Bun.spawn(["vp", "dev"], { stdout: "inherit", stderr: "inherit" });
let stopping = false;
function stop() {
  if (stopping) return;
  stopping = true;
  backend.kill();
  frontend.kill();
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
const code = await Promise.race([backend.exited, frontend.exited]);
stop();
await Promise.all([backend.exited, frontend.exited]);
process.exit(code);
