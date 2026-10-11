-- Token bucket: refill for the time since the last call, take one token if there
-- is one, save the state. Runs atomically in Redis, so concurrent API replicas
-- can't race on the same bucket.
--
-- KEYS[1]  bucket key, e.g. ratelimit:runs_create:<user_id>  (hash: tokens, ts)
-- ARGV[1]  capacity (burst size)
-- ARGV[2]  refill rate, tokens per second
-- returns  {allowed (0/1), remaining tokens (floored), retry_after_ms}

local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])  -- tokens per second
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- An idle bucket refills completely after capacity/rate seconds; drop it then.
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)

local retry_ms = 0
if allowed == 0 then
  retry_ms = math.ceil((1 - tokens) / rate * 1000)
end
return {allowed, math.floor(tokens), retry_ms}
