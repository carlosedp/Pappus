-- DICE: K1 held + K3 rolls the page, K1 held + K2 undoes.
--
--   * a roll moves the page's controls, each by at most DICE AMOUNT of its
--     range (measured on the knob, in raw 0..1)
--   * what must never move does not: BUFFR and SOURCE on GRAINSWARM, the
--     destinations on a modulation page
--   * undo restores every value exactly
--   * the K2/K3 presses that dice owns do not also move between pages, even
--     when K1 is let go before them
--   * DICE nn% shows in the header

package.path = "test/?.lua;" .. package.path
local mock = require("mock_norns")
mock.install("lib/Engine_Pappus.sc")
dofile("pappus.lua")
init()

local fails = {}
local function check(cond, msg)
  if not cond then fails[#fails + 1] = msg end
end

local function page_index(kind)
  for i, pg in ipairs(pappus.pages) do if pg.kind == kind then return i end end
end

local function snapshot(pg)
  local t = {}
  for _, c in ipairs(pg.cells) do
    for _, id in ipairs({ c.id, c.alt, c.mode }) do
      if id then t[id] = params:get(id) end
    end
  end
  return t
end

local function raw(id, v)
  local p = params:lookup_param(id)
  if p.controlspec then return p.controlspec:unmap(v) end
  if p.options then return (v - 1) / math.max(1, #p.options - 1) end
  return (v - p.min) / math.max(1, p.max - p.min)
end

local function roll()
  key(1, 1); key(3, 1); key(3, 0); key(1, 0)
end

math.randomseed(7)
params:set("dice_amount", 0.3)

-- GRAINSWARM 1, page 1: continuous controls
local gi = page_index("grain")
goto_page_index(gi)
local pg = pappus.pages[gi]
local before = snapshot(pg)
roll()
check(current_page() == gi, "dice moved the page: now " .. tostring(current_page()))
local after = snapshot(pg)
local moved = 0
for id, v in pairs(before) do
  if after[id] ~= v then
    moved = moved + 1
    if not params:lookup_param(id).options then
      local d = math.abs(raw(id, after[id]) - raw(id, v))
      check(d <= 0.3 + 1e-6, id .. " moved " .. d .. " of its range, more than 0.3")
    end
  end
end
check(moved >= 4, "only " .. moved .. " controls moved")
check(dice_flash and dice_flash.text == "DICE 30", "no DICE flash")

-- undo is exact
key(1, 1); key(2, 1); key(2, 0); key(1, 0)
local back = snapshot(pg)
-- (exact up to the knob's own map/unmap round trip, which norns does too)
for id, v in pairs(before) do
  check(math.abs(back[id] - v) <= 1e-9 * math.max(1, math.abs(v)),
    "undo left " .. id .. " at " .. tostring(back[id]) .. ", was " .. tostring(v))
end
check(current_page() == gi, "undo moved the page")

-- page 2 holds BUFFR, which must never be rolled; nor SOURCE
local g2 = page_index("grain2")
goto_page_index(g2)
local bl, src = params:get("m_buflen"), params:get("m_src")
for _ = 1, 5 do roll() end
check(params:get("m_buflen") == bl, "dice changed BUFFR")
check(params:get("m_src") == src, "dice changed SOURCE")

-- a modulation page: amounts may move, destinations may not
local ei = page_index("envmod")
goto_page_index(ei)
local d1, d2 = params:get("env_d1"), params:get("env_d2")
for _ = 1, 5 do roll() end
check(params:get("env_d1") == d1 and params:get("env_d2") == d2,
  "dice rewired the envelope's destinations")

-- K1 let go before K3: K3's release still belongs to dice
goto_page_index(gi)
key(1, 1); key(3, 1); key(1, 0); key(3, 0)
check(current_page() == gi, "a K3 release after K1 went up turned the page")

-- and the PARAMS trigger does the same as the keys
local b2 = snapshot(pg)
params:set("dice", 1)
local a2, m2 = snapshot(pg), 0
for id, v in pairs(b2) do if a2[id] ~= v then m2 = m2 + 1 end end
check(m2 >= 4, "the dice trigger moved only " .. m2)

if #fails > 0 then
  for _, f in ipairs(fails) do print("  FAIL " .. f) end
  print(string.format("DICE TEST FAILED (%d)", #fails))
  os.exit(1)
end
print("DICE TEST OK")
