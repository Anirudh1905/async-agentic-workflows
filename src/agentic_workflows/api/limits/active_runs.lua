-- Admit a run if the user has a free slot. Runs atomically in Redis, so two
-- concurrent POST /runs can't both take the last slot.
--
-- KEYS[1]  the user's active runs, e.g. active_runs:<user_id>  (zset: run_id -> admitted at, seconds)
-- ARGV[1]  max active runs
-- ARGV[2]  run_id
-- ARGV[3]  key ttl, seconds
-- returns  {admitted (0/1), active runs after this call}

local max = tonumber(ARGV[1])
local n = redis.call('ZCARD', KEYS[1])
if n >= max then
  return {0, n}
end

local t = redis.call('TIME')
redis.call('ZADD', KEYS[1], tonumber(t[1]) + tonumber(t[2]) / 1000000, ARGV[2])
-- Garbage-collects users who stop using the API. Far longer than any run.
redis.call('EXPIRE', KEYS[1], ARGV[3])
return {1, n + 1}
