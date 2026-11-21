//! Vibe Zone mini social API — fast Rust implementation (v2).
//!
//! Architecture (see README.md for the full rationale):
//! * One single-threaded epoll loop serves HTTP (1 vCPU target). No async
//!   runtime, no HTTP framework: hand-written HTTP/1.1 parsing, batched
//!   accepts, `TCP_DEFER_ACCEPT`, one `send` per response.
//! * One dedicated writer thread owns the only SQLite connection. All writes
//!   are serialized through it and opportunistically group-committed, so
//!   concurrent duplicate likes can never double-count. Replies come back
//!   in batches over a channel, woken by an eventfd.
//! * SQLite (WAL, synchronous NORMAL) is the durable source of truth; every
//!   acknowledged write is committed before its response is sent, so data
//!   survives SIGKILL.
//! * `GET /feed` never touches SQLite: the writer keeps the newest 20 posts
//!   in memory and a pre-rendered *complete HTTP response* (headers + JSON
//!   body) that the net thread swaps in. A feed request is one `send`.
//! * Every response carries `Connection: close`. The runner caps the
//!   container at 4096 FDs while k6 holds one keep-alive connection per VU
//!   (up to 30 000 VU), so idle keep-alive connections would exhaust FDs
//!   and fail the level. Closing after each response keeps FD usage at the
//!   number of in-flight requests (tens).

use rusqlite::{params, Connection, OptionalExtension};
use std::collections::VecDeque;
use std::io;
use std::os::unix::io::RawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{SystemTime, UNIX_EPOCH};

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Baseline body limit from the contract: oversized bodies get a JSON 400.
const MAX_BODY: usize = 16 * 1024;
/// Max request head (request line + headers) we will buffer.
const MAX_HEAD: usize = 16 * 1024;
/// Max commands coalesced into one SQLite transaction (group commit).
const MAX_BATCH: usize = 256;
/// Inline per-connection read buffer; larger requests spill to a heap Vec.
const INLINE_BUF: usize = 2048;
/// Max accepts per epoll wakeup (bounds accept starvation of other FDs).
const MAX_ACCEPT_PER_WAKE: usize = 512;
/// Max epoll events processed per `epoll_wait` return.
const MAX_EVENTS: usize = 1024;

/// One post as stored, served, and serialized.
#[derive(Clone)]
struct Post {
    id: i64,
    author: String,
    content: String,
    created_at: String,
    likes: i64,
}

// ---------------------------------------------------------------------------
// Validation (mirrors the starter's Python semantics exactly)
// ---------------------------------------------------------------------------

/// ASCII `[a-zA-Z0-9_]{1,32}`.
fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// Python `str.isspace()`: Unicode White_Space plus U+001C..U+001F, which
/// Rust's `char::is_whitespace` does not cover.
fn is_blank_char(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// 1..=280 Unicode characters (code points, like Python `len`), not blank
/// after trim. Original whitespace is preserved by the caller.
fn valid_content(value: &str) -> bool {
    let mut chars = 0usize;
    let mut blank = true;
    for c in value.chars() {
        chars += 1;
        if chars > 280 {
            return false;
        }
        if !is_blank_char(c) {
            blank = false;
        }
    }
    chars >= 1 && !blank
}

/// Positive signed 64-bit integer. serde_json parses JSON `1.5`, `1.0`,
/// `1e2`, `"1"`, `true`, `2**63` and `2**64` as f64/u64/bool/string, so
/// `as_i64` rejects all of them exactly like the starter's `type(x) is int`.
fn valid_post_id(value: &serde_json::Value) -> Option<i64> {
    value.as_i64().filter(|n| *n >= 1)
}

// ---------------------------------------------------------------------------
// Timestamps: UTC with milliseconds, ending in `Z` (no chrono dependency)
// ---------------------------------------------------------------------------

/// Days-since-epoch to civil date (Howard Hinnant's algorithm).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn format_created_at(millis: i64) -> String {
    let secs = millis.div_euclid(1000);
    let ms = millis.rem_euclid(1000);
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    let (y, mo, d) = civil_from_days(days);
    let (h, mi, s) = (tod / 3600, (tod % 3600) / 60, tod % 60);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{ms:03}Z")
}

fn now_created_at() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    format_created_at(millis)
}

// ---------------------------------------------------------------------------
// Hand-rolled JSON + HTTP responses (no serde per request on this path)
// ---------------------------------------------------------------------------

fn push_usize(out: &mut Vec<u8>, mut n: usize) {
    let mut tmp = [0u8; 20];
    let mut i = tmp.len();
    if n == 0 {
        out.push(b'0');
        return;
    }
    while n > 0 {
        i -= 1;
        tmp[i] = b'0' + (n % 10) as u8;
        n /= 10;
    }
    out.extend_from_slice(&tmp[i..]);
}

fn push_i64(out: &mut Vec<u8>, n: i64) {
    if n < 0 {
        out.push(b'-');
        push_usize(out, n.unsigned_abs() as usize);
    } else {
        push_usize(out, n as usize);
    }
}

/// JSON string escaping that round-trips through any JSON parser. Byte-wise
/// is safe for UTF-8: every byte of a multi-byte sequence is >= 0x80, so no
/// byte can be mistaken for an ASCII special.
fn escape_json_into(out: &mut Vec<u8>, s: &str) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    out.push(b'"');
    for &b in s.as_bytes() {
        match b {
            b'"' => out.extend_from_slice(b"\\\""),
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            b'\t' => out.extend_from_slice(b"\\t"),
            0x08 => out.extend_from_slice(b"\\b"),
            0x0c => out.extend_from_slice(b"\\f"),
            0x00..=0x1f => {
                out.extend_from_slice(b"\\u00");
                out.push(HEX[(b >> 4) as usize]);
                out.push(HEX[(b & 0xf) as usize]);
            }
            _ => out.push(b),
        }
    }
    out.push(b'"');
}

fn append_post_json(out: &mut Vec<u8>, p: &Post) {
    out.extend_from_slice(b"{\"id\":");
    push_i64(out, p.id);
    out.extend_from_slice(b",\"author\":");
    escape_json_into(out, &p.author);
    out.extend_from_slice(b",\"content\":");
    escape_json_into(out, &p.content);
    out.extend_from_slice(b",\"created_at\":");
    escape_json_into(out, &p.created_at);
    out.extend_from_slice(b",\"likes\":");
    push_i64(out, p.likes);
    out.push(b'}');
}

/// `HTTP/1.1 <code> <text>\r\ncontent-type: application/json\r\n
/// content-length: <n>\r\nconnection: close\r\n\r\n<body>`
fn wrap_response(code: u16, text: &str, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 96);
    out.extend_from_slice(b"HTTP/1.1 ");
    push_usize(&mut out, code as usize);
    out.push(b' ');
    out.extend_from_slice(text.as_bytes());
    out.extend_from_slice(b"\r\ncontent-type: application/json\r\ncontent-length: ");
    push_usize(&mut out, body.len());
    out.extend_from_slice(b"\r\nconnection: close\r\n\r\n");
    out.extend_from_slice(body);
    out
}

fn err_body(msg: &str) -> Vec<u8> {
    // Messages are static ASCII without quotes/backslashes; no escaping needed.
    let mut b = Vec::with_capacity(msg.len() + 11);
    b.extend_from_slice(b"{\"error\":\"");
    b.extend_from_slice(msg.as_bytes());
    b.extend_from_slice(b"\"}");
    b
}

fn err_response(code: u16, text: &str, msg: &str) -> Vec<u8> {
    wrap_response(code, text, &err_body(msg))
}

fn post_response(p: &Post) -> Vec<u8> {
    let mut body = Vec::with_capacity(64 + p.author.len() + p.content.len());
    append_post_json(&mut body, p);
    wrap_response(201, "Created", &body)
}

fn like_response(post_id: i64, likes: i64) -> Vec<u8> {
    let mut body = Vec::with_capacity(48);
    body.extend_from_slice(b"{\"post_id\":");
    push_i64(&mut body, post_id);
    body.extend_from_slice(b",\"likes\":");
    push_i64(&mut body, likes);
    body.extend_from_slice(b",\"liked\":true}");
    wrap_response(200, "OK", &body)
}

/// Pre-rendered complete feed response (headers + body). Rebuilt by the
/// writer only when a committed batch changes the top 20.
fn render_feed_response(top: &VecDeque<Post>) -> Arc<[u8]> {
    let mut body = Vec::with_capacity(4096);
    body.extend_from_slice(b"{\"posts\":[");
    for (i, p) in top.iter().enumerate() {
        if i > 0 {
            body.push(b',');
        }
        append_post_json(&mut body, p);
    }
    body.extend_from_slice(b"]}");
    Arc::from(wrap_response(200, "OK", &body))
}

/// Pre-rendered health response: `{"status":"ok"}` is 15 bytes.
fn health_response() -> &'static [u8] {
    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 15\r\nconnection: close\r\n\r\n{\"status\":\"ok\"}"
}

// ---------------------------------------------------------------------------
// SQLite + writer thread
// ---------------------------------------------------------------------------

enum Cmd {
    Create {
        conn: u64,
        author: String,
        content: String,
        created_at: String,
    },
    Like {
        conn: u64,
        post_id: i64,
        user: String,
    },
}

enum Reply {
    Created(Post),
    Liked { post_id: i64, likes: i64 },
    LikeNotFound,
    DbError,
}

/// One message per committed batch: all replies plus the new feed response
/// (if the top 20 changed). The net thread applies the feed update before
/// sending the replies, so reads never lag behind acknowledged writes.
struct BatchReply {
    replies: Vec<(u64, Reply)>,
    feed: Option<Arc<[u8]>>,
}

fn open_db(path: &str) -> Result<Connection, rusqlite::Error> {
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA temp_store=MEMORY;
         PRAGMA cache_size=-8000;
         PRAGMA journal_size_limit=16777216;
         CREATE TABLE IF NOT EXISTS posts (
             id INTEGER PRIMARY KEY,
             author TEXT NOT NULL,
             content TEXT NOT NULL,
             created_at TEXT NOT NULL,
             like_count INTEGER NOT NULL DEFAULT 0
         );
         CREATE TABLE IF NOT EXISTS likes (
             post_id INTEGER NOT NULL,
             user TEXT NOT NULL,
             PRIMARY KEY (post_id, user)
         ) WITHOUT ROWID;",
    )?;
    // WAL + NORMAL is SIGKILL-safe (the runner kills the container, not the
    // host). Warn loudly if WAL could not be enabled, but stay correct.
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap_or_default();
    if mode != "wal" {
        eprintln!("warning: journal_mode is {mode:?}, expected \"wal\"");
    }
    Ok(conn)
}

/// Rebuild the in-memory top 20 from SQLite after a (re)start.
fn load_top(conn: &Connection) -> Result<VecDeque<Post>, rusqlite::Error> {
    let top = {
        let mut stmt = conn.prepare(
            "SELECT id, author, content, created_at, like_count
             FROM posts ORDER BY id DESC LIMIT 20",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(Post {
                id: r.get(0)?,
                author: r.get(1)?,
                content: r.get(2)?,
                created_at: r.get(3)?,
                likes: r.get(4)?,
            })
        })?;
        let mut top = VecDeque::new();
        for row in rows {
            top.push_back(row?);
        }
        top
    };
    // Compact the WAL left behind by a killed run; we are the only connection.
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)");
    Ok(top)
}

fn insert_post(
    conn: &Connection,
    author: &str,
    content: &str,
    created_at: &str,
) -> Result<i64, rusqlite::Error> {
    let mut stmt = conn.prepare_cached(
        "INSERT INTO posts(author, content, created_at, like_count) VALUES (?1, ?2, ?3, 0)",
    )?;
    stmt.execute(params![author, content, created_at])?;
    Ok(conn.last_insert_rowid())
}

struct LikeDone {
    likes: i64,
    inserted: bool,
}

enum LikeErr {
    NotFound,
    Db,
}

fn like_post(conn: &Connection, post_id: i64, user: &str) -> Result<LikeDone, LikeErr> {
    let current: Option<i64> = conn
        .prepare_cached("SELECT like_count FROM posts WHERE id = ?1")
        .map_err(|_| LikeErr::Db)?
        .query_row(params![post_id], |r| r.get(0))
        .optional()
        .map_err(|_| LikeErr::Db)?;
    let current = match current {
        Some(c) => c,
        None => return Err(LikeErr::NotFound),
    };
    let inserted = conn
        .prepare_cached("INSERT OR IGNORE INTO likes(post_id, user) VALUES (?1, ?2)")
        .map_err(|_| LikeErr::Db)?
        .execute(params![post_id, user])
        .map_err(|_| LikeErr::Db)?;
    if inserted == 1 {
        conn.prepare_cached("UPDATE posts SET like_count = like_count + 1 WHERE id = ?1")
            .map_err(|_| LikeErr::Db)?
            .execute(params![post_id])
            .map_err(|_| LikeErr::Db)?;
        Ok(LikeDone {
            likes: current + 1,
            inserted: true,
        })
    } else {
        Ok(LikeDone {
            likes: current,
            inserted: false,
        })
    }
}

enum Outcome {
    Created(Post),
    Liked { post_id: i64, likes: i64, inserted: bool },
    LikeNotFound,
}

/// The writer owns the only SQLite connection. It drains whatever commands
/// are already queued and commits them as one transaction (group commit):
/// no added latency when idle, far fewer WAL writes under load. In-memory
/// state and replies are produced only *after* a successful commit, so an
/// acknowledged write is always durable.
fn writer_main(
    conn: Connection,
    rx: mpsc::Receiver<Cmd>,
    reply_tx: mpsc::Sender<BatchReply>,
    wake_fd: RawFd,
    mut top: VecDeque<Post>,
) {
    loop {
        let first = match rx.recv() {
            Ok(cmd) => cmd,
            Err(_) => break, // all senders gone; server is shutting down
        };
        let mut batch = Vec::with_capacity(64);
        batch.push(first);
        while batch.len() < MAX_BATCH {
            match rx.try_recv() {
                Ok(cmd) => batch.push(cmd),
                Err(_) => break,
            }
        }

        let mut outcomes: Vec<Outcome> = Vec::with_capacity(batch.len());
        let mut failed = conn.execute_batch("BEGIN IMMEDIATE").is_err();
        if !failed {
            for cmd in &batch {
                match cmd {
                    Cmd::Create {
                        author,
                        content,
                        created_at,
                        ..
                    } => match insert_post(&conn, author, content, created_at) {
                        Ok(id) => outcomes.push(Outcome::Created(Post {
                            id,
                            author: author.clone(),
                            content: content.clone(),
                            created_at: created_at.clone(),
                            likes: 0,
                        })),
                        Err(_) => {
                            failed = true;
                            break;
                        }
                    },
                    Cmd::Like { post_id, user, .. } => match like_post(&conn, *post_id, user) {
                        Ok(done) => outcomes.push(Outcome::Liked {
                            post_id: *post_id,
                            likes: done.likes,
                            inserted: done.inserted,
                        }),
                        Err(LikeErr::NotFound) => outcomes.push(Outcome::LikeNotFound),
                        Err(LikeErr::Db) => {
                            failed = true;
                            break;
                        }
                    },
                }
            }
        }
        if failed || conn.execute_batch("COMMIT").is_err() {
            let _ = conn.execute_batch("ROLLBACK");
            let replies = batch
                .into_iter()
                .map(|cmd| {
                    let id = match &cmd {
                        Cmd::Create { conn, .. } => *conn,
                        Cmd::Like { conn, .. } => *conn,
                    };
                    (id, Reply::DbError)
                })
                .collect();
            if reply_tx
                .send(BatchReply {
                    replies,
                    feed: None,
                })
                .is_err()
            {
                break;
            }
            wake(wake_fd);
            continue;
        }

        // Committed: update the in-memory top 20, re-render the feed
        // response if it changed (once per batch, and only when a like
        // actually touched a top-20 post), then acknowledge the writes.
        let mut dirty = false;
        for outcome in &outcomes {
            match outcome {
                Outcome::Created(post) => {
                    top.push_front(post.clone());
                    if top.len() > 20 {
                        top.pop_back();
                    }
                    dirty = true;
                }
                Outcome::Liked {
                    post_id,
                    likes,
                    inserted,
                } => {
                    if *inserted {
                        if let Some(p) = top.iter_mut().find(|p| p.id == *post_id) {
                            p.likes = *likes;
                            dirty = true;
                        }
                    }
                }
                Outcome::LikeNotFound => {}
            }
        }
        let feed = dirty.then(|| render_feed_response(&top));
        let replies = batch
            .into_iter()
            .zip(outcomes.into_iter())
            .map(|(cmd, outcome)| {
                let id = match &cmd {
                    Cmd::Create { conn, .. } => *conn,
                    Cmd::Like { conn, .. } => *conn,
                };
                let reply = match outcome {
                    Outcome::Created(post) => Reply::Created(post),
                    Outcome::Liked { post_id, likes, .. } => Reply::Liked { post_id, likes },
                    Outcome::LikeNotFound => Reply::LikeNotFound,
                };
                (id, reply)
            })
            .collect();
        if reply_tx.send(BatchReply { replies, feed }).is_err() {
            break;
        }
        wake(wake_fd);
    }
}

/// Set by the SIGTERM/SIGINT handler; the net loop exits cleanly so
/// `main` returns (and the LLVM profile runtime, if any, flushes).
static SHUTDOWN: AtomicBool = AtomicBool::new(false);

extern "C" fn on_term_signal(_sig: libc::c_int) {
    SHUTDOWN.store(true, Ordering::Relaxed);
}

fn install_signal_handlers() {
    unsafe {
        // SIGPIPE: never die when a client vanishes mid-write (we also use
        // MSG_NOSIGNAL on every send).
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        // SIGTERM/SIGINT: graceful shutdown (the participant guide asks
        // the server to shut down cleanly on SIGTERM). No SA_RESTART, so
        // the blocking epoll_wait returns EINTR and the loop sees the flag.
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = on_term_signal as extern "C" fn(libc::c_int) as usize;
        action.sa_flags = 0;
        libc::sigemptyset(&mut action.sa_mask);
        libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut());
        libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut());
    }
}

/// Wake the net thread's epoll loop via the eventfd (one 8-byte write).
fn wake(fd: RawFd) {
    let one: u64 = 1;
    unsafe {
        libc::write(
            fd,
            &one as *const u64 as *const libc::c_void,
            std::mem::size_of::<u64>(),
        );
    }
}

// ---------------------------------------------------------------------------
// HTTP/1.1 request parser (incremental, over the connection's buffer)
// ---------------------------------------------------------------------------

struct Request<'a> {
    method: &'a [u8],
    /// Path with any query string stripped (and absolute-form reduced).
    path: &'a [u8],
    /// `application/json` or `*+json` after `;` strip + lowercase.
    content_type_ok: bool,
    body: &'a [u8],
}

enum ParseResult<'a> {
    Complete(Request<'a>),
    NeedMore,
    /// Respond 400 with this message, then close.
    Bad(&'static str),
}

fn find_subslice(hay: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || hay.len() < needle.len() {
        return None;
    }
    hay.windows(needle.len()).position(|w| w == needle)
}

fn parse_request(buf: &[u8]) -> ParseResult<'_> {
    let head_end = match find_subslice(buf, b"\r\n\r\n") {
        Some(i) => i,
        None => {
            if buf.len() > MAX_HEAD {
                return ParseResult::Bad("headers too large");
            }
            return ParseResult::NeedMore;
        }
    };
    let head = &buf[..head_end];
    let body_start = head_end + 4;

    // Request line: METHOD SP PATH SP HTTP/x.y. A headerless request has no
    // CRLF inside `head` (the line runs up to the CRLFCRLF terminator).
    let line_end = find_subslice(head, b"\r\n").unwrap_or(head.len());
    let line = &head[..line_end];
    let headers_start = if line_end < head.len() {
        line_end + 2
    } else {
        head.len()
    };
    let mut parts = line.split(|&b| b == b' ');
    let method = parts.next().unwrap_or(b"");
    let raw_path = parts.next().unwrap_or(b"");
    let version = parts.next().unwrap_or(b"");
    if method.is_empty()
        || raw_path.is_empty()
        || !version.starts_with(b"HTTP/")
        || parts.next().is_some()
    {
        return ParseResult::Bad("bad request line");
    }
    // Strip query string; reduce absolute-form URIs to their path.
    let mut path = match raw_path.iter().position(|&b| b == b'?') {
        Some(i) => &raw_path[..i],
        None => raw_path,
    };
    if path.starts_with(b"http://") || path.starts_with(b"https://") {
        let after_scheme = if path[4] == b's' { 8 } else { 7 };
        match path[after_scheme..].iter().position(|&b| b == b'/') {
            Some(i) => path = &path[after_scheme + i..],
            None => path = b"/",
        }
    }

    // Headers: only content-length and content-type matter.
    let mut content_length: Option<usize> = None;
    let mut content_type_ok = false;
    for header in head[headers_start..].split(|&b| b == b'\n') {
        let header = header.strip_suffix(b"\r").unwrap_or(header);
        let colon = match header.iter().position(|&b| b == b':') {
            Some(i) => i,
            None => continue, // tolerate a malformed header line
        };
        let name = &header[..colon];
        let mut value = &header[colon + 1..];
        while value.first() == Some(&b' ') || value.first() == Some(&b'\t') {
            value = &value[1..];
        }
        while value.last() == Some(&b' ') || value.last() == Some(&b'\t') {
            value = &value[..value.len() - 1];
        }
        if name.eq_ignore_ascii_case(b"content-length") {
            if content_length.is_some() {
                return ParseResult::Bad("duplicate content-length");
            }
            if value.is_empty() || !value.iter().all(|b| b.is_ascii_digit()) {
                return ParseResult::Bad("bad content-length");
            }
            let mut n: usize = 0;
            for &b in value {
                n = match n
                    .checked_mul(10)
                    .and_then(|v| v.checked_add((b - b'0') as usize))
                {
                    Some(v) => v,
                    None => return ParseResult::Bad("bad content-length"),
                };
            }
            content_length = Some(n);
        } else if name.eq_ignore_ascii_case(b"content-type") {
            let mime = value.split(|&b| b == b';').next().unwrap_or(b"");
            let mime = {
                let mut m = mime;
                while m.last() == Some(&b' ') || m.last() == Some(&b'\t') {
                    m = &m[..m.len() - 1];
                }
                m
            };
            // Flask's get_json accepts `application/json` and `*+json`.
            if mime.eq_ignore_ascii_case(b"application/json")
                || (mime.len() > 5 && mime[mime.len() - 5..].eq_ignore_ascii_case(b"+json"))
            {
                content_type_ok = true;
            }
        }
    }

    let content_length = content_length.unwrap_or(0);
    if content_length > MAX_BODY {
        return ParseResult::Bad("body too large");
    }
    // GETs never need the body; POSTs wait for all of it.
    if method == b"POST" && buf.len() - body_start < content_length {
        return ParseResult::NeedMore;
    }
    let body = if method == b"POST" {
        &buf[body_start..body_start + content_length]
    } else {
        b""
    };
    ParseResult::Complete(Request {
        method,
        path,
        content_type_ok,
        body,
    })
}

// ---------------------------------------------------------------------------
// Epoll net loop
// ---------------------------------------------------------------------------

/// epoll data tags: connections use their id; the listener and eventfd use
/// reserved values that can never collide (ids have a small index on top).
const LISTENER_TAG: u64 = u64::MAX;
const WAKE_TAG: u64 = u64::MAX - 1;

fn conn_id(index: u32, gen: u32) -> u64 {
    ((index as u64) << 32) | gen as u64
}

enum OutData {
    Shared(Arc<[u8]>),
    Owned(Vec<u8>),
}

struct OutBuf {
    data: OutData,
    sent: usize,
}

impl OutBuf {
    fn remaining(&self) -> &[u8] {
        match &self.data {
            OutData::Shared(b) => &b[self.sent..],
            OutData::Owned(b) => &b[self.sent..],
        }
    }
}

struct Conn {
    fd: RawFd,
    gen: u32,
    /// Inline read buffer (covers all test traffic); `spill` takes over
    /// only for requests larger than `INLINE_BUF`.
    inbuf: [u8; INLINE_BUF],
    inlen: usize,
    spill: Option<Vec<u8>>,
    /// Response being written (None until we have one to send).
    out: Option<OutBuf>,
    /// Request dispatched to the writer; awaiting its reply.
    waiting: bool,
}

impl Conn {
    fn new(fd: RawFd, gen: u32) -> Self {
        Conn {
            fd,
            gen,
            inbuf: [0u8; INLINE_BUF],
            inlen: 0,
            spill: None,
            out: None,
            waiting: false,
        }
    }

    fn read_buf(&self) -> &[u8] {
        match &self.spill {
            Some(v) => v,
            None => &self.inbuf[..self.inlen],
        }
    }

    /// Append freshly received bytes, spilling to the heap if needed.
    /// Returns false if the request exceeds the total cap.
    fn append(&mut self, data: &[u8]) -> bool {
        if let Some(spill) = &mut self.spill {
            if spill.len() + data.len() > MAX_HEAD + MAX_BODY {
                return false;
            }
            spill.extend_from_slice(data);
            return true;
        }
        if self.inlen + data.len() <= INLINE_BUF {
            self.inbuf[self.inlen..self.inlen + data.len()].copy_from_slice(data);
            self.inlen += data.len();
            return true;
        }
        let total = self.inlen + data.len();
        if total > MAX_HEAD + MAX_BODY {
            return false;
        }
        let mut spill = Vec::with_capacity(total.max(4096));
        spill.extend_from_slice(&self.inbuf[..self.inlen]);
        spill.extend_from_slice(data);
        self.spill = Some(spill);
        self.inlen = 0;
        true
    }
}

struct Server {
    epfd: RawFd,
    listener: RawFd,
    wake_fd: RawFd,
    conns: Vec<Option<Conn>>,
    gens: Vec<u32>,
    free: Vec<u32>,
    cmd_tx: mpsc::Sender<Cmd>,
    reply_rx: mpsc::Receiver<BatchReply>,
    feed: Arc<[u8]>,
}

fn epoll_ctl(epfd: RawFd, op: i32, fd: RawFd, events: u32, data: u64) -> io::Result<()> {
    let mut ev = libc::epoll_event {
        events,
        u64: data,
    };
    let r = unsafe { libc::epoll_ctl(epfd, op, fd, &mut ev) };
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

impl Server {
    fn drop_conn(&mut self, index: u32, conn: Conn) {
        unsafe {
            libc::epoll_ctl(self.epfd, libc::EPOLL_CTL_DEL, conn.fd, std::ptr::null_mut());
            libc::close(conn.fd);
        }
        self.gens[index as usize] = self.gens[index as usize].wrapping_add(1);
        self.free.push(index);
    }

    fn close_conn(&mut self, index: u32) {
        if let Some(conn) = self.conns[index as usize].take() {
            self.drop_conn(index, conn);
        }
    }

    fn accept_batch(&mut self) {
        for _ in 0..MAX_ACCEPT_PER_WAKE {
            let fd = unsafe {
                libc::accept4(
                    self.listener,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
                )
            };
            if fd < 0 {
                return; // EAGAIN (drained) or EMFILE (back off via epoll)
            }
            // No Nagle delay on small responses.
            let one: libc::c_int = 1;
            unsafe {
                libc::setsockopt(
                    fd,
                    libc::IPPROTO_TCP,
                    libc::TCP_NODELAY,
                    &one as *const libc::c_int as *const libc::c_void,
                    std::mem::size_of::<libc::c_int>() as libc::socklen_t,
                );
            }
            let index = match self.free.pop() {
                Some(i) => i,
                None => {
                    let i = self.conns.len() as u32;
                    self.conns.push(None);
                    self.gens.push(0);
                    i
                }
            };
            let gen = self.gens[index as usize];
            if epoll_ctl(
                self.epfd,
                libc::EPOLL_CTL_ADD,
                fd,
                libc::EPOLLIN as u32,
                conn_id(index, gen),
            )
            .is_err()
            {
                unsafe { libc::close(fd) };
                self.free.push(index);
                continue;
            }
            self.conns[index as usize] = Some(Conn::new(fd, gen));
        }
    }

    /// Try to flush a connection's pending output. Returns true if the
    /// connection is fully written and should be closed.
    fn flush(&mut self, index: u32) -> bool {
        let conn = match self.conns[index as usize].as_mut() {
            Some(c) => c,
            None => return true,
        };
        let out = match conn.out.as_mut() {
            Some(o) => o,
            None => return false,
        };
        loop {
            let rem = out.remaining();
            if rem.is_empty() {
                return true;
            }
            let n = unsafe {
                libc::send(
                    conn.fd,
                    rem.as_ptr() as *const libc::c_void,
                    rem.len(),
                    libc::MSG_NOSIGNAL,
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                if err.kind() == io::ErrorKind::WouldBlock {
                    return false;
                }
                return true; // peer gone or fatal: close
            }
            if n == 0 {
                return true;
            }
            out.sent += n as usize;
        }
    }

    /// Queue a response and try to send it immediately. Closes the
    /// connection when fully sent (every response is `Connection: close`).
    fn respond(&mut self, index: u32, data: OutData) {
        match self.conns[index as usize].as_mut() {
            Some(c) => {
                c.out = Some(OutBuf { data, sent: 0 });
                c.waiting = false;
            }
            None => return,
        }
        if self.flush(index) {
            self.close_conn(index);
            return;
        }
        // Partial write: wait for writability.
        let (fd, gen) = match self.conns[index as usize].as_ref() {
            Some(c) => (c.fd, c.gen),
            None => return,
        };
        let _ = epoll_ctl(
            self.epfd,
            libc::EPOLL_CTL_MOD,
            fd,
            (libc::EPOLLIN | libc::EPOLLOUT) as u32,
            conn_id(index, gen),
        );
    }

    fn respond_err(&mut self, index: u32, code: u16, text: &str, msg: &str) {
        self.respond(index, OutData::Owned(err_response(code, text, msg)));
    }
}

/// What `dispatch` decided for a complete request. The connection itself
/// stays owned by the caller while dispatching (it borrows the request,
/// which borrows the connection buffer), so dispatch returns an action
/// instead of touching the connection.
enum Action {
    Respond(OutData),
    /// Dispatched to the writer; the reply will complete the connection.
    Wait,
}

impl Server {
    /// Route a complete request. Pure with respect to the connection: it
    /// only uses `cmd_tx` / `feed` plus the request bytes.
    fn dispatch(&mut self, id: u64, req: Request<'_>) -> Action {
        if req.method == b"GET" && req.path == b"/health" {
            let resp: Arc<[u8]> = Arc::from(health_response());
            return Action::Respond(OutData::Shared(resp));
        }
        if req.method == b"GET" && req.path == b"/feed" {
            let feed = self.feed.clone();
            return Action::Respond(OutData::Shared(feed));
        }
        if req.method == b"POST" && (req.path == b"/posts" || req.path == b"/like") {
            let is_posts = req.path == b"/posts";
            if !req.content_type_ok {
                return Action::Respond(OutData::Owned(err_response(
                    400,
                    "Bad Request",
                    "content type must be application/json",
                )));
            }
            let obj = match serde_json::from_slice::<serde_json::Value>(req.body) {
                Ok(serde_json::Value::Object(map)) => map,
                _ => {
                    return Action::Respond(OutData::Owned(err_response(
                        400,
                        "Bad Request",
                        "expected a JSON object",
                    )));
                }
            };
            if is_posts {
                let author = obj.get("author").and_then(|v| v.as_str());
                let content = obj.get("content").and_then(|v| v.as_str());
                let (author, content) = match (author, content) {
                    (Some(a), Some(c)) if valid_name(a) && valid_content(c) => (a, c),
                    _ => {
                        return Action::Respond(OutData::Owned(err_response(
                            400,
                            "Bad Request",
                            "author must match [a-zA-Z0-9_]{1,32} and content must contain 1..280 characters and not be blank",
                        )));
                    }
                };
                let cmd = Cmd::Create {
                    conn: id,
                    author: author.to_owned(),
                    content: content.to_owned(),
                    created_at: now_created_at(),
                };
                if self.cmd_tx.send(cmd).is_err() {
                    return Action::Respond(OutData::Owned(err_response(
                        500,
                        "Internal Server Error",
                        "writer unavailable",
                    )));
                }
                return Action::Wait;
            } else {
                let post_id = obj.get("post_id").and_then(valid_post_id);
                let user = obj.get("user").and_then(|v| v.as_str());
                let (post_id, user) = match (post_id, user) {
                    (Some(p), Some(u)) if valid_name(u) => (p, u),
                    _ => {
                        return Action::Respond(OutData::Owned(err_response(
                            400,
                            "Bad Request",
                            "expected a positive integer post_id and a valid user",
                        )));
                    }
                };
                let cmd = Cmd::Like {
                    conn: id,
                    post_id,
                    user: user.to_owned(),
                };
                if self.cmd_tx.send(cmd).is_err() {
                    return Action::Respond(OutData::Owned(err_response(
                        500,
                        "Internal Server Error",
                        "writer unavailable",
                    )));
                }
                return Action::Wait;
            }
        }
        Action::Respond(OutData::Owned(err_response(
            404,
            "Not Found",
            "not found",
        )))
    }

    fn on_readable(&mut self, index: u32) {
        // Take the connection out of the slab: the request borrows its
        // buffer while dispatch borrows `self`, and the two borrows must
        // not overlap. The connection goes back (or is dropped) below.
        let mut conn = match self.conns[index as usize].take() {
            Some(c) => c,
            None => return,
        };
        // Read once; loopback delivers the whole small request at once, and
        // level-triggered epoll re-fires if more arrives later.
        let mut scratch = [0u8; 4096];
        let n = unsafe {
            libc::recv(
                conn.fd,
                scratch.as_mut_ptr() as *mut libc::c_void,
                scratch.len(),
                0,
            )
        };
        if n == 0 {
            self.drop_conn(index, conn);
            return;
        }
        if n < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::WouldBlock {
                self.conns[index as usize] = Some(conn);
            } else {
                self.drop_conn(index, conn);
            }
            return;
        }
        // While awaiting a writer reply, any readable data means pipelined
        // bytes we ignore (clients here never pipeline); the reply still
        // completes the connection.
        if conn.waiting {
            self.conns[index as usize] = Some(conn);
            return;
        }
        if !conn.append(&scratch[..n as usize]) {
            self.conns[index as usize] = Some(conn);
            self.respond_err(index, 400, "Bad Request", "request too large");
            return;
        }
        let id = conn_id(index, conn.gen);
        match parse_request(conn.read_buf()) {
            ParseResult::Complete(req) => match self.dispatch(id, req) {
                Action::Respond(data) => {
                    self.conns[index as usize] = Some(conn);
                    self.respond(index, data);
                }
                Action::Wait => {
                    conn.waiting = true;
                    self.conns[index as usize] = Some(conn);
                }
            },
            ParseResult::NeedMore => {
                self.conns[index as usize] = Some(conn);
            }
            ParseResult::Bad(msg) => {
                self.conns[index as usize] = Some(conn);
                self.respond_err(index, 400, "Bad Request", msg);
            }
        }
    }

    fn on_writable(&mut self, index: u32) {
        if self.flush(index) {
            self.close_conn(index);
        }
    }

    fn drain_replies(&mut self) {
        // Drain the eventfd counter (level-triggered: must read to clear).
        let mut buf = [0u8; 8];
        unsafe {
            libc::read(
                self.wake_fd,
                buf.as_mut_ptr() as *mut libc::c_void,
                buf.len(),
            );
        }
        loop {
            let batch = match self.reply_rx.try_recv() {
                Ok(b) => b,
                Err(_) => break,
            };
            if let Some(feed) = batch.feed {
                self.feed = feed;
            }
            for (id, reply) in batch.replies {
                let index = (id >> 32) as u32;
                let gen = id as u32;
                let alive = self
                    .conns
                    .get(index as usize)
                    .and_then(|c| c.as_ref())
                    .map(|c| c.gen == gen && c.waiting)
                    .unwrap_or(false);
                if !alive {
                    continue; // peer went away; drop the reply
                }
                match reply {
                    Reply::Created(post) => {
                        self.respond(index, OutData::Owned(post_response(&post)));
                    }
                    Reply::Liked { post_id, likes } => {
                        self.respond(index, OutData::Owned(like_response(post_id, likes)));
                    }
                    Reply::LikeNotFound => {
                        self.respond_err(index, 404, "Not Found", "post not found");
                    }
                    Reply::DbError => {
                        self.respond_err(index, 500, "Internal Server Error", "database error");
                    }
                }
            }
        }
    }

    fn run(&mut self) {
        let mut events = vec![libc::epoll_event { events: 0, u64: 0 }; MAX_EVENTS];
        loop {
            if SHUTDOWN.load(Ordering::Relaxed) {
                break;
            }
            // 1 s timeout bounds shutdown latency; the wakeup costs one
            // syscall per idle second and nothing under load.
            let n = unsafe {
                libc::epoll_wait(
                    self.epfd,
                    events.as_mut_ptr(),
                    events.len() as libc::c_int,
                    1000,
                )
            };
            if n == 0 {
                continue; // timeout: re-check the shutdown flag
            }
            if n < 0 {
                let err = io::Error::last_os_error();
                if err.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                eprintln!("epoll_wait failed: {err}");
                std::thread::sleep(std::time::Duration::from_millis(10));
                continue;
            }
            for ev in &events[..n as usize] {
                let tag = ev.u64;
                let e = ev.events;
                if tag == LISTENER_TAG {
                    if e & (libc::EPOLLIN as u32) != 0 {
                        self.accept_batch();
                    }
                    continue;
                }
                if tag == WAKE_TAG {
                    self.drain_replies();
                    continue;
                }
                let index = (tag >> 32) as u32;
                // Stale event for a recycled slot: generation mismatch.
                let gen_ok = self
                    .conns
                    .get(index as usize)
                    .and_then(|c| c.as_ref())
                    .map(|c| c.gen == tag as u32)
                    .unwrap_or(false);
                if !gen_ok {
                    continue;
                }
                if e & ((libc::EPOLLERR | libc::EPOLLHUP) as u32) != 0
                    && e & ((libc::EPOLLIN | libc::EPOLLOUT) as u32) == 0
                {
                    self.close_conn(index);
                    continue;
                }
                if e & (libc::EPOLLIN as u32) != 0 {
                    self.on_readable(index);
                    // The connection may have closed inside on_readable.
                    if self
                        .conns
                        .get(index as usize)
                        .and_then(|c| c.as_ref())
                        .is_none()
                    {
                        continue;
                    }
                }
                if e & (libc::EPOLLOUT as u32) != 0 {
                    self.on_writable(index);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Listener setup + main
// ---------------------------------------------------------------------------

fn make_listener(port: u16) -> io::Result<RawFd> {
    let fd = unsafe {
        libc::socket(
            libc::AF_INET,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let one: libc::c_int = 1;
    unsafe {
        libc::setsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_REUSEADDR,
            &one as *const libc::c_int as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        );
        // Accept only wakes once the client has sent data: the first recv
        // then never misses, and half-open SYNs don't consume backlog.
        let defer: libc::c_int = 1;
        libc::setsockopt(
            fd,
            libc::IPPROTO_TCP,
            libc::TCP_DEFER_ACCEPT,
            &defer as *const libc::c_int as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        );
    }
    let addr = libc::sockaddr_in {
        sin_family: libc::AF_INET as libc::sa_family_t,
        sin_port: port.to_be(),
        sin_addr: libc::in_addr { s_addr: 0 }, // 0.0.0.0
        sin_zero: [0; 8],
    };
    let r = unsafe {
        libc::bind(
            fd,
            &addr as *const libc::sockaddr_in as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
        )
    };
    if r < 0 {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(e);
    }
    // Deep backlog: connection-per-request bursts during k6 ramps must not
    // drop SYNs (a retransmit costs ~1 s and would blow p99).
    let r = unsafe { libc::listen(fd, 4096) };
    if r < 0 {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(e);
    }
    Ok(fd)
}

fn main() {
    install_signal_handlers();

    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8080);
    let data_dir = std::env::var("DATA_DIR").unwrap_or_else(|_| "/data".to_string());
    let _ = std::fs::create_dir_all(&data_dir);
    let db_path = format!("{}/social.sqlite3", data_dir.trim_end_matches('/'));

    let conn = match open_db(&db_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("failed to open SQLite at {db_path}: {e}");
            std::process::exit(1);
        }
    };
    let top = match load_top(&conn) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("failed to rebuild feed cache: {e}");
            std::process::exit(1);
        }
    };
    let feed = render_feed_response(&top);

    let listener = match make_listener(port) {
        Ok(fd) => fd,
        Err(e) => {
            eprintln!("failed to listen on 0.0.0.0:{port}: {e}");
            std::process::exit(1);
        }
    };
    let epfd = unsafe { libc::epoll_create1(libc::EPOLL_CLOEXEC) };
    if epfd < 0 {
        eprintln!("epoll_create1 failed: {}", io::Error::last_os_error());
        std::process::exit(1);
    }
    let wake_fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
    if wake_fd < 0 {
        eprintln!("eventfd failed: {}", io::Error::last_os_error());
        std::process::exit(1);
    }
    if let Err(e) = epoll_ctl(
        epfd,
        libc::EPOLL_CTL_ADD,
        listener,
        libc::EPOLLIN as u32,
        LISTENER_TAG,
    ) {
        eprintln!("epoll add listener failed: {e}");
        std::process::exit(1);
    }
    if let Err(e) = epoll_ctl(
        epfd,
        libc::EPOLL_CTL_ADD,
        wake_fd,
        libc::EPOLLIN as u32,
        WAKE_TAG,
    ) {
        eprintln!("epoll add eventfd failed: {e}");
        std::process::exit(1);
    }

    let (cmd_tx, cmd_rx) = mpsc::channel();
    let (reply_tx, reply_rx) = mpsc::channel();
    std::thread::spawn(move || writer_main(conn, cmd_rx, reply_tx, wake_fd, top));

    eprintln!("vibe-zone listening on 0.0.0.0:{port}");
    let mut server = Server {
        epfd,
        listener,
        wake_fd,
        conns: Vec::new(),
        gens: Vec::new(),
        free: Vec::new(),
        cmd_tx,
        reply_rx,
        feed,
    };
    server.run();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    #[test]
    fn timestamps_match_known_dates() {
        assert_eq!(format_created_at(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(format_created_at(1), "1970-01-01T00:00:00.001Z");
        assert_eq!(format_created_at(999), "1970-01-01T00:00:00.999Z");
        assert_eq!(format_created_at(1_000), "1970-01-01T00:00:01.000Z");
        // 2026-11-21T10:31:07.123Z (the example in docs/API.md)
        assert_eq!(format_created_at(1_795_257_067_123), "2026-11-21T10:31:07.123Z");
        // Leap day, year boundary, far future
        assert_eq!(format_created_at(1_709_164_800_000), "2024-02-29T00:00:00.000Z");
        assert_eq!(format_created_at(1_893_456_000_000), "2030-01-01T00:00:00.000Z");
        assert_eq!(format_created_at(4_102_358_400_000), "2099-12-31T00:00:00.000Z");
    }

    #[test]
    fn names() {
        assert!(valid_name("a"));
        assert!(valid_name("user_42"));
        assert!(valid_name("A"));
        assert!(valid_name("a".repeat(32).as_str()));
        assert!(!valid_name(""));
        assert!(!valid_name("a".repeat(33).as_str()));
        assert!(!valid_name("a-b"));
        assert!(!valid_name(" a"));
        assert!(!valid_name("a\n"));
        assert!(!valid_name("ż"));
        assert!(!valid_name("a.b"));
    }

    #[test]
    fn contents() {
        assert!(valid_content("x"));
        assert!(valid_content("x".repeat(280).as_str()));
        assert!(valid_content("🦆".repeat(280).as_str()));
        assert!(valid_content("  Zażółć gęślą jaźń 🚀  "));
        assert!(valid_content("a\n'b\"<script>"));
        assert!(!valid_content(""));
        assert!(!valid_content(" \n\t"));
        assert!(!valid_content("\u{a0}")); // NBSP is whitespace
        assert!(!valid_content("\u{1c}")); // Python strip() also strips FS
        assert!(!valid_content("x".repeat(281).as_str()));
        assert!(!valid_content("🦆".repeat(281).as_str()));
    }

    #[test]
    fn post_ids() {
        let good = serde_json::json!(1);
        assert_eq!(valid_post_id(&good), Some(1));
        let max = serde_json::json!(9223372036854775807i64);
        assert_eq!(valid_post_id(&max), Some(9223372036854775807));
        for bad in [
            serde_json::json!(0),
            serde_json::json!(-1),
            serde_json::json!(true),
            serde_json::json!(1.5),
            serde_json::json!(1.0),
            serde_json::json!("1"),
            serde_json::json!(null),
            serde_json::json!(9223372036854775808u64),
            serde_json::json!(18446744073709551615u64),
        ] {
            assert_eq!(valid_post_id(&bad), None, "should reject {bad}");
        }
    }

    #[test]
    fn json_escaper_round_trips() {
        // Every byte value 0x00..=0xFF must survive a parse round-trip.
        let mut s = String::new();
        for b in 0u8..=0x7f {
            s.push(b as char);
        }
        s.push_str("Zażółć gęślą jaźń 🚀 \u{1c}\u{1f} \"\\/\n\t");
        let mut out = Vec::new();
        escape_json_into(&mut out, &s);
        let parsed: String = serde_json::from_slice(&out).expect("escaped output must parse");
        assert_eq!(parsed, s);
        // Spot-check serde_json-compatible spellings.
        let mut out = Vec::new();
        escape_json_into(&mut out, "a\n'b\"<script>");
        assert_eq!(out, b"\"a\\n'b\\\"<script>\"");
    }

    #[test]
    fn feed_response_parses_and_matches_posts() {
        let mut top = VecDeque::new();
        top.push_front(Post {
            id: 2,
            author: "bob".into(),
            content: "hi \"there\"\n".into(),
            created_at: "2026-11-21T10:31:07.123Z".into(),
            likes: 3,
        });
        top.push_front(Post {
            id: 3,
            author: "ana".into(),
            content: "🦆".into(),
            created_at: "2026-11-21T10:31:08.000Z".into(),
            likes: 0,
        });
        let resp = render_feed_response(&top);
        let text = std::str::from_utf8(&resp).unwrap();
        assert!(text.starts_with("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"));
        let body = text.split("\r\n\r\n").nth(1).unwrap();
        let v: serde_json::Value = serde_json::from_str(body).unwrap();
        assert_eq!(v["posts"][0]["id"], 3);
        assert_eq!(v["posts"][1]["content"], "hi \"there\"\n");
        assert_eq!(v["posts"][1]["likes"], 3);
        // Content-Length must match the body exactly.
        let len_line = text
            .lines()
            .find(|l| l.starts_with("content-length:"))
            .unwrap();
        assert_eq!(
            len_line["content-length: ".len()..].parse::<usize>().unwrap(),
            body.len()
        );
    }

    #[test]
    fn http_parser_cases() {
        // Simple GET.
        let req = b"GET /feed HTTP/1.1\r\nhost: x\r\n\r\n";
        match parse_request(req) {
            ParseResult::Complete(r) => {
                assert_eq!(r.method, b"GET");
                assert_eq!(r.path, b"/feed");
            }
            _ => panic!("GET should parse"),
        }
        // Query string stripped; absolute-form reduced.
        match parse_request(b"GET /feed?x=1&y=2 HTTP/1.1\r\n\r\n") {
            ParseResult::Complete(r) => assert_eq!(r.path, b"/feed"),
            _ => panic!("query should strip"),
        }
        match parse_request(b"GET http://h:8080/like HTTP/1.1\r\n\r\n") {
            ParseResult::Complete(r) => assert_eq!(r.path, b"/like"),
            _ => panic!("absolute-form should reduce"),
        }
        // POST with JSON body, mixed-case headers, charset suffix.
        let body = b"{\"a\":1}";
        let mut req = b"POST /posts HTTP/1.1\r\nContent-Type: Application/JSON; charset=utf-8\r\nCONTENT-LENGTH: 7\r\n\r\n".to_vec();
        req.extend_from_slice(body);
        match parse_request(&req) {
            ParseResult::Complete(r) => {
                assert_eq!(r.method, b"POST");
                assert_eq!(r.path, b"/posts");
                assert!(r.content_type_ok);
                assert_eq!(r.body, body);
            }
            _ => panic!("POST should parse"),
        }
        // +json suffix accepted; text/plain rejected.
        match parse_request(b"POST /like HTTP/1.1\r\ncontent-type: application/vnd.api+json\r\ncontent-length: 2\r\n\r\n{}") {
            ParseResult::Complete(r) => assert!(r.content_type_ok),
            _ => panic!("+json should be accepted"),
        }
        match parse_request(b"POST /like HTTP/1.1\r\ncontent-type: text/plain\r\ncontent-length: 2\r\n\r\n{}") {
            ParseResult::Complete(r) => assert!(!r.content_type_ok),
            _ => panic!("text/plain should parse (but flag false)"),
        }
        // Incomplete body -> NeedMore; oversized -> Bad.
        match parse_request(b"POST /posts HTTP/1.1\r\ncontent-length: 10\r\n\r\nabc") {
            ParseResult::NeedMore => {}
            _ => panic!("short body should be NeedMore"),
        }
        match parse_request(b"POST /posts HTTP/1.1\r\ncontent-length: 20000\r\n\r\n") {
            ParseResult::Bad(_) => {}
            _ => panic!("oversized body should be Bad"),
        }
        match parse_request(b"POST /posts HTTP/1.1\r\ncontent-length: abc\r\n\r\n") {
            ParseResult::Bad(_) => {}
            _ => panic!("bad content-length should be Bad"),
        }
        // Bad request line.
        match parse_request(b"GARBAGE\r\n\r\n") {
            ParseResult::Bad(_) => {}
            _ => panic!("garbage should be Bad"),
        }
    }

    static TEST_DB_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temp_db() -> (Connection, std::path::PathBuf) {
        let n = TEST_DB_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("vibe-zone-test-{n}.sqlite3"));
        let _ = std::fs::remove_file(&path);
        let conn = open_db(path.to_str().unwrap()).expect("open test db");
        (conn, path)
    }

    /// Drive the writer through the real channel/eventfd path and collect
    /// replies, like the net thread does.
    fn drive_writer(
        conn: Connection,
        top: VecDeque<Post>,
        cmds: Vec<Cmd>,
    ) -> (Vec<(u64, Reply)>, Option<Arc<[u8]>>) {
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let (reply_tx, reply_rx) = mpsc::channel();
        let wake_fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        assert!(wake_fd >= 0);
        std::thread::spawn(move || writer_main(conn, cmd_rx, reply_tx, wake_fd, top));
        let n = cmds.len();
        for cmd in cmds {
            cmd_tx.send(cmd).unwrap();
        }
        let mut replies = Vec::new();
        let mut feed = None;
        while replies.len() < n {
            let batch = reply_rx.recv().expect("writer reply");
            if batch.feed.is_some() {
                feed = batch.feed;
            }
            replies.extend(batch.replies);
        }
        drop(cmd_tx);
        unsafe { libc::close(wake_fd) };
        (replies, feed)
    }

    #[test]
    fn writer_round_trip_and_feed_cache() {
        let (conn, path) = temp_db();
        let top = load_top(&conn).expect("load top");
        assert!(top.is_empty());

        // Create 25 posts; feed must keep the newest 20.
        let mut cmds = Vec::new();
        for i in 0..25u64 {
            cmds.push(Cmd::Create {
                conn: i + 1,
                author: format!("u_{i}"),
                content: format!("post {i}"),
                created_at: format_created_at(1_795_257_067_123 + i as i64),
            });
        }
        // Like the newest post twice (idempotent) + once by another user.
        for (j, user) in ["alice", "alice", "bob"].iter().enumerate() {
            cmds.push(Cmd::Like {
                conn: 100 + j as u64,
                post_id: 25,
                user: user.to_string(),
            });
        }
        // Like a missing post -> 404 path.
        cmds.push(Cmd::Like {
            conn: 200,
            post_id: 999,
            user: "alice".to_string(),
        });
        let (replies, feed) = drive_writer(conn, top, cmds);
        assert_eq!(replies.len(), 29);
        for (id, reply) in &replies {
            match reply {
                Reply::Created(p) => assert_eq!(p.id, *id as i64),
                Reply::Liked { likes, .. } => assert!(*likes == 1 || *likes == 2),
                Reply::LikeNotFound => assert_eq!(*id, 200),
                Reply::DbError => panic!("unexpected DbError"),
            }
        }
        let feed = feed.expect("feed should have been re-rendered");
        let text = std::str::from_utf8(&feed).unwrap();
        let body = text.split("\r\n\r\n").nth(1).unwrap();
        let v: serde_json::Value = serde_json::from_str(body).unwrap();
        let posts = v["posts"].as_array().unwrap();
        assert_eq!(posts.len(), 20);
        assert_eq!(posts[0]["id"], 25);
        assert_eq!(posts[0]["likes"], 2);
        assert_eq!(posts[19]["id"], 6);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(path.with_extension("sqlite3-wal"));
        let _ = std::fs::remove_file(path.with_extension("sqlite3-shm"));
    }
}
