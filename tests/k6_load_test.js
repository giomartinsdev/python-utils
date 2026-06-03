import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE_URL = 'http://127.0.0.1:5000';

const errorRate = new Rate('errors');
const deadlockErrors = new Counter('deadlock_errors');
const nestedTxnSuccess = new Counter('nested_txn_success');
const bulkOpsSuccess = new Counter('bulk_ops_success');
const concurrentWriteSuccess = new Counter('concurrent_write_success');
const sessionCycleTime = new Trend('session_cycle_time_ms');

export const options = {
  scenarios: {
    // Scenario 1: Sustained concurrent writes to stress session pool
    concurrent_writes: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '5s', target: 30 },
        { duration: '15s', target: 50 },
        { duration: '10s', target: 100 },
        { duration: '10s', target: 100 },
        { duration: '5s', target: 0 },
      ],
      exec: 'concurrentWrites',
    },

    // Scenario 2: Rapid-fire nested transactions to test savepoint handling
    nested_transactions: {
      executor: 'constant-vus',
      vus: 20,
      duration: '30s',
      exec: 'nestedTransactions',
      startTime: '5s',
    },

    // Scenario 3: Bulk operations hammering batch flush logic
    bulk_operations: {
      executor: 'per-vu-iterations',
      vus: 15,
      iterations: 10,
      exec: 'bulkOperations',
      startTime: '5s',
    },

    // Scenario 4: Mixed read/write to provoke contention & potential deadlocks
    mixed_read_write: {
      executor: 'constant-arrival-rate',
      rate: 80,
      timeUnit: '1s',
      duration: '30s',
      preAllocatedVUs: 40,
      maxVUs: 80,
      exec: 'mixedReadWrite',
      startTime: '5s',
    },

    // Scenario 5: Pure reads under load (read-only session pool stress)
    read_storm: {
      executor: 'constant-vus',
      vus: 30,
      duration: '25s',
      exec: 'readStorm',
      startTime: '10s',
    },

    // Scenario 6: Timezone compute (no DB, baseline for comparison)
    timezone_compute: {
      executor: 'constant-vus',
      vus: 10,
      duration: '20s',
      exec: 'timezoneCompute',
      startTime: '10s',
    },
  },

  thresholds: {
    http_req_failed: ['rate<0.05'],        // <5% request failures
    http_req_duration: ['p(95)<2000'],     // p95 under 2s
    errors: ['rate<0.05'],                  // <5% app-level errors
    deadlock_errors: ['count<5'],           // near-zero deadlocks
  },
};

// ── Helpers ──

const TIMEZONES = [
  'America/Sao_Paulo',
  'America/New_York',
  'Europe/London',
  'Asia/Tokyo',
  'Australia/Sydney',
  'UTC',
];

function randomTz() {
  return TIMEZONES[Math.floor(Math.random() * TIMEZONES.length)];
}

function randomHour() {
  return Math.floor(Math.random() * 24);
}

function randomMinute() {
  return Math.floor(Math.random() * 60);
}

// ── Scenario functions ──

export function concurrentWrites() {
  const tz = randomTz();
  const payload = JSON.stringify({
    name: `Event-VU${__VU}-Iter${__ITER}`,
    timezone: tz,
    hour: randomHour(),
    minute: randomMinute(),
  });

  const start = Date.now();
  const res = http.post(`${BASE_URL}/events`, payload, {
    headers: { 'Content-Type': 'application/json' },
  });
  sessionCycleTime.add(Date.now() - start);

  const ok = check(res, {
    'write: status 201': (r) => r.status === 201,
    'write: has id': (r) => JSON.parse(r.body).id !== undefined,
  });

  if (ok) {
    concurrentWriteSuccess.add(1);
  } else {
    errorRate.add(1);
    if (res.status === 500 && res.body && res.body.includes('deadlock')) {
      deadlockErrors.add(1);
    }
  }

  sleep(0.05);
}

export function nestedTransactions() {
  const start = Date.now();
  const res = http.post(`${BASE_URL}/events/nested-demo`);
  sessionCycleTime.add(Date.now() - start);

  const ok = check(res, {
    'nested: status 200': (r) => r.status === 200,
    'nested: safe persisted': (r) => JSON.parse(r.body).safe_event_persisted === true,
    'nested: risky rolled back': (r) => JSON.parse(r.body).nested_rolled_back === true,
  });

  if (ok) {
    nestedTxnSuccess.add(1);
  } else {
    errorRate.add(1);
  }

  sleep(0.1);
}

export function bulkOperations() {
  const batchSize = 20 + Math.floor(Math.random() * 30); // 20-50 items
  const items = [];
  for (let i = 0; i < batchSize; i++) {
    items.push({
      name: `Bulk-VU${__VU}-Iter${__ITER}-${i}`,
      timezone: randomTz(),
      hour: randomHour(),
      minute: randomMinute(),
    });
  }

  const start = Date.now();
  const res = http.post(`${BASE_URL}/events/bulk`, JSON.stringify(items), {
    headers: { 'Content-Type': 'application/json' },
  });
  sessionCycleTime.add(Date.now() - start);

  const ok = check(res, {
    'bulk: status 201': (r) => r.status === 201,
    'bulk: correct count': (r) => JSON.parse(r.body).created === batchSize,
  });

  if (ok) {
    bulkOpsSuccess.add(1);
  } else {
    errorRate.add(1);
  }

  sleep(0.2);
}

export function mixedReadWrite() {
  const isWrite = Math.random() > 0.4; // 60% writes, 40% reads

  if (isWrite) {
    const payload = JSON.stringify({
      name: `Mixed-VU${__VU}-${Date.now()}`,
      timezone: randomTz(),
      hour: randomHour(),
      minute: randomMinute(),
    });

    const res = http.post(`${BASE_URL}/events`, payload, {
      headers: { 'Content-Type': 'application/json' },
    });

    const ok = check(res, {
      'mixed-write: status 201': (r) => r.status === 201,
    });

    if (!ok) {
      errorRate.add(1);
      if (res.status === 500 && res.body && res.body.includes('deadlock')) {
        deadlockErrors.add(1);
      }
    }
  } else {
    const res = http.get(`${BASE_URL}/events`);
    check(res, {
      'mixed-read: status 200': (r) => r.status === 200,
      'mixed-read: is array': (r) => JSON.parse(r.body).length >= 0,
    });
  }

  sleep(0.01);
}

export function readStorm() {
  const res = http.get(`${BASE_URL}/events`);

  check(res, {
    'read: status 200': (r) => r.status === 200,
  });

  if (res.status !== 200) {
    errorRate.add(1);
  }

  sleep(0.05);
}

export function timezoneCompute() {
  const tz1 = randomTz();
  const tz2 = randomTz();
  const h1 = randomHour();
  const h2 = randomHour();

  const responses = http.batch([
    ['GET', `${BASE_URL}/time/now/${tz1}`],
    ['GET', `${BASE_URL}/time/diff?tz1=${tz1}&hour1=${h1}&min1=0&tz2=${tz2}&hour2=${h2}&min2=0`],
  ]);

  check(responses[0], { 'tz-now: status 200': (r) => r.status === 200 });
  check(responses[1], { 'tz-diff: status 200': (r) => r.status === 200 });

  sleep(0.1);
}
