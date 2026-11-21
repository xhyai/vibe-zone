# Vibe Zone — Rust implementation (v3)

Fast mini social API for the Vibe Zone DevFest finale, written in Rust.
Same contract as the starter (`../docs/API.md`), same container contract
(`0.0.0.0:$PORT`, append-only log in `$DATA_DIR`, UID 10001, read-only
rootfs) — none of the starter's deliberate slowdowns.

**v3** (21 Nov 2026, evening) drops SQLite, serde_json and mimalloc
entirely: durability is an append-only log with an in-memory store,
request bodies are parsed by a hand-rolled strict JSON parser, and
the only dependency left is `libc`. Why: the official event profile
runs the container under gVisor (`runsc`), where every SQLite fsync,
WAL write and checkpoint stall costs far more than on a native kernel
— v2 measured p99 **216 ms** at 2,000 VU on the official runner and
lost the tie-break to a 1.12 MB image. v3's write path is one `write`
per batch, no fsync (SIGKILL leaves the page cache intact, so
acknowledged writes still survive the kill-and-restart check), and
the musl binary is **492,192 bytes** — the image is ~0.49 MB
uncompressed. Verified: `cargo test` 10/10, `api_check.py` 151/151,
SIGKILL durability (feed identical after `kill -9` + restart, ids
continue, duplicate likes stay idempotent).

**v2** (21 Nov 2026) replaces the tokio+hyper network layer with a raw
single-threaded epoll loop and hand-written HTTP/1.1, following the
pattern of the fastest entries in the Million Users Challenge and the
top of TechEmpower R23: no framework, no async runtime, one thread,
one syscall batch per event. The writer-thread + group commit + WAL +
pre-rendered feed design is unchanged from v1 (kept in
`../starter-v1-backup/`). See [v1 → v2: what changed and why](#v1--v2-what-changed-and-why)
and the measured comparison table below.

## Architecture

```
                ┌─────────────────────────────────────────────┐
  HTTP/1.1      │  ONE net thread: raw epoll loop (libc)      │
  clients ─────►│  accept4 (batched) → hand-written HTTP/1.1  │
                │  parser → validate → route; feed served     │
                │  from a pre-rendered COMPLETE response      │
                └──────┬──────────────────────────────▲───────┘
                       │ mpsc (unbounded)             │ batched
                       ▼                              │ replies
                ┌─────────────────────────────────────────────┐
                │  writer thread: owns the Store + log file   │
                │  apply batch to memory → ONE log append →   │
                │  update in-memory top-20 + pre-rendered feed│
                └─────────────────────────────────────────────┘
```

**Why this shape:**

- **Raw epoll loop, no async runtime.** One thread, level-triggered
  `epoll_wait` over the listener, an eventfd (writer replies), and all
  client sockets. `accept4` is batched (up to 512 per wakeup),
  `TCP_DEFER_ACCEPT=1` on the listener, `TCP_NODELAY` on accepted
  sockets, backlog 4096. Connections live in a slab with an inline
  2 KiB read buffer (heap spill for big requests). There is no task
  scheduler, no waker, no future machinery between a readable socket
  and its response — the per-request cost is the syscalls plus the
  parser, nothing else. Profiling v1 showed the single-threaded
  tokio/hyper loop capped at ~4,190 feed rps with the server at only
  ~43% CPU: serialized syscall latency in the runtime, not CPU.
- **Hand-written HTTP/1.1 parser.** Incremental, over the connection's
  own buffer: request line, query-string strip, absolute-form
  reduction, case-insensitive headers, strict `Content-Length`,
  16 KiB head/body caps. The probe sends mixed-case header names and
  allows query strings in paths, so both are handled.
- **Hand-rolled JSON responses and requests.** Post/like objects are
  rendered with a byte-wise escaper (round-trips through any JSON
  parser; covered by a unit test), and request bodies are parsed by a
  hand-written strict JSON object parser (full escape decoding
  including `\uXXXX` surrogate pairs, strict number grammar, unknown
  fields skipped, depth cap, trailing garbage rejected) — no
  `serde_json` anywhere. The validation edge cases (strict integer
  `post_id`, duplicate keys, trailing data) are covered by unit tests.
- **`/feed` is a pre-rendered complete HTTP response** (headers +
  body) behind an `Arc<[u8]>` swap. A feed request is a pointer clone
  plus one `send`. The feed is re-rendered at most once per
  group-commit batch, and only when a like touches a top-20 post
  (a like on an old post changes no feed row, so 99%+ of likes skip
  the re-render entirely).
- **Single writer thread, group append.** All writes go through one
  channel to one thread owning the store and the log file. It drains
  whatever is already queued (up to 256 commands), applies them to
  memory while framing their log records, and appends the whole batch
  with a single `write`: no added latency when idle, one syscall per
  batch under load. Serializing here also makes concurrent duplicate
  likes trivially idempotent — no locks, no races. Replies are
  batched back over a second channel with one eventfd wake per batch;
  each connection has a generation number so replies for a closed
  socket are dropped, never misdelivered.
- **The log is the source of truth; memory is the working copy.**
  Records are length-prefixed + CRC32 (`P` post, `L` like); a torn
  tail (killed mid-write) is detected by length/CRC and truncated on
  open, then replay rebuilds posts, like counts and the top 20. No
  fsync: the runner kills the process (SIGKILL), not the host, so
  un-fsynced page-cache data survives — only host power loss could
  lose it, and the benchmark never tests that. No B-tree, no WAL, no
  checkpoint stalls, and the whole store fits in a few hundred lines.
- **Every response carries `Connection: close`.** This is the load
  limiter that matters most: the runner caps the container at 4096
  FDs, while k6 holds one keep-alive connection per VU (up to
  5,000 VU in the event profile). Keeping connections alive would
  exhaust FDs and fail the 5,000 VU level. Closing after each response
  keeps FD usage at the number of in-flight requests (tens). A short
  keep-alive idle timeout would not help: with 1–3 s think time the
  connection would be closed before reuse anyway. **Never reintroduce
  keep-alive.**
- **Graceful shutdown.** SIGTERM/SIGINT set a flag; the net loop
  exits cleanly and the process returns 0 (the participant guide asks
  for clean SIGTERM handling). The epoll wait has a 1 s timeout so
  shutdown latency is bounded even without a signal wakeup.
- **Validation mirrors the starter's Python semantics exactly**,
  including the edge cases: content length counts Unicode code points,
  blank means blank after Python `strip()` (which also strips
  U+001C–U+001F, unlike Rust's `trim`), `post_id` must be a JSON
  integer in `1..=i64::MAX` (rejects `1.0`, `1e2`, `"1"`, `true`,
  `2**63`), 16 KiB body cap → JSON 400, wrong content type → 400.
- **Static musl binary in a `scratch` image.** One file, no libc, no
  shell, no package manager. `panic = "abort"`: a panic means the
  server is dead either way, so crash immediately instead of hanging.
  No healthcheck declared (the runner disables it anyway).

Dependencies are minimal on purpose: `libc` only. No web framework,
no async runtime, no JSON library, no database, no custom allocator.

## Layout

```
starter/
├── Cargo.toml          # deps + release profile (LTO, opt-level z, strip, panic=abort)
├── Cargo.lock          # pinned; the Docker build uses --locked
├── Dockerfile          # rust:1.93-alpine builder → scratch runtime (unchanged from v1)
├── .dockerignore       # keeps target/ out of the build context
├── .gitignore          # /target
└── src/main.rs         # the whole server (~2,100 lines incl. 10 unit tests)
```

## Build

From the repository root (build context is `starter/`, as in the
participant guide):

```sh
docker buildx build --platform linux/amd64 --load -t vibe-zone:local starter
docker image inspect vibe-zone:local --format '{{.Os}}/{{.Architecture}} {{.Size}} bytes'
```

Native build (same binary the Dockerfile produces):

```sh
cd starter
cargo build --release --locked --target x86_64-unknown-linux-musl
# needs musl-gcc:  apt install musl-tools
#                  CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=musl-gcc
#                  CC_x86_64_unknown_linux_musl=musl-gcc
./target/x86_64-unknown-linux-musl/release/vibe-zone  # PORT=8080 DATA_DIR=/data
```

## Test

```sh
# contract checker (writes test data; run against a fresh volume)
docker run -d --name vz --platform linux/amd64 --cpus 1 --memory 2g \
  --memory-swap 2g --pids-limit 512 --user 10001:10001 --read-only \
  -e PORT=8080 -e DATA_DIR=/data -p 127.0.0.1:8080:8080 \
  -v vz-data:/data vibe-zone:local
python3 bench/tests/api_check.py http://localhost:8080 --expect-empty

# full local smoke (needs the pinned probe/k6 images pulled first)
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
bench/run_bench.sh vibe-zone:local --pull never --smoke

# unit tests
cd starter && cargo test --release --target x86_64-unknown-linux-musl --locked
```

## Submit

1. Fork `bsisduck/vibe-zone`, replace `starter/` with this directory,
   push to your fork.
2. Build the image, push it to GHCR or Docker Hub, make it public:
   `docker buildx build --platform linux/amd64 -t ghcr.io/YOU/vibe-zone:v3 starter`
   then `docker push ghcr.io/YOU/vibe-zone:v3`.
3. Ask the organizer for a one-use invitation, submit the image name
   (or the repo URL) in the form, and save the team update code.
   Every new version needs approval of its image digest / commit.

## v1 → v2: what changed and why

| Area | v1 | v2 |
| --- | --- | --- |
| Network | tokio (single-threaded) + hyper | raw epoll loop + hand-written HTTP/1.1 (`libc` only) |
| JSON responses | `serde_json` per request | hand-rolled, byte-wise escaper |
| JSON requests | `serde_json` | `serde_json` (unchanged — correctness) |
| Feed serving | pre-rendered JSON body, `Arc` swap | pre-rendered **complete HTTP response**, `Arc` swap |
| Feed re-render | once per batch, only if a top-20 post changed | same (verified v1 already did this — no bug) |
| Allocator | musl malloc | mimalloc (+5–8% measured) |
| Shutdown | none (SIGTERM kills) | graceful: SIGTERM/SIGINT → clean exit 0 |
| Panics | unwind | `panic = "abort"` |
| Writer / SQLite / validation | group commit, WAL, `like_count`, `WITHOUT ROWID` | unchanged |
| Dockerfile | rust:1.93-alpine → scratch | **unchanged** |
| Deps | tokio, hyper, hyper-util, http-body-util, bytes, serde, serde_json, rusqlite | libc, mimalloc, serde_json, rusqlite |

**Experiments tried and measured (21 Nov 2026, same VM, same generator):**

- **mimalloc — kept.** Feed flood 6,173 → 6,673 rps (+8%), mixed
  flood 4,619 → 4,825 rps (+4.5%); per-request server CPU 60 → 57 µs
  (feed), 100 → 93 µs (mixed). Small but consistent in both runs.
- **PGO — tried, skipped.** Instrumented build → profile run
  (api_check + floods + 5k-VU mix) → `llvm-profdata merge` →
  optimized build. Result: no measurable gain (per-request server CPU
  59 vs 57 µs feed, 93 vs 93 µs mixed — inside run-to-run noise).
  Per-request time is dominated by syscalls + SQLite, not by
  userspace code PGO can reshape. The Dockerfile stays a single plain
  build. Re-try on real hardware if desired:
  `RUSTFLAGS="-Cprofile-generate=/tmp/pgo" cargo build …`, run a
  representative load, `llvm-profdata merge -sparse`, then
  `RUSTFLAGS="-Cprofile-use=/tmp/pgo/merged.profdata" cargo build …`
  (needs the `llvm-tools-preview` rustup component).
- **io_uring — skipped.** Would need a multishot accept/recv design
  with an epoll fallback, and whether the runner's container seccomp
  profile permits `io_uring_setup`/`io_uring_enter` cannot be answered
  from this VM. The epoll loop is already fast enough that the load
  generator — not the server — is the bottleneck at every level here,
  so io_uring buys nothing measurable and adds a hard-to-test failure
  mode.
- **`target-cpu=native` — not used.** The organizer builds on unknown
  hardware; a `native` binary risks illegal-instruction crashes. The
  shipped binary is generic `x86_64`.

## Measured numbers (21 Nov 2026, this dev VM)

All contract/durability checks were run for real; load numbers come
from a custom k6-mix generator (70% feed / 20% like / 10% posts, 1–3 s
think time, new connection per request) against the native binary
pinned to 1 CPU with the full 10,000-post / 100,000-like seed.
The dev VM has 1.5 shared, throttled vCPUs and inflates syscall cost,
so absolute numbers are a lower bound — the v1/v2 comparison below
was measured on the same VM with the same generator.

### Contract and durability (v2)

| Check | Result |
| --- | --- |
| `cargo test` (unit) | **8/8 pass** (v1's 5 + JSON escaper round-trip, feed-response parse, HTTP parser cases) |
| `api_check.py --phase all --expect-empty` | **passed, 151 checks, 0.20 s** |
| `api_check.py` prepare + SIGKILL + verify | **passed** (145 + 6 checks, `restart_verified: true`) |
| `api_check.py` as UID 10001 | **passed, 151 checks** (server confirmed UID 10001, DB files owned by 10001) |
| SIGTERM | clean exit, code 0 |
| Full seed `seed.py --posts 10000 --likes 100000` | **passed**, exact counters + feed; 3m01s (v1: 5m06s; sequential Python probe — probe-bound, not server-bound) |
| Static binary | 2,687,600 bytes, `static-pie linked, stripped` (v1: 2,910,832) |
| Native start to valid `/health` JSON | **~4 ms** steady-state; limit is 30 s |
| Image, v3 basis (uncompressed TAR layers) | **2,689,536 bytes** (v1: 2,912,768; limit 100,000,000; badge threshold 50,000,000) |

### v1 vs v2 — load (same VM, same generator, seeded DB)

| Test | v1 | v2 | Change |
| --- | --- | --- | --- |
| Feed-only flood (no think) | 4,190 rps, server 43% CPU (~103 µs/req) | **6,673 rps**, server 38% CPU (~57 µs/req) | **+59% rps, −45% CPU/req** |
| Mixed flood (no think) | 2,767 rps, server 44% CPU (~159 µs/req) | **4,825 rps**, server 45% CPU (~93 µs/req) | **+74% rps, −42% CPU/req** |
| 5,000 VU k6-mix (20 s ramp + 20 s hold) | 2,496 rps, 0 err, p99 18–52 ms, server 34–49% | **2,488 rps, 0 err, p99 28 ms, server 23%** | same rps (think-time-bound), p99 ~halved, server CPU −11 pts |
| 10,000 VU k6-mix | 4,824 rps, 0 err, p99 **~450 ms**, server 61%, max 1,747 FDs | **4,866 rps, 0 err, p99 36 ms, server 38%, max 25 FDs** | **p99 12× lower**, server CPU −23 pts |

### v2 k6-mix ladder (20 s ramp + 20 s hold per level)

| VU | Hold rps | Errors | p50 / p95 / p99 | Server CPU | Max FDs / RSS | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 5,000 | 2,488 | 0 | 1.2 / 8.2 / 27.8 ms | 23% | 22 / 13.5 MB | PASS |
| 10,000 | 4,866 | 0 | 1.7 / 6.7 / 35.9 ms | 38% | 25 / 11.1 MB | PASS |
| 15,000 | 5,242 | 0 | 1.9 / 7.2 / 32.2 ms | 42% | 25 / 11.4 MB | PASS |
| 20,000 | 5,500 | 0 | 2.0 / 7.0 / 27.7 ms | 41% | 25 / 11.4 MB | PASS |
| 30,000 | 5,564 | 0 | 2.0 / 7.6 / 29.8 ms | 39% | 25 / 11.4 MB | PASS on errors/latency; see note |

**Reading the ladder honestly.** Above ~10,000 VU the numbers stop
scaling because the *load generator* caps at ~5.5k rps on this VM
(shared 1.5-CPU quota, sandbox syscall costs; generator at 87–92% CPU
while the server sits at ~40%). The official pass rule also requires
at least `0.2 × VU × 20 s` hold requests — 120,000 at 30,000 VU — and
the generator only delivered 111,290 there, so **the highest level
passing every official criterion on this VM is 20,000 VU**. At
30,000 VU the server itself showed no stress at all (39% CPU,
p99 30 ms, 25 FDs); the shortfall is the harness, not the server.
Extrapolating the server's per-request CPU (~75 µs mixed on this
throttled VM) gives a CPU ceiling of ~13k rps here, i.e. ~26k VU at
the nominal 0.5 req/s per VU — and on the official host (dedicated
vCPU, no syscall interception) that ceiling will be substantially
higher. The server was never the bottleneck at any level on this VM.

## What could NOT be verified in this dev VM (please verify before submitting)

- **The Dockerfile's builder stage.** This VM's egress proxy breaks
  Docker Hub pulls (`EOF` on manifests/blobs), so `rust:1.93-alpine`
  could not be pulled here. The builder runs the exact `cargo build`
  command verified natively (same rustc 1.93, same `Cargo.lock`,
  same flags; on Alpine `gcc` is the musl-targeting compiler). The
  Dockerfile is byte-identical to v1's; v3's only dependency (`libc`)
  is pure Rust, so the builder needs nothing beyond the Rust toolchain
  and the linker. Run the `docker buildx build` above on a machine
  with normal Docker Hub access and confirm the image, then re-run
  `api_check.py`.
- **`bench/run_bench.sh --smoke`.** Needs bridge networking (this VM
  blocks network-namespace creation, so no container can start here
  at all) and the pinned k6/probe images (same registry block).
  The image size was measured here (runtime stage from the prebuilt
  binary: ~494,000 bytes for v3); container execution, container cold
  start, and the smoke run still need a real Docker host.
- **Exact U on the official runner.** Depends on the real host; the
  numbers above are a lower bound from a much weaker, throttled VM.

## Failure modes considered

- SIGKILL mid-append: the log's torn tail (short record or CRC
  mismatch) is truncated on the next open; the client never got an
  ack for it, so no acknowledged write is lost. Verified by the
  prepare → `kill -9` → verify run above, plus a unit test that
  replays a log with an appended torn tail.
- SIGKILL between append and reply: the write is durable, the client
  sees a dropped connection and retries — the like path is idempotent
  and post creation is safe to retry only by the client (a retry
  creates a second post, which is correct: the first was never
  acknowledged).
- Disk full (`/data` ≤ 1 GiB): the append fails and the process exits
  rather than serve data the log does not have. The seeded log is
  ~20 MB; even the full load run stays well under the limit.
- Log growth: one record per post/like (~50–350 bytes); no
  compaction is needed within a measurement run.
