-- SAMPLE LOADING: a file instead of a recording.
--
--   * pointing "1 sample" at a WAV locks GRAINSWARM 1, sends the engine the
--     path, and sets BUFFR to the file's length
--   * the waveform trace is drawn from the file - it is not flat
--   * the folder the param starts on (and a non-audio file) does nothing
--   * GRAINSWARM 2 loads the same way; on LITE it does nothing
--   * a snapshot recall forgets the file name, because the buffer is the
--     snapshot's now

package.path = "test/?.lua;" .. package.path
local mock = require("mock_norns")
mock.install("lib/Engine_Pappus.sc")
dofile("pappus.lua")
init()

local fails = {}
local function check(cond, msg)
  if not cond then fails[#fails + 1] = msg end
end

-- init's bang ran the actions with the default (a folder): nothing loaded
check((mock.calls.engine["sampleload"] or 0) == 0,
  "the default folder value loaded something")

params:set("m_sample", "audio/notes.txt")
check((mock.calls.engine["sampleload"] or 0) == 0, "a .txt was sent to the engine")

params:set("m_sample", "audio/rain.wav")        -- 12.0 s, 44.1 kHz stereo
check((mock.calls.engine["sampleload"] or 0) == 1, "rain.wav was not loaded")
local last = mock.calls.last["sampleload"] or {}
check(last[1] == 1 and last[2] == "audio/rain.wav",
  "sampleload got " .. tostring(last[1]) .. ", " .. tostring(last[2]))
check(params:get("m_lock") == 2, "GRAINSWARM 1 was not locked")
check(math.abs(params:get("m_buflen") - 12.0) < 0.05,
  "BUFFR is " .. params:get("m_buflen") .. ", not the file's 12 s")
check(mock.calls.last["fade"] and mock.calls.last["fade"][1] == 1,
  "the output was left ducked")

local lit = 0
for i = 1, #VS[1].wave do if VS[1].wave[i] > 0.01 then lit = lit + 1 end end
check(lit > #VS[1].wave * 0.9, "the trace is mostly flat: " .. lit .. " lit slots")

-- 24-bit, 16 s
params:set("n_sample", "audio/rice.wav")
if LITE then
  check((mock.calls.engine["sampleload"] or 0) == 1, "LITE loaded into GRAINSWARM 2")
else
  check((mock.calls.engine["sampleload"] or 0) == 2, "rice.wav was not loaded")
  check(params:get("n_lock") == 2, "GRAINSWARM 2 was not locked")
  check(math.abs(params:get("n_buflen") - 16.0) < 0.05,
    "2 BUFFR is " .. params:get("n_buflen"))
  local lit2 = 0
  for i = 1, #VS[2].wave do if VS[2].wave[i] > 0.01 then lit2 = lit2 + 1 end end
  check(lit2 > #VS[2].wave * 0.5, "the 24-bit trace is flat: " .. lit2)
end

-- a snapshot recall forgets which file it was
snap_store(1)
snap_recall(1)
check(not tostring(params:get("m_sample")):find("rain", 1, true),
  "after a snapshot recall PARAMS still names " .. tostring(params:get("m_sample")))

if #fails > 0 then
  for _, f in ipairs(fails) do print("  FAIL " .. f) end
  print(string.format("SAMPLE TEST FAILED (%d)", #fails))
  os.exit(1)
end
print("SAMPLE TEST OK")
