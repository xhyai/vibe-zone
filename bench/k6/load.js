import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const errors = new Rate('response_errors');
const latency = new Trend('hold_latency', true);
const holdErrors = new Rate('hold_errors');
const holdRequests = new Counter('hold_requests');
const semanticErrors = new Counter('semantic_errors');
const integrityCompleted = new Counter('integrity_completed');
const level = Number(__ENV.VUS || 500);
const ramp = Number(__ENV.RAMP_SECONDS || 20);
const hold = Number(__ENV.HOLD_SECONDS || 20);
const base = __ENV.BASE_URL || 'http://sut:8080';
const seedPosts = Number(__ENV.SEED_POSTS || 10000);
export const options = {
  scenarios: { ramp: { executor: 'ramping-vus', startVUs: Number(__ENV.PREV_VUS || 0),
    stages: [{ duration: `${ramp}s`, target: level }, { duration: `${hold}s`, target: level }],
    gracefulRampDown: '0s', gracefulStop: '6s' },
    integrity: { executor: 'constant-vus', vus: 1, duration: `${ramp + hold}s`, exec: 'integrity', gracefulStop: '6s' } },
  thresholds: {
    response_errors: [{ threshold: 'rate==0', abortOnFail: true }],
    hold_errors: ['rate==0'],
    hold_latency: ['p(99)<500'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  discardResponseBodies: false,
  maxRedirects: 0,
};

let seen = {};
let ownPost = null;
let lastLike = null;
function validPost(p) {
  return p && Number.isInteger(p.id) && p.id > 0 && typeof p.author === 'string'
    && /^[a-zA-Z0-9_]{1,32}$/.test(p.author) && typeof p.content === 'string'
    && [...p.content].length >= 1 && [...p.content].length <= 280 && p.content.trim().length > 0
    && typeof p.created_at === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(p.created_at)
    && Number.isInteger(p.likes) && p.likes >= 0;
}

function decoded(response) {
  // Leave room for a full feed with every Unicode character JSON-escaped.
  if (response.body && response.body.length > 131072) throw new Error('Oversized response');
  if (!/^application\/json(?:;|$)/i.test(response.headers['Content-Type'] || '')) throw new Error('Not JSON');
  return response.json();
}

// Independent randomized transactions use the same API while the main traffic runs.
// Main-load likes target seeded IDs, so these new posts have exactly one unique liker.
export function integrity() {
  const startedInHold = exec.instance.currentTestRunDuration >= ramp * 1000
    && exec.instance.currentTestRunDuration < (ramp + hold) * 1000;
  const nonce = Math.random().toString(36).slice(2) + __ITER;
  const author = `c_${nonce}`.slice(0, 32);
  const content = `${Math.random()} Zażółć 🚀 ${nonce}`;
  const params = { headers: { 'Content-Type': 'application/json' }, timeout: '5s' };
  let response;
  let ok = true;
  let semantic = false;
  try {
    response = http.post(`${base}/posts`, JSON.stringify({author, content}), params);
    if (response.status !== 201) throw new Error('HTTP');
    const post = decoded(response);
    if (!validPost(post) || post.author !== author || post.content !== content || post.likes !== 0) {
      semantic = true; throw new Error('Invalid post');
    }
    for (let i = 0; i < 2; i++) {
      response = http.post(`${base}/like`, JSON.stringify({post_id: post.id, user: author}), params);
      if (response.status !== 200) throw new Error('HTTP');
      const liked = decoded(response);
      if (liked.post_id !== post.id || liked.liked !== true || liked.likes !== 1) {
        semantic = true; throw new Error('Counter / idempotency');
      }
      response = http.get(`${base}/feed`, params);
      if (response.status !== 200) throw new Error('HTTP');
      const feed = decoded(response).posts;
      if (!Array.isArray(feed) || feed.length !== 20 || !feed.every(validPost) || feed[0].id < post.id
          || !feed.every((p, j) => j === 0 || feed[j - 1].id > p.id)) {
        semantic = true; throw new Error('Stale / invalid feed');
      }
      if (feed[19].id <= post.id && !feed.some(p => p.id === post.id && p.content === content
          && p.author === author && p.created_at === post.created_at && p.likes === 1)) {
        semantic = true; throw new Error('Read after write');
      }
    }
  } catch (_) {
    ok = false;
    // A malformed successful response is a semantic failure, not just overload.
    if (response && response.status >= 200 && response.status < 300) semantic = true;
  }
  errors.add(!ok);
  semanticErrors.add(semantic ? 1 : 0);
  integrityCompleted.add(ok && startedInHold ? 1 : 0);
  sleep(.5 + Math.random());
}

export default function () {
  const inHold = exec.instance.currentTestRunDuration >= ramp * 1000
    && exec.instance.currentTestRunDuration < (ramp + hold) * 1000;
  const choice = Math.random();
  const user = `v${__VU}_l${level}`;
  const content = `DevFest ${__VU}/${__ITER}/${Math.random()} 🚀`;
  const postId = 1 + Math.floor(Math.random() * seedPosts);
  const params = { headers: { 'Content-Type': 'application/json' }, timeout: '5s' };
  let response;
  const started = Date.now();
  if (choice < 0.7) response = http.get(`${base}/feed`, params);
  else if (choice < 0.9) response = http.post(`${base}/like`, JSON.stringify({ post_id: postId, user }), params);
  else response = http.post(`${base}/posts`, JSON.stringify({ author: user, content }), params);
  let ok = false;
  try {
    const data = decoded(response);
    ok = /^application\/json(?:;|$)/i.test(response.headers['Content-Type'] || '');
    if (choice < 0.7) {
      ok = ok && response.status === 200 && Array.isArray(data.posts) && data.posts.length === Math.min(20, seedPosts);
      if (ok) {
        const current = {};
        for (let i = 0; i < data.posts.length; i++) {
          const p = data.posts[i];
          ok = ok && validPost(p) && (i === 0 || data.posts[i - 1].id > p.id)
            && (!(p.id in seen) || p.likes >= seen[p.id]);
          current[p.id] = p.likes;
        }
        if (ownPost && (data.posts.length < 20 || data.posts[19].id <= ownPost.id)) {
          ok = ok && data.posts.some(p => p.id === ownPost.id && p.content === ownPost.content && p.author === ownPost.author);
        }
        if (lastLike) {
          const liked = data.posts.find(p => p.id === lastLike.post_id);
          if (liked) ok = ok && liked.likes >= lastLike.likes;
        }
        seen = current;
      }
    } else if (choice < 0.9) {
      ok = ok && response.status === 200 && data.post_id === postId && data.liked === true
        && Number.isInteger(data.likes) && data.likes >= 1;
      if (ok) lastLike = data;
    } else {
      ok = ok && response.status === 201 && validPost(data) && data.author === user && data.content === content && data.likes === 0;
      if (ok) ownPost = data;
    }
  } catch (_) { ok = false; }
  errors.add(!ok);
  semanticErrors.add(!ok && response.status >= 200 && response.status < 300 ? 1 : 0);
  if (inHold) {
    holdRequests.add(1);
    // Include response reception: a slow download must not improve the score.
    latency.add(Date.now() - started);
    holdErrors.add(!ok);
  }
  sleep(1 + Math.random() * 2);
}

export function handleSummary(data) {
  return { '/tmp/summary.json': JSON.stringify(data) };
}
