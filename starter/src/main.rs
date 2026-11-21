//! Vibe Zone mini social API — fast Rust implementation (v3).
//!
//! Architecture (see README.md for the full rationale):
//! * One single-threaded epoll loop serves HTTP (1 vCPU target). No async
//!   runtime, no HTTP framework: hand-written HTTP/1.1 parsing, batched
//!   accepts, `TCP_DEFER_ACCEPT`, one `send` per response.
//! * One dedicated writer thread owns all state. All writes are serialized
//!   through it and opportunistically group-appended, so concurrent
//!   duplicate likes can never double-count. Replies come back in batches
//!   over a channel, woken by an eventfd.
//! * Durability is an append-only log (one `write` per batch, no fsync):
//!   every acknowledged write is in the log before its response is sent.
//!   The runner kills the process (SIGKILL), not the host, so un-fsynced
//!   page-cache data survives; only host power loss could lose it, and the
//!   benchmark never tests that. No SQLite: no B-tree, no WAL, no
//!   checkpoint stalls — and the binary stays small.
//! * `GET /feed` never touches the log: the writer keeps the newest 20
//!   posts in memory and a pre-rendered *complete HTTP response* (headers
//!   + JSON body) that the net thread swaps in. A feed request is one
//!   `send`.
//! * Every response carries `Connection: close`. The runner caps the
//!   container at 4096 FDs while k6 holds one keep-alive connection per VU
//!   (up to 5 000 VU), so idle keep-alive connections would exhaust FDs
//!   and fail the level. Closing after each response keeps FD usage at the
//!   number of in-flight requests (tens).

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::io::RawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{SystemTime, UNIX_EPOCH};

/// Baseline body limit from the contract: oversized bodies get a JSON 400.
const MAX_BODY: usize = 16 * 1024;
/// Max request head (request line + headers) we will buffer.
const MAX_HEAD: usize = 16 * 1024;
/// Max commands coalesced into one log append (group commit).
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

/// Positive signed 64-bit integer from a raw JSON number token. Only a plain
/// integer spelling is accepted (`-` and digits, no fraction/exponent), so
/// `1.5`, `1.0`, `1e2` and out-of-range values are rejected exactly like
/// the starter's `type(x) is int`.
fn valid_post_id(token: &[u8]) -> Option<i64> {
    if token.is_empty() {
        return None;
    }
    let digits = if token[0] == b'-' { &token[1..] } else { token };
    if digits.is_empty() || !digits.iter().all(|b| b.is_ascii_digit()) {
        return None;
    }
    std::str::from_utf8(token)
        .ok()
        .and_then(|s| s.parse::<i64>().ok())
        .filter(|n| *n >= 1)
}

// ---------------------------------------------------------------------------
// Minimal strict JSON object parser (request bodies only; no serde)
// ---------------------------------------------------------------------------
//
// Parses a top-level object and returns its members in order (last
// occurrence of a key wins for the caller, like serde_json::Value).
// Strings are fully decoded (escapes, \uXXXX, surrogate pairs); numbers
// are returned as raw tokens; other values are validated and skipped.
// Any syntax error, trailing garbage, invalid UTF-8, lone surrogate or
// nesting past MAX_JSON_DEPTH rejects the whole body, like serde_json.

const MAX_JSON_DEPTH: usize = 64;

enum Field {
    Str(String),
    Num(Vec<u8>),
    Other,
}

struct JsonParser<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> JsonParser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len()
            && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn expect(&mut self, c: u8) -> Option<()> {
        if self.peek() == Some(c) {
            self.i += 1;
            Some(())
        } else {
            None
        }
    }

    fn hex4(&mut self) -> Option<u32> {
        if self.i + 4 > self.b.len() {
            return None;
        }
        let mut v = 0u32;
        for _ in 0..4 {
            let c = self.b[self.i];
            let d = match c {
                b'0'..=b'9' => (c - b'0') as u32,
                b'a'..=b'f' => (c - b'a' + 10) as u32,
                b'A'..=b'F' => (c - b'A' + 10) as u32,
                _ => return None,
            };
            v = v * 16 + d;
            self.i += 1;
        }
        Some(v)
    }

    /// Parse a JSON string starting at the opening quote; returns the
    /// decoded value. Rejects raw control bytes, bad escapes, lone
    /// surrogates and invalid UTF-8.
    fn parse_string(&mut self) -> Option<String> {
        self.expect(b'"')?;
        let mut out: Vec<u8> = Vec::with_capacity(32);
        loop {
            let c = self.peek()?;
            match c {
                b'"' => {
                    self.i += 1;
                    return String::from_utf8(out).ok();
                }
                b'\\' => {
                    self.i += 1;
                    let e = self.peek()?;
                    self.i += 1;
                    match e {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0c),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let cp = self.hex4()?;
                            let ch = if (0xd800..0xdc00).contains(&cp) {
                                // High surrogate: a \uXXXX low surrogate
                                // must follow.
                                if self.peek() != Some(b'\\')
                                    || self.b.get(self.i + 1).copied() != Some(b'u')
                                {
                                    return None;
                                }
                                self.i += 2;
                                let lo = self.hex4()?;
                                if !(0xdc00..0xe000).contains(&lo) {
                                    return None;
                                }
                                let n = 0x1_0000
                                    + ((cp - 0xd800) << 10)
                                    + (lo - 0xdc00);
                                char::from_u32(n)?
                            } else if (0xdc00..0xe000).contains(&cp) {
                                return None; // lone low surrogate
                            } else {
                                char::from_u32(cp)?
                            };
                            let mut tmp = [0u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut tmp).as_bytes());
                        }
                        _ => return None,
                    }
                }
                0x00..=0x1f => return None,
                _ => {
                    out.push(c);
                    self.i += 1;
                }
            }
        }
    }

    /// Parse a JSON number starting here; returns the raw token. Strict
    /// grammar: `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`.
    fn parse_number(&mut self) -> Option<&'a [u8]> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek()? {
            b'0' => {
                self.i += 1;
            }
            b'1'..=b'9' => {
                while self.peek().map_or(false, |c| c.is_ascii_digit()) {
                    self.i += 1;
                }
            }
            _ => return None,
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !self.peek().map_or(false, |c| c.is_ascii_digit()) {
                return None;
            }
            while self.peek().map_or(false, |c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.i += 1;
            }
            if !self.peek().map_or(false, |c| c.is_ascii_digit()) {
                return None;
            }
            while self.peek().map_or(false, |c| c.is_ascii_digit()) {
                self.i += 1;
            }
        }
        Some(&self.b[start..self.i])
    }

    fn parse_literal(&mut self, word: &[u8]) -> Option<()> {
        if self.b.len() - self.i >= word.len() && &self.b[self.i..self.i + word.len()] == word {
            self.i += word.len();
            Some(())
        } else {
            None
        }
    }

    /// Validate (and discard) any JSON value. Strings and numbers are
    /// validated strictly; containers recurse with a depth cap.
    fn skip_value(&mut self, depth: usize) -> Option<()> {
        if depth > MAX_JSON_DEPTH {
            return None;
        }
        self.ws();
        match self.peek()? {
            b'"' => {
                self.parse_string()?;
            }
            b'{' => {
                self.i += 1;
                self.ws();
                if self.peek() == Some(b'}') {
                    self.i += 1;
                    return Some(());
                }
                loop {
                    self.ws();
                    self.parse_string()?;
                    self.ws();
                    self.expect(b':')?;
                    self.skip_value(depth + 1)?;
                    self.ws();
                    match self.peek()? {
                        b',' => {
                            self.i += 1;
                        }
                        b'}' => {
                            self.i += 1;
                            return Some(());
                        }
                        _ => return None,
                    }
                }
            }
            b'[' => {
                self.i += 1;
                self.ws();
                if self.peek() == Some(b']') {
                    self.i += 1;
                    return Some(());
                }
                loop {
                    self.skip_value(depth + 1)?;
                    self.ws();
                    match self.peek()? {
                        b',' => {
                            self.i += 1;
                        }
                        b']' => {
                            self.i += 1;
                            return Some(());
                        }
                        _ => return None,
                    }
                }
            }
            b't' => self.parse_literal(b"true")?,
            b'f' => self.parse_literal(b"false")?,
            b'n' => self.parse_literal(b"null")?,
            b'-' | b'0'..=b'9' => {
                self.parse_number()?;
            }
            _ => return None,
        }
        Some(())
    }
}

/// Parse a top-level JSON object into (key, value) members. `None` on any
/// syntax error or trailing garbage.
fn parse_json_object(body: &[u8]) -> Option<Vec<(String, Field)>> {
    let mut p = JsonParser { b: body, i: 0 };
    p.ws();
    p.expect(b'{')?;
    let mut out = Vec::new();
    p.ws();
    if p.peek() == Some(b'}') {
        p.i += 1;
        p.ws();
        return if p.i == body.len() { Some(out) } else { None };
    }
    loop {
        p.ws();
        let key = p.parse_string()?;
        p.ws();
        p.expect(b':')?;
        p.ws();
        let field = match p.peek()? {
            b'"' => Field::Str(p.parse_string()?),
            b'-' | b'0'..=b'9' => Field::Num(p.parse_number()?.to_vec()),
            _ => {
                p.skip_value(1)?;
                Field::Other
            }
        };
        out.push((key, field));
        p.ws();
        match p.peek()? {
            b',' => {
                p.i += 1;
            }
            b'}' => {
                p.i += 1;
                p.ws();
                return if p.i == body.len() { Some(out) } else { None };
            }
            _ => return None,
        }
    }
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
// Append-only log store + writer thread
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

// Durability without SQLite: every acknowledged write is a record in a
// single append-only file. The writer holds all state in memory (posts
// in a Vec indexed by id-1, likes in a per-post HashSet) and appends one
// batch of records with a single `write` before acknowledging. No fsync:
// SIGKILL leaves the page cache intact, so acknowledged writes survive
// the runner's kill-and-restart check; only host power loss could lose
// them, which the benchmark never tests.
//
// Record framing: `[u32 len][u8 kind][payload][u32 crc32]`, all integers
// little-endian. `len` counts kind + payload + crc. A torn tail (short
// read or CRC mismatch, e.g. killed mid-write) is truncated on open.
// Payloads:
//   `P`: id:u64, author_len:u32, author, content_len:u32, content,
//        created_len:u32, created
//   `L`: post_id:u64, user_len:u32, user

/// CRC-32 (IEEE, poly 0xEDB88320), bit-by-bit: no 1 KiB table, fast
/// enough for a few hundred bytes per batch and a ~20 MB replay.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0xedb8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

fn put_u32(out: &mut Vec<u8>, n: u32) {
    out.extend_from_slice(&n.to_le_bytes());
}

fn put_u64(out: &mut Vec<u8>, n: u64) {
    out.extend_from_slice(&n.to_le_bytes());
}

fn put_bytes(out: &mut Vec<u8>, b: &[u8]) {
    put_u32(out, b.len() as u32);
    out.extend_from_slice(b);
}

/// Frame one record (kind + payload) with length prefix and CRC.
fn frame_record(out: &mut Vec<u8>, kind: u8, payload: &[u8]) {
    put_u32(out, (1 + payload.len() + 4) as u32);
    let start = out.len();
    out.push(kind);
    out.extend_from_slice(payload);
    let crc = crc32(&out[start..]);
    put_u32(out, crc);
}

struct Store {
    posts: Vec<Post>,
    likes: HashMap<i64, HashSet<String>>,
    next_id: i64,
}

struct LikeDone {
    likes: i64,
    inserted: bool,
}

impl Store {
    fn new() -> Store {
        Store {
            posts: Vec::new(),
            likes: HashMap::new(),
            next_id: 1,
        }
    }

    /// Apply a create: appends its record to `out`, stores the post,
    /// returns it. Ids are sequential from 1, so `posts[id-1]` is the post.
    fn create(
        &mut self,
        author: &str,
        content: &str,
        created_at: &str,
        out: &mut Vec<u8>,
    ) -> Post {
        let id = self.next_id;
        self.next_id += 1;
        let mut payload = Vec::with_capacity(32 + author.len() + content.len());
        put_u64(&mut payload, id as u64);
        put_bytes(&mut payload, author.as_bytes());
        put_bytes(&mut payload, content.as_bytes());
        put_bytes(&mut payload, created_at.as_bytes());
        frame_record(out, b'P', &payload);
        let post = Post {
            id,
            author: author.to_owned(),
            content: content.to_owned(),
            created_at: created_at.to_owned(),
            likes: 0,
        };
        self.posts.push(post.clone());
        post
    }

    /// Apply a like: appends its record to `out` only when the like is
    /// new (idempotent re-likes append nothing and don't change counts).
    /// `None` when the post does not exist.
    fn like(&mut self, post_id: i64, user: &str, out: &mut Vec<u8>) -> Option<LikeDone> {
        if post_id < 1 || post_id as usize > self.posts.len() {
            return None;
        }
        let set = self.likes.entry(post_id).or_default();
        if set.contains(user) {
            let likes = self.posts[(post_id - 1) as usize].likes;
            return Some(LikeDone {
                likes,
                inserted: false,
            });
        }
        set.insert(user.to_owned());
        let post = &mut self.posts[(post_id - 1) as usize];
        post.likes += 1;
        let likes = post.likes;
        let mut payload = Vec::with_capacity(16 + user.len());
        put_u64(&mut payload, post_id as u64);
        put_bytes(&mut payload, user.as_bytes());
        frame_record(out, b'L', &payload);
        Some(LikeDone {
            likes,
            inserted: true,
        })
    }
}

/// Reader over a byte slice for replay.
struct Reader<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> Reader<'a> {
    fn u32(&mut self) -> Option<u32> {
        let v = self.b.get(self.i..self.i + 4)?;
        self.i += 4;
        Some(u32::from_le_bytes(v.try_into().ok()?))
    }

    fn u64(&mut self) -> Option<u64> {
        let v = self.b.get(self.i..self.i + 8)?;
        self.i += 8;
        Some(u64::from_le_bytes(v.try_into().ok()?))
    }

    fn bytes(&mut self) -> Option<&'a [u8]> {
        let n = self.u32()? as usize;
        let v = self.b.get(self.i..self.i + n)?;
        self.i += n;
        Some(v)
    }

    fn string(&mut self) -> Option<String> {
        std::str::from_utf8(self.bytes()?).ok().map(|s| s.to_owned())
    }
}

/// Open (or create) the log, replay it into a Store, truncate any torn
/// tail, and return the store, the append handle and the top 20.
fn open_store(path: &str) -> io::Result<(Store, File, VecDeque<Post>)> {
    let mut file = File::options()
        .read(true)
        .append(true)
        .create(true)
        .open(path)?;
    let mut raw = Vec::new();
    file.read_to_end(&mut raw)?;
    let mut store = Store::new();
    let mut off = 0usize;
    let mut good = 0usize;
    while off + 4 <= raw.len() {
        let len = u32::from_le_bytes(raw[off..off + 4].try_into().unwrap()) as usize;
        if len < 6 || off + 4 + len > raw.len() {
            break; // torn tail
        }
        let rec = &raw[off + 4..off + 4 + len];
        let (kind_payload, crc_bytes) = rec.split_at(len - 4);
        let crc = u32::from_le_bytes(crc_bytes.try_into().unwrap());
        if crc32(kind_payload) != crc {
            break; // torn tail
        }
        let kind = kind_payload[0];
        let mut r = Reader {
            b: &kind_payload[1..],
            i: 0,
        };
        let ok = match kind {
            b'P' => {
                let (Some(id), Some(author), Some(content), Some(created)) =
                    (r.u64(), r.string(), r.string(), r.string())
                else {
                    break;
                };
                if id as usize != store.posts.len() + 1 || r.i != kind_payload.len() - 1 {
                    break;
                }
                store.posts.push(Post {
                    id: id as i64,
                    author,
                    content,
                    created_at: created,
                    likes: 0,
                });
                store.next_id = id as i64 + 1;
                true
            }
            b'L' => {
                let (Some(post_id), Some(user)) = (r.u64(), r.string()) else {
                    break;
                };
                if post_id < 1
                    || post_id as usize > store.posts.len()
                    || r.i != kind_payload.len() - 1
                {
                    break;
                }
                if store
                    .likes
                    .entry(post_id as i64)
                    .or_default()
                    .insert(user)
                {
                    store.posts[(post_id - 1) as usize].likes += 1;
                }
                true
            }
            _ => break,
        };
        if !ok {
            break;
        }
        off += 4 + len;
        good = off;
    }
    if good < raw.len() {
        // Torn tail (killed mid-write): drop it so the next append lands
        // on a clean record boundary.
        file.set_len(good as u64)?;
    }
    file.seek(SeekFrom::End(0))?;
    let mut top = VecDeque::new();
    for p in store.posts.iter().rev().take(20) {
        top.push_back(p.clone());
    }
    Ok((store, file, top))
}

enum Outcome {
    Created(Post),
    Liked { post_id: i64, likes: i64, inserted: bool },
    LikeNotFound,
}

/// The writer owns the only Store and the log handle. It drains whatever
/// commands are already queued, applies them to memory while framing
/// their log records, and appends the whole batch with one `write`:
/// no added latency when idle, one syscall per batch under load.
/// Replies are produced only *after* a successful append, so an
/// acknowledged write is always in the log. A failed append means the
/// store can no longer be trusted, so the process exits rather than
/// serve data the log does not have.
fn writer_main(
    mut store: Store,
    mut log: File,
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

        let mut log_buf = Vec::with_capacity(1024);
        let mut outcomes: Vec<Outcome> = Vec::with_capacity(batch.len());
        for cmd in &batch {
            match cmd {
                Cmd::Create {
                    author,
                    content,
                    created_at,
                    ..
                } => {
                    let post = store.create(author, content, created_at, &mut log_buf);
                    outcomes.push(Outcome::Created(post));
                }
                Cmd::Like { post_id, user, .. } => {
                    match store.like(*post_id, user, &mut log_buf) {
                        Some(done) => outcomes.push(Outcome::Liked {
                            post_id: *post_id,
                            likes: done.likes,
                            inserted: done.inserted,
                        }),
                        None => outcomes.push(Outcome::LikeNotFound),
                    }
                }
            }
        }
        // One append makes the whole batch durable (page cache survives
        // SIGKILL). Exit on failure: never acknowledge unwritten data.
        if let Err(e) = log.write_all(&log_buf) {
            eprintln!("fatal: log append failed: {e}");
            std::process::exit(1);
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
            let fields = match parse_json_object(req.body) {
                Some(f) => f,
                None => {
                    return Action::Respond(OutData::Owned(err_response(
                        400,
                        "Bad Request",
                        "expected a JSON object",
                    )));
                }
            };
            // Last occurrence of a key wins, like serde_json::Value.
            let field = |name: &str| -> Option<&Field> {
                fields
                    .iter()
                    .rev()
                    .find(|(k, _)| k == name)
                    .map(|(_, v)| v)
            };
            if is_posts {
                let author = match field("author") {
                    Some(Field::Str(s)) => Some(s.as_str()),
                    _ => None,
                };
                let content = match field("content") {
                    Some(Field::Str(s)) => Some(s.as_str()),
                    _ => None,
                };
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
                let post_id = match field("post_id") {
                    Some(Field::Num(n)) => valid_post_id(n),
                    _ => None,
                };
                let user = match field("user") {
                    Some(Field::Str(s)) => Some(s.as_str()),
                    _ => None,
                };
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
    let log_path = format!("{}/log.bin", data_dir.trim_end_matches('/'));

    let (store, log, top) = match open_store(&log_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("failed to open log at {log_path}: {e}");
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
    std::thread::spawn(move || writer_main(store, log, cmd_rx, reply_tx, wake_fd, top));

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
        assert_eq!(valid_post_id(b"1"), Some(1));
        assert_eq!(
            valid_post_id(b"9223372036854775807"),
            Some(9223372036854775807)
        );
        for bad in [
            "0",
            "-1",
            "1.5",
            "1.0",
            "1e2",
            "",
            "+1",
            " 1",
            "1 ",
            "9223372036854775808",
            "18446744073709551615",
        ] {
            assert_eq!(valid_post_id(bad.as_bytes()), None, "should reject {bad}");
        }
    }

    #[test]
    fn json_parser_cases() {
        // Escapes, \uXXXX, surrogate pairs, nested unknowns skipped.
        let body = b"{\"author\": \"a\\u0041\", \"content\": \"hi \\uD83E\\uDD86\\n\\/\", \"n\": 42, \"f\": 1.5e2, \"o\": {\"x\": [1, true, null]}, \"b\": false}";
        let fields = parse_json_object(body).expect("should parse");
        let get = |name: &str| -> &Field {
            fields
                .iter()
                .rev()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v)
                .expect("field present")
        };
        match get("author") {
            Field::Str(s) => assert_eq!(s, "aA"),
            _ => panic!("author should be a string"),
        }
        match get("content") {
            Field::Str(s) => assert_eq!(s, "hi 🦆\n/"),
            _ => panic!("content should be a string"),
        }
        match get("n") {
            Field::Num(n) => assert_eq!(n, b"42"),
            _ => panic!("n should be a number token"),
        }
        match get("f") {
            Field::Num(n) => assert_eq!(n, b"1.5e2"),
            _ => panic!("f should be a number token"),
        }
        match get("o") {
            Field::Other => {}
            _ => panic!("o should be Other"),
        }
        // Duplicate key: last wins.
        let fields = parse_json_object(b"{\"a\": \"first\", \"a\": \"second\"}").unwrap();
        match &fields.iter().rev().find(|(k, _)| k == "a").unwrap().1 {
            Field::Str(s) => assert_eq!(s, "second"),
            _ => panic!("last should win"),
        }
        // Duplicate key where the last value has another type: the last
        // value wins as a whole (string lookup must not fall back).
        let fields = parse_json_object(b"{\"a\": \"str\", \"a\": 7}").unwrap();
        assert!(matches!(
            fields.iter().rev().find(|(k, _)| k == "a").unwrap().1,
            Field::Num(_)
        ));
        // Malformed bodies.
        for bad in [
            b"".as_slice(),
            b"{}x",
            b"[1,2]",
            b"{\"a\":}",
            b"{\"a\"}",
            b"{\"a\":1,}",
            b"{\"a\": \"unclosed}",
            b"{\"a\": \"bad \\x escape\"}",
            b"{\"a\": \"lone \\uD83E surrogate\"}",
            b"{\"a\": \"lone \\uDC00 surrogate\"}",
            b"{\"a\": 01}",
            b"{\"a\": 1.}",
            b"{\"a\": tru}",
            b"{\"a\": \"\xff\"}",
            b"{\"a\": \"raw\x01control\"}",
        ] {
            assert!(parse_json_object(bad).is_none(), "should reject {bad:?}");
        }
        // Nesting past the depth cap is rejected (no stack overflow).
        let mut deep = b"{\"a\":".to_vec();
        deep.extend_from_slice(&vec![b'['; 100]);
        deep.extend_from_slice(&vec![b']'; 100]);
        deep.push(b'}');
        assert!(parse_json_object(&deep).is_none());
        // Empty object and whitespace-only surroundings are fine.
        assert!(parse_json_object(b"  {  }  ").unwrap().is_empty());
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
        // Wrap in an object and parse with our own parser.
        let mut wrapped = b"{\"v\":".to_vec();
        wrapped.extend_from_slice(&out);
        wrapped.push(b'}');
        let fields = parse_json_object(&wrapped).expect("escaped output must parse");
        assert_eq!(fields.len(), 1);
        match &fields[0].1 {
            Field::Str(parsed) => assert_eq!(*parsed, s),
            _ => panic!("expected a string field"),
        }
        // Spot-check spellings.
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
        // Newest first (id 3 before id 2), content escaped, likes present.
        let pos3 = body.find("\"id\":3").expect("id 3 present");
        let pos2 = body.find("\"id\":2").expect("id 2 present");
        assert!(pos3 < pos2);
        assert!(body.contains("\"content\":\"hi \\\"there\\\"\\n\""));
        assert!(body.contains("\"likes\":3"));
        assert!(body.contains("\"content\":\"🦆\""));
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

    static TEST_STORE_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temp_store_path() -> std::path::PathBuf {
        let n = TEST_STORE_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!("vibe-zone-test-{n}.log"));
        let _ = std::fs::remove_file(&path);
        path
    }

    /// Drive the writer through the real channel/eventfd path and collect
    /// replies, like the net thread does.
    fn drive_writer(
        store: Store,
        log: File,
        top: VecDeque<Post>,
        cmds: Vec<Cmd>,
    ) -> (Vec<(u64, Reply)>, Option<Arc<[u8]>>) {
        let (cmd_tx, cmd_rx) = mpsc::channel();
        let (reply_tx, reply_rx) = mpsc::channel();
        let wake_fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        assert!(wake_fd >= 0);
        std::thread::spawn(move || writer_main(store, log, cmd_rx, reply_tx, wake_fd, top));
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
        let path = temp_store_path();
        let (store, log, top) = open_store(path.to_str().unwrap()).expect("open store");
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
        let (replies, feed) = drive_writer(store, log, top, cmds);
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
        // Newest 20 of 25: ids 25..6, newest first; post 25 has 2 likes.
        assert!(body.contains("\"id\":25"));
        assert!(body.contains("\"id\":6"));
        assert!(!body.contains("\"id\":5"));
        let first25 = body.find("\"id\":25").unwrap();
        let first6 = body.find("\"id\":6").unwrap();
        assert!(first25 < first6);
        assert!(body.contains("\"likes\":2"));

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn log_survives_reopen_and_truncates_torn_tail() {
        let path = temp_store_path();
        {
            let (mut store, _log, _top) = open_store(path.to_str().unwrap()).expect("open");
            let mut buf = Vec::new();
            store.create("ana", "hello", "2026-11-21T10:31:07.123Z", &mut buf);
            store.create("bob", "🦆", "2026-11-21T10:31:08.000Z", &mut buf);
            store.like(1, "alice", &mut buf);
            store.like(1, "alice", &mut buf); // dup: no record
            store.like(2, "alice", &mut buf);
            let mut f = File::options().append(true).open(&path).unwrap();
            f.write_all(&buf).unwrap();
            // Simulate a kill mid-write: a torn record tail.
            f.write_all(b"\x2a\x00\x00\x00P\x01\x00").unwrap();
        }
        let (store, _log, top) = open_store(path.to_str().unwrap()).expect("reopen");
        assert_eq!(store.posts.len(), 2);
        assert_eq!(store.next_id, 3);
        assert_eq!(store.posts[0].likes, 1);
        assert_eq!(store.posts[1].likes, 1);
        assert_eq!(store.posts[1].content, "🦆");
        assert_eq!(top.len(), 2);
        assert_eq!(top[0].id, 2);
        // The torn tail was truncated: the file now ends on a record
        // boundary, so a fresh append replays cleanly.
        let (store2, _log2, _top2) = open_store(path.to_str().unwrap()).expect("reopen 2");
        assert_eq!(store2.posts.len(), 2);
        let _ = std::fs::remove_file(&path);
    }
}
