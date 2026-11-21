# Mini social API / contract

Port `$PORT` (8080); persistence / trwałość: SQLite in `$DATA_DIR` (`/data`). JSON, UTF-8, `Content-Type: application/json`. Errors / błędy: `{"error":"description"}`. Invalid body / złe body: **400**. Missing post / brak posta: **404**.

| Endpoint | Input | Success |
| --- | --- | --- |
| `GET /health` | - | 200 `{"status":"ok"}` |
| `POST /posts` | `{"author":"user_42","content":"Hello DevFest"}` | 201 post object below |
| `POST /like` | `{"post_id":1,"user":"user_7"}` | 200 `{"post_id":1,"likes":1,"liked":true}` |
| `GET /feed` | - | 200 `{"posts":[...]}` |

```json
{
  "id": 1,
  "author": "user_42",
  "content": "Hello DevFest",
  "created_at": "2026-11-21T10:31:07.123Z",
  "likes": 0
}
```

- `author`, `user`: ASCII `[a-zA-Z0-9_]{1,32}`.
- `content`: 1-280 Unicode characters (not bytes); must not be blank after trim. Original whitespace is preserved. / Liczymy znaki, nie bajty; zachowujemy oryginalną treść.
- `id`: server-assigned integer, starts at 1 on an empty database and strictly increases. / Nadawany przez serwer, rosnący od 1.
- `post_id`: positive signed 64-bit integer; JSON booleans, strings, fractions, zero and negative values are invalid.
- `created_at`: UTC timestamp with milliseconds, ending in `Z`.
- Likes are idempotent per `(post_id, user)`, including concurrent requests. Repeating a like returns 200 with an unchanged count. Authors may like their own posts. No unlike.
- Feed contains the newest **at most 20** posts ordered by **descending ID**, with current counts of unique likes. Empty database: `{"posts":[]}`.
- After a successful write, the next read sees it. Each confirmed 201/200 survives a container restart, including SIGKILL. / Read-after-write i trwałość po awarii są obowiązkowe.

Extra JSON keys are ignored. Invalid JSON, wrong content type and non-object JSON bodies return 400. The baseline limits request bodies to 16 KiB (excess returns JSON 400).
