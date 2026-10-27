-- Marquee player controls: a YouTube-style layer drawn by mpv itself (so it sits on the video, at full
-- frame rate). Talks to the Python app through `script-message marquee <action> [value]` and reads what
-- the app knows (title, quality, intro times, next episode) from the user-data/marquee/* properties.
local mp = require "mp"
local utils = require "mp.utils"
local assdraw = require "mp.assdraw"

local ICON = "Segoe Fluent Icons"
local FONT = "Segoe UI Semibold"
local RED = "3300FF"          -- ASS colours are BGR: #FF0033
local WHITE = "FFFFFF"
local GLASS = "3F2014"        -- #14203F (Clear Home navy)
local function u(cp) -- UTF-8 for a code point in the BMP (icon glyphs live at U+E700..U+EBFF)
  return string.char(0xE0 + math.floor(cp / 4096), 0x80 + math.floor(cp / 64) % 64, 0x80 + cp % 64)
end
local G = {
  play = u(0xE768), pause = u(0xE769), next = u(0xE893), vol = u(0xE767), vol1 = u(0xE993), vol2 = u(0xE994),
  vol0 = u(0xE992), mute = u(0xE74F), cc = u(0xE7F0), gear = u(0xE713), full = u(0xE740), unfull = u(0xE73F),
  back = u(0xE72B), ff = u(0xEB9D), rew = u(0xEB9E), check = u(0xE73E), chev = u(0xE76C), chevl = u(0xE76B),
  pip = u(0xEE49), unpip = u(0xEE47), close = u(0xE711), playf = u(0xF5B0), pausef = u(0xF8AE),
  sparkle = u(0xF4A5), bookmark = u(0xE8EC), scissors = u(0xE8C6), folder = u(0xE838), stop = u(0xE71A),
  add = u(0xE710), loop = u(0xE8EE), cast = u(0xEC15),
}

local ov = mp.create_osd_overlay("ass-events")
ask_atlas = nil  -- defined with the key bindings below
scene_pick_close, scene_undo_now = nil, nil  -- the scene picker / Undo (also defined below)
clip_mode, clip_set, clip_add, clip_loop, clip_save = nil, nil, nil, nil, nil
local W, H = 1280, 720
local S = 1                    -- UI scale (1 at 1080p height)
local state = {
  visible = 1, last_move = 0, mouse = {x = -1, y = -1}, inside = false, menu = nil, menu_anim = 0,
  closing_menu = nil, closing_t = 0,
  flash = nil, flash_t = 0, seek_flash = nil, seek_t = 0, seek_acc = 0, held = false, hold_t = nil, prev_speed = 1,
  bar_hover = false, scrub = nil, vol_open = 0, vol_drag = false, vol_flash_t = -10, buttons = {}, rows = {},
  toast = nil, toast_t = 0, glass_rects = {}, shader = false, glass_opts = nil, next_cancelled = false, next_sent = false, gear_spin = -10, cursor = nil, last_ass = nil,
}

-- ------------------------------------------------------------------------------- helpers
local function now() return mp.get_time() end
local function clamp(v, a, b) return math.max(a, math.min(b, v)) end
local function lerp(a, b, t) return a + (b - a) * t end
local function ease(t) return 1 - (1 - t) ^ 3 end
local function prop(name, def) local v = mp.get_property_native(name); if v == nil then return def end; return v end
local ud_cache = {}
local function ud(name, def) -- values the app sets, as JSON strings
  local raw = mp.get_property_native("user-data/marquee/" .. name)
  if type(raw) ~= "string" then if raw == nil then return def end; return raw end
  if raw == "" then return def end
  local c = ud_cache[name]
  if c and c.raw == raw then if c.val == nil then return def end; return c.val end
  local val = utils.parse_json(raw)
  ud_cache[name] = {raw = raw, val = val}
  if val == nil then return def end
  return val
end
local function send(action, value, ...)
  local extra = {}
  for _, x in ipairs({...}) do table.insert(extra, tostring(x)) end
  local up = table.unpack or unpack
  mp.commandv("script-message", "marquee", action, value and tostring(value) or "", up(extra))
end

local function fmt(t)
  t = math.max(0, math.floor(t or 0))
  local h, m, s = math.floor(t / 3600), math.floor(t % 3600 / 60), t % 60
  if h > 0 then return string.format("%d:%02d:%02d", h, m, s) end
  return string.format("%d:%02d", m, s)
end

local function esc(s) -- text from outside (titles, track names) must not be read as ASS tags
  s = tostring(s or "")
  return (s:gsub("\\", "\\\239\187\191"):gsub("{", "\\{"):gsub("}", "\\}"):gsub("\n", " "))
end

local function ulen(s) local _, n = tostring(s):gsub("[^\128-\191]", ""); return n end

local function alpha(a) -- 0..1 opacity -> ASS alpha hex
  return string.format("%02X", clamp(math.floor((1 - a) * 255 + 0.5), 0, 255))
end

local function box(ass, x, y, w, h, r, color, a, border, bcolor, ba, extra)
  if w <= 0 or h <= 0 or (a <= 0.003 and (ba or 0) <= 0.003) then return end
  r = math.min(r or 0, w / 2, h / 2)
  ass:new_event()
  ass:append(string.format("{\\pos(0,0)\\an7\\bord%.2f\\shad0\\1c&H%s&\\1a&H%s&\\3c&H%s&\\3a&H%s&%s}",
    border or 0, color, alpha(a), bcolor or WHITE, alpha(ba or 0), extra or ""))
  ass:draw_start()
  if r > 0.5 then ass:round_rect_cw(x, y, x + w, y + h, r) else ass:rect_cw(x, y, x + w, y + h) end
  ass:draw_stop()
end

-- Clear Home "liquid glass" on video. mpv can't blur what's behind, so the frosting is faked with layers:
-- a soft drop shadow, a navy tint, a faint white fill, a brighter sheen over the top and left (the 140deg
-- gradient), an inner glow, a 1 px white rim and a bright highlight along the top edge.
local SHADER_RECTS = 4
local function glass(ass, x, y, w, h, r, a, opts)
  if a <= 0.003 or w <= 0 or h <= 0 then return end
  opts = opts or {}
  r = math.min(r, w / 2, h / 2)
  local tint = opts.tint or 0.5
  local white = opts.white or 0.07
  -- with the glass shader (glass.glsl) the video under the panel is really frosted and bent: the layers
  -- here then only add the tint and light on top, so they're much lighter
  local frosted = false
  if state.shader and not opts.flat and not opts.noshader and #state.glass_rects < SHADER_RECTS then
    table.insert(state.glass_rects, {x, y, w, h, r, a})
    -- (menus keep more of their tint: captions are drawn after the shader, so they'd show through)
    tint, white, frosted = tint * (opts.keep or 0.25), white * 0.4, true
  end
  if not opts.flat then
    box(ass, x, y + 5 * S, w, h, r, "000000", 0.32 * a, 0, nil, 0, string.format("\\blur%.1f", 12 * S))
  end
  box(ass, x, y, w, h, r, GLASS, tint * a)
  box(ass, x, y, w, h, r, "FFFFFF", white * a)
  if not frosted then  -- the shader draws its own sheen; without it, a soft top-down one in a few steps
    for i = 0, 3 do
      box(ass, x, y, w, h, r, "FFFFFF", 0.022 * a, 0, nil, 0,
        string.format("\\clip(%.1f,%.1f,%.1f,%.1f)", x, y, x + w, y + h * (0.2 + 0.15 * i)))
    end
  end
  -- inner glow + rim
  box(ass, x + 1.5, y + 1.5, w - 3, h - 3, r - 1.5, "FFFFFF", 0, 2.2 * S, WHITE, 0.08 * a, string.format("\\blur%.1f", 2.5 * S))
  box(ass, x, y, w, h, r, "FFFFFF", 0, 1, WHITE, (opts.rim or 0.42) * a)
  -- top highlight
  local hx = r * 0.75
  box(ass, x + hx, y + 1, w - 2 * hx, math.max(1, 1.1 * S), 0, "FFFFFF", 0.6 * a, 0, nil, 0, "\\blur0.6")
end

local function glass_circle(ass, x, y, d, a, strong)
  glass(ass, x, y, d, d, d / 2, a, {flat = true, tint = strong and 0.35 or 0.22, white = strong and 0.14 or 0.1, rim = 0.4})
end

local function text(ass, x, y, an, size, str, color, a, font, extra)
  if (a or 1) <= 0.003 then return end
  ass:new_event()
  -- on a clear glass panel the moving picture shows through: a soft shadow keeps the words readable
  local soft = state.legible and string.format("\\shad%.1f\\4c&H000000&\\4a&H%s&", 1.4 * S, alpha(0.55 * (a or 1))) or "\\shad0"
  ass:append(string.format("{\\pos(%.2f,%.2f)\\an%d\\fs%.2f\\fn%s\\bord0%s\\1c&H%s&\\1a&H%s&%s}",
    x, y, an, size, font or FONT, soft, color or WHITE, alpha(a or 1), extra or ""))
  ass:append(str)
end

local function shadow_text(ass, x, y, an, size, str, color, a, font, extra)
  -- a soft shadow keeps white text readable on bright frames
  text(ass, x, y, an, size, str, color, a, font, string.format("\\shad0\\bord%.2f\\3c&H000000&\\3a&H%s&\\blur%.2f%s",
    1.6 * S, alpha(0.35 * (a or 1)), 2.5 * S, extra or ""))
end

local function icon(ass, x, y, size, glyph, a, color, extra)
  -- a hairline outline in the same colour gives the thin Fluent glyphs a little more weight
  text(ass, x, y, 5, size, glyph, color or WHITE, a, ICON, string.format("\\bord%.2f\\3c&H%s&\\3a&H%s&%s",
    0.45 * S, color or WHITE, alpha(a or 1), extra or ""))
end

local function hit(b) local m = state.mouse; return b and m.x >= b.x and m.x <= b.x + b.w and m.y >= b.y and m.y <= b.y + b.h end

local function button(id, x, y, w, h, action)
  local b = {id = id, x = x, y = y, w = w, h = h, action = action}
  table.insert(state.buttons, b)
  return b, hit(b)
end

local function flash_pause()
  state.flash = prop("pause", false) and G.pause or G.play
  state.flash_t = now()
end

local function toggle_pause()
  mp.commandv("cycle", "pause")
  flash_pause()
end

local function poke() state.last_move = now() end

local function seek(d)
  if state.seek_flash and (d > 0) == (state.seek_flash > 0) and now() - state.seek_t < 0.8 then
    state.seek_acc = state.seek_acc + d
  else state.seek_acc = d end
  mp.commandv("seek", tostring(d), "relative", "exact")
  state.seek_flash = state.seek_acc; state.seek_t = now()
end

-- ------------------------------------------------------------------------------- menus
local function track_list(kind)
  local out = {}
  for _, t in ipairs(prop("track-list", {})) do
    if t.type == kind then table.insert(out, t) end
  end
  return out
end

local LANGS = {eng = "English", en = "English", jpn = "Japanese", ja = "Japanese", spa = "Spanish", es = "Spanish",
  fre = "French", fra = "French", fr = "French", ger = "German", deu = "German", de = "German", ita = "Italian",
  it = "Italian", por = "Portuguese", pt = "Portuguese", kor = "Korean", ko = "Korean", chi = "Chinese",
  zho = "Chinese", zh = "Chinese", rus = "Russian", ru = "Russian", ara = "Arabic", hin = "Hindi"}

local CODECS = {truehd = "Dolby TrueHD", mlp = "Dolby TrueHD", eac3 = "Dolby Digital Plus", ac3 = "Dolby Digital",
  dts = "DTS", flac = "FLAC", aac = "AAC", opus = "Opus", mp3 = "MP3", vorbis = "Vorbis", alac = "ALAC"}

local function channels_label(n)
  if not n then return "" end
  return n >= 8 and "7.1" or n >= 6 and "5.1" or n == 2 and "Stereo" or n == 1 and "Mono" or (n .. " ch")
end

-- "English [NF]" -> "NF", "English Signs/Songs [Group]" -> "Signs/Songs · Group", "(SDH)" kept as "(SDH)"
local function tidy_title(title, lang)
  local s = title or ""
  if lang then
    local l = lang:lower()
    if s:lower():sub(1, #l) == l then s = s:sub(#l + 1) end
  end
  s = s:gsub("%(?[Ff]orced%)?", ""):gsub("^[%s%-·:,]+", ""):gsub("[%s%-·:,]+$", "")
  s = s:gsub("%s*%[([^%]]*)%]%s*", " · %1 · "):gsub("^[%s·]+", ""):gsub("[%s·]+$", ""):gsub("%s*·%s*·%s*", " · ")
  return s
end

local function track_label(t)
  local lang = t.lang and (LANGS[t.lang:lower()] or t.lang:upper()) or nil
  if t.type == "audio" then
    local codec = CODECS[t.codec or ""] or (t.codec or ""):upper()
    local prof = t["codec-profile"] or ""
    if t.codec == "dts" and prof ~= "" and prof ~= "DTS" then codec = prof end      -- "DTS-HD MA", "DTS:X"
    local title = (t.title or ""):lower()
    if (t.codec == "truehd" or t.codec == "eac3") and (title:find("atmos") or prof:lower():find("atmos")) then
      codec = codec .. " Atmos"
    end
    local parts = {}
    if lang then table.insert(parts, lang) end
    table.insert(parts, codec .. " " .. channels_label(t["demux-channel-count"]))
    if not lang and t.title then table.insert(parts, 1, t.title) end
    return table.concat(parts, " · ")
  end
  local rest = tidy_title(t.title, lang)
  local out
  if rest == "" then out = lang or ("Track " .. t.id)
  elseif rest:sub(1, 1) == "(" then out = (lang and (lang .. " ") or "") .. rest            -- "English (SDH)"
  elseif lang then out = lang .. " · " .. rest
  else out = rest end
  return out
end

local function track_tags(t)
  local tags = {}
  if t.forced or (t.title or ""):lower():find("forced") then table.insert(tags, "Forced") end
  -- "Default" only means something when a single track carries it
  local defaults = 0
  for _, o in ipairs(track_list(t.type)) do if o.default then defaults = defaults + 1 end end
  if t.default and defaults == 1 then table.insert(tags, "Default") end
  if t.external then table.insert(tags, "External") end
  return table.concat(tags, " · ")
end

local function speed_label(sp) if math.abs(sp - 1) < 0.01 then return "Normal" end; return string.format("%gx", math.floor(sp * 100 + 0.5) / 100) end

-- Pass-through: is the current track going out bitstreamed right now?
local function passthrough_now()
  local f = prop("audio-out-params/format", "") or ""
  return f:sub(1, 5) == "spdif"
end

local function set_speed(sp)
  sp = clamp(math.floor(sp * 20 + 0.5) / 20, 0.5, 3.0)
  mp.set_property_number("speed", sp)
  state.prev_speed = sp
  state.toast = "Speed " .. speed_label(sp); state.toast_t = now()
end

local SPEEDS = {0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3}
local QUALITIES = {{"original", "Original"}, {"2160", "2160p 4K"}, {"1080", "1080p"}, {"720", "720p"}, {"480", "480p"}}
local UPSCALERS = {{"auto", "Auto (recommended)"}, {"nvidia", "NVIDIA RTX Super Resolution"}, {"anime4k", "Anime4K"},
  {"hq", "High quality (mpv)"}, {"off", "Off"}}
local TAGS = {"Favourite", "Funny", "Review"}

local function menu_rows(m)
  local rows = {}
  local pt_active = ud("passthrough_active", false) or passthrough_now()
  local pt_note = "Not with pass-through"
  if m == "captions" then
    local sid = prop("sid", false)
    table.insert(rows, {label = "Off", checked = (sid == false or sid == "no"), act = function() mp.set_property("sid", "no") end})
    for _, t in ipairs(track_list("sub")) do
      table.insert(rows, {label = track_label(t), tag = track_tags(t), checked = t.selected,
        act = function() mp.set_property_number("sid", t.id) end})
    end
  elseif m == "settings" then
    -- where the picture comes from and how healthy it is (live)
    local ahead = prop("demuxer-cache-duration", 0) or 0
    local drops = (prop("frame-drop-count", 0) or 0) + (prop("decoder-frame-drop-count", 0) or 0)
    table.insert(rows, {info = true, disabled = true, label = string.format("Playing from: %s · cache %d s ahead · %d dropped frame%s",
      ud("source_kind", "local file"), math.floor(ahead), drops, drops == 1 and "" or "s")})
    local stable = ud("stable", false)
    table.insert(rows, {label = "Stable volume", toggle = true, on = stable, disabled = pt_active and pt_note,
      act = function() send("stable", stable and "off" or "on") end})
    table.insert(rows, {label = "Quality", value = ud("quality_label", "Original"), sub = "quality"})
    table.insert(rows, {label = "Playback speed", value = speed_label(state.held and state.prev_speed or prop("speed", 1)), sub = "speed"})
    local cur = ""
    for _, t in ipairs(track_list("audio")) do
      if t.selected then cur = t.lang and (LANGS[t.lang:lower()] or t.lang:upper()) or ("Track " .. t.id) end
    end
    table.insert(rows, {label = "Audio", value = cur .. (pt_active and " · pass-through" or ""), sub = "audio"})
    table.insert(rows, {label = "Upscaling", value = ud("upscaler_label", "Off"), sub = "upscaler"})
    local hdr = ud("sdr_to_hdr", "auto")
    table.insert(rows, {label = "SDR → HDR", value = hdr == "auto" and "Auto" or "Off", cycle = true,
      act = function() send("sdr_to_hdr", hdr == "auto" and "off" or "auto") end})
    local chapters = prop("chapter-list", {})
    if #chapters > 1 then
      table.insert(rows, {label = "Chapters", value = tostring(#chapters), sub = "chapters"})
    end
    local bms = ud("bookmarks", {})
    if #bms > 0 then table.insert(rows, {label = "Bookmarks", value = tostring(#bms), sub = "bookmarks"}) end
    if ud("clips_ok", false) then
      table.insert(rows, {label = "Clip", value = "X", sub = nil, cycle = true, act = function() clip_mode(true) end})
    end
    table.insert(rows, {label = "Playback", value = "Arrows " .. ud("seek_step", 5) .. " s", sub = "playback"})
  elseif m == "quality" then
    table.insert(rows, {label = "Quality", back = "settings"})
    local src = ud("source_height", 0)
    if ud("quality_locked", false) then src = 1 end  -- a local file outside Jellyfin: only the original
    for _, q in ipairs(QUALITIES) do
      -- nothing above the source's own resolution (a 1080p file has no 4K to offer)
      if q[1] == "original" or src == 0 or tonumber(q[1]) < src * 0.95 then table.insert(rows, {label = q[2], checked = ud("quality", "original") == q[1], act = function()
        if ud("quality", "original") ~= q[1] then send("quality", q[1]) end
      end}) end
    end
  elseif m == "speed" then
    table.insert(rows, {label = "Playback speed", back = "settings"})
    table.insert(rows, {stepper = true, label = speed_label(prop("speed", 1))})
    for _, sp in ipairs(SPEEDS) do
      table.insert(rows, {label = speed_label(sp), checked = math.abs(prop("speed", 1) - sp) < 0.01,
        act = function() set_speed(sp) end})
    end
  elseif m == "upscaler" then
    table.insert(rows, {label = "Upscaling", back = "settings"})
    for _, up in ipairs(UPSCALERS) do
      local missing = up[1] == "anime4k" and not ud("anime4k", false)
      table.insert(rows, {label = up[2], checked = ud("upscaler", "off") == up[1], disabled = missing and "Shaders not installed",
        act = function() send("upscaler", up[1]) end})
    end
  elseif m == "audio" then
    table.insert(rows, {label = "Audio", back = "settings"})
    for _, t in ipairs(track_list("audio")) do
      local label = track_label(t)
      if t.selected and passthrough_now() then label = label .. " · passthrough" end
      table.insert(rows, {label = label, checked = t.selected, act = function() mp.set_property_number("aid", t.id) end})
    end
    local night, dlg, pt = ud("night", false), ud("dialogue", false), ud("passthrough", false)
    table.insert(rows, {label = "Night mode", toggle = true, on = night, divider = true, disabled = pt_active and pt_note,
      act = function() send("night", night and "off" or "on") end})
    local vf = ud("voice_focus", dlg and "dialogue" or "off")
    table.insert(rows, {label = "Voice focus", value = ({off = "Off", dialogue = "Dialogue boost", isolate = "AI voice isolation"})[vf],
      sub = not pt_active and "voice" or nil, disabled = pt_active and pt_note})
    table.insert(rows, {label = "Pass-through to receiver/soundbar", toggle = true, on = pt,
      act = function() send("passthrough", pt and "off" or "on") end})
    local dev, desc = ud("audio_device", "auto"), "System default"
    for _, d in ipairs(prop("audio-device-list", {})) do
      if d.name == dev and dev ~= "auto" then desc = d.description or d.name end
    end
    table.insert(rows, {label = "Output device", value = desc, sub = "device"})
  elseif m == "voice" then
    table.insert(rows, {label = "Voice focus", back = "audio"})
    local vf = ud("voice_focus", "off")
    table.insert(rows, {label = "Off", checked = vf == "off", act = function() send("voice_focus", "off") end})
    table.insert(rows, {label = "Dialogue boost", checked = vf == "dialogue", act = function() send("voice_focus", "dialogue") end})
    table.insert(rows, {label = "AI voice isolation", checked = vf == "isolate",
      disabled = not ud("voice_model", false) and "Model not installed",
      act = function() send("voice_focus", "isolate") end})
  elseif m == "chapters" then
    table.insert(rows, {label = "Chapters", back = "settings"})
    local cur = prop("chapter", -1)
    for i, c in ipairs(prop("chapter-list", {})) do
      table.insert(rows, {label = (c.title and c.title ~= "" and c.title or ("Chapter " .. i)), tag = fmt(c.time),
        checked = (i - 1) == cur, act = function() mp.commandv("seek", tostring(c.time), "absolute", "exact") end})
    end
  elseif m == "bookmarks" then
    table.insert(rows, {label = "Bookmarks", back = "settings"})
    for _, b in ipairs(ud("bookmarks", {})) do
      local label = fmt(b.ms / 1000) .. ((b.note or "") ~= "" and ("  " .. b.note) or "")
      table.insert(rows, {label = label, tag = b.tag ~= "" and b.tag or nil, remove = b.id,
        act = function() mp.commandv("seek", string.format("%.3f", b.ms / 1000), "absolute", "exact") end})
    end
  elseif m == "device" then
    table.insert(rows, {label = "Output device", back = "audio"})
    local dev = ud("audio_device", "auto")
    for _, d in ipairs(prop("audio-device-list", {})) do
      if d.name == "auto" or d.name:sub(1, 7) == "wasapi/" then
        table.insert(rows, {label = d.name == "auto" and "System default" or (d.description or d.name),
          checked = d.name == dev, act = function() send("audio_device", d.name) end})
      end
    end
  elseif m == "cast" then
    -- TVs on the home network (the app looks while this menu is open); picking one hands the video over
    local devs = ud("cast_devices", {})
    local searching = ud("cast_searching", false)
    local head
    if searching then head = "Looking for TVs" .. string.rep(".", math.floor(now() * 2.5) % 4)
    elseif #devs == 0 then head = "No TVs found on this network"
    else head = "Play on a TV from here" end
    table.insert(rows, {info = true, disabled = true, label = head})
    for _, d in ipairs(devs) do
      table.insert(rows, {label = d.name or "TV", tag = d.type or "", act = function() send("cast", d.id) end})
    end
    local note = ud("cast_note", "")
    if note ~= "" then table.insert(rows, {info = true, disabled = true, label = note}) end
  elseif m == "playback" then
    table.insert(rows, {label = "Playback", back = "settings"})
    if ud("has_next", false) then
      local ap = ud("autoplay", true)
      table.insert(rows, {label = "Autoplay next episode", toggle = true, on = ap, act = function() send("autoplay", ap and "off" or "on") end})
    end
    local step = ud("seek_step", 5)
    table.insert(rows, {label = "Arrow-key jump", value = step .. " s", cycle = true,
      act = function() send("seek_step", step == 5 and 10 or 5) end})
    local hw = ud("hwdec", true)
    table.insert(rows, {label = "Hardware decoding", toggle = true, on = hw, note = hw and "recommended" or nil,
      act = function() send("hwdec", hw and "off" or "on") end})
  end
  return rows
end

local function close_menu()
  if state.menu then state.closing_menu = state.menu; state.closing_t = now() end
  state.menu = nil
end

local function open_menu(name)
  if state.menu == name then close_menu() return end
  state.closing_menu = nil
  state.menu = name; state.menu_anim = now()
  if name == "settings" then state.gear_spin = now() end
  if name == "cast" then send("cast-scan") end
end

-- ------------------------------------------------------------------------------- drawing
local function wrap(str, per, max_lines)
  local lines = {}
  for para in (tostring(str or "") .. "\n"):gmatch("(.-)\n") do
    local line = ""
    for word in para:gmatch("%S+") do
      if line == "" then line = word
      elseif ulen(line) + 1 + ulen(word) <= per then line = line .. " " .. word
      else table.insert(lines, line); line = word end
    end
    table.insert(lines, line)
  end
  while #lines > 1 and lines[#lines] == "" do table.remove(lines) end
  if max_lines and #lines > max_lines then
    while #lines > max_lines do table.remove(lines) end
    lines[max_lines] = lines[max_lines] .. " …"
  end
  return lines
end

local function ellipsize(str, max_chars)
  if ulen(str) <= max_chars then return str end
  local out, n = {}, 0
  for ch in str:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
    n = n + 1
    if n > max_chars - 1 then break end
    table.insert(out, ch)
  end
  return table.concat(out) .. "…"
end

local function draw_menu(ass, name, k, m, interactive)
  local rows = menu_rows(name)
  local rh = 46 * S
  local fs = 19 * S
  local cw = fs * 0.5            -- average character width of the UI font at this size
  local mw = 320 * S
  for _, r in ipairs(rows) do  -- wide enough for the longest row: label + tag/value + toggle/chevron
    local extra = (r.toggle and 70 or 0) + (r.sub and 40 or 0) + (r.checked ~= nil and 30 or 0) + 48
    local side = r.info and -ulen(r.label) * 0.2 or r.disabled and ulen(r.disabled) or (r.tag and r.tag ~= "" and ulen(r.tag) or ulen(r.value or ""))
    local need = (ulen(r.label or "") + side * 0.85 + (side > 0 and 3 or 0)) * cw + extra * S
    mw = math.max(mw, need)
  end
  mw = math.min(mw, W * 0.7)
  local mh = #rows * rh + 16 * S
  local mx = W - m - 12 * S - mw
  local my = (state.above or (H - 128 * S)) - mh + (1 - k) * 18 * S
  glass(ass, mx, my, mw, mh, 22 * S, k, {tint = 0.36, keep = 0.5})  -- clear liquid glass: the movie shows through
  state.legible = true
  if interactive then state.rows = {} end
  for i, r in ipairs(rows) do
    local ry = my + 8 * S + (i - 1) * rh
    local rb = {x = mx + 6 * S, y = ry, w = mw - 12 * S, h = rh, row = r}
    if interactive then table.insert(state.rows, rb) end
    local hov = interactive and hit(rb) and not r.disabled
    if hov and not r.stepper then box(ass, rb.x, rb.y, rb.w, rb.h, 14 * S, "FFFFFF", 0.12 * k, 1, WHITE, 0.14 * k) end
    if r.divider then box(ass, mx + 12 * S, ry, mw - 24 * S, 1, 0, "FFFFFF", 0.16 * k) end
    local lx = mx + 22 * S
    local a = r.disabled and 0.45 * k or k
    if r.info then
      text(ass, lx, ry + rh / 2, 4, 15 * S, esc(r.label), WHITE, 0.6 * k)
      box(ass, mx + 12 * S, ry + rh - 1, mw - 24 * S, 1, 0, "FFFFFF", 0.14 * k)
    elseif r.back then
      icon(ass, lx + 6 * S, ry + rh / 2, 14 * S, G.chevl, k)
      text(ass, lx + 28 * S, ry + rh / 2, 4, fs, esc(r.label), WHITE, k)
      box(ass, mx + 12 * S, ry + rh - 1, mw - 24 * S, 1, 0, "FFFFFF", 0.18 * k)
    elseif r.stepper then
      -- [ − ]   1.25x   [ + ]  in 0.05 steps
      local bw = 52 * S
      local lb = {x = mx + 16 * S, y = ry + 5 * S, w = bw, h = rh - 10 * S}
      local rbx = {x = mx + mw - 16 * S - bw, y = ry + 5 * S, w = bw, h = rh - 10 * S}
      for _, b in ipairs({lb, rbx}) do
        local hv = interactive and hit(b)
        box(ass, b.x, b.y, b.w, b.h, b.h / 2, "FFFFFF", (hv and 0.22 or 0.1) * k, 1, WHITE, 0.25 * k)
      end
      text(ass, lb.x + bw / 2, ry + rh / 2, 5, 24 * S, "−", WHITE, k)
      text(ass, rbx.x + bw / 2, ry + rh / 2, 5, 24 * S, "+", WHITE, k)
      text(ass, mx + mw / 2, ry + rh / 2, 5, 21 * S, esc(r.label), WHITE, k)
      r.minus, r.plus = lb, rbx
    else
      if r.checked then icon(ass, lx + 6 * S, ry + rh / 2, 15 * S, G.check, k) end
      local tx = lx + (r.checked ~= nil and 30 or 0) * S
      local right = mx + mw - 22 * S
      local room = right - tx - ((r.toggle and 60 or 0) + (r.sub and 30 or 0)) * S
      local side = r.disabled or (r.tag ~= "" and r.tag) or (r.cycle and r.value) or nil
      if side then room = room - (ulen(side) * 0.85 * cw + 12 * S) end
      text(ass, tx, ry + rh / 2, 4, fs, esc(ellipsize(r.label, math.max(6, math.floor(room / cw)))), WHITE, a)
      if r.note then text(ass, tx + (ulen(r.label) + 1) * cw, ry + rh / 2, 4, 15 * S, esc(r.note), WHITE, 0.55 * a) end
      if r.disabled then
        text(ass, right, ry + rh / 2, 6, 15 * S, esc(r.disabled), WHITE, 0.6 * k)
      elseif r.toggle then
        local tw, th = 40 * S, 22 * S
        local tx2, ty = right - tw, ry + rh / 2 - th / 2
        box(ass, tx2, ty, tw, th, th / 2, r.on and RED or "FFFFFF", r.on and k or 0.3 * k)
        box(ass, tx2 + (r.on and tw - th or 0) + 3 * S, ty + 3 * S, th - 6 * S, th - 6 * S, (th - 6 * S) / 2, WHITE, k)
      elseif r.sub then
        text(ass, mx + mw - 42 * S, ry + rh / 2, 6, 17 * S, esc(r.value or ""), WHITE, 0.72 * k)
        icon(ass, mx + mw - 24 * S, ry + rh / 2, 12 * S, G.chev, 0.8 * k)
      elseif r.remove then
        local xb = {x = right - 26 * S, y = ry + rh / 2 - 13 * S, w = 26 * S, h = 26 * S}
        r.remove_box = xb
        if interactive and hit(xb) then box(ass, xb.x, xb.y, xb.w, xb.h, 13 * S, "FFFFFF", 0.18 * k) end
        icon(ass, xb.x + xb.w / 2, xb.y + xb.h / 2, 11 * S, G.close, 0.8 * k)
        if r.tag then
          local cwid = (ulen(r.tag) * 15 * S * 0.5) + 20 * S
          box(ass, xb.x - 8 * S - cwid, ry + rh / 2 - 12 * S, cwid, 24 * S, 12 * S, "FFFFFF", 0.1 * k, 1, WHITE, 0.25 * k)
          text(ass, xb.x - 8 * S - cwid / 2, ry + rh / 2, 5, 15 * S, esc(r.tag), WHITE, 0.8 * k)
        end
      elseif side then
        -- tags (Forced / Default) and cycling values as a small glass chip on the right
        local chip = esc(side)
        local cwid = (ulen(side) * 15 * S * 0.5) + 20 * S
        box(ass, right - cwid, ry + rh / 2 - 12 * S, cwid, 24 * S, 12 * S, "FFFFFF", 0.1 * k, 1, WHITE, 0.25 * k)
        text(ass, right - cwid / 2, ry + rh / 2, 5, 15 * S, chip, WHITE, 0.8 * k)
      end
    end
  end
  state.legible = false
end

local function set_cursor(show)
  local want = show and "no" or "always"
  if state.cursor ~= want then state.cursor = want; mp.set_property("cursor-autohide", want) end
end

-- Hand the glass rectangles to the shader, in pixels of the video image (the OUTPUT hook only covers the
-- video, not the black bars). Only when something changed: during a fade that's every frame, otherwise never.
local function push_glass()
  if not state.shader then return end
  local d = prop("osd-dimensions", {})
  local ml, mt = d.ml or 0, d.mt or 0
  local parts = {}
  local any = false
  for i = 0, SHADER_RECTS - 1 do
    local g = state.glass_rects[i + 1]
    if g then
      any = true
      table.insert(parts, string.format("g%dx=%d,g%dy=%d,g%dw=%d,g%dh=%d,g%dr=%.1f,g%da=%.3f",
        i, math.floor(g[1] - ml + 0.5), i, math.floor(g[2] - mt + 0.5), i, math.floor(g[3] + 0.5),
        i, math.floor(g[4] + 0.5), i, g[5], i, math.floor(g[6] * 50 + 0.5) / 50))
    else
      table.insert(parts, string.format("g%da=0", i))
    end
  end
  table.insert(parts, 1, string.format("g_on=%d,g_blur=%.1f,g_vw=%d,g_vh=%d", any and 1 or 0, 13 * S,
    math.max(1, W - ml - (d.mr or 0)), math.max(1, H - mt - (d.mb or 0))))
  local opts = table.concat(parts, ",")
  if opts ~= state.glass_opts then
    state.glass_opts = opts
    mp.set_property("glsl-shader-opts", opts)
  end
end

local function draw()
  state.buttons = {}
  W, H = mp.get_osd_size()
  if not W or W <= 0 or H <= 0 then return end
  if prop("idle-active", false) then
    if state.last_ass ~= "" then ov.data = ""; ov:update(); state.last_ass = "" end
    state.glass_rects = {}
    push_glass()
    set_cursor(true)
    return
  end
  state.pip = ud("pip", false)
  state.mini = ud("mini", false)
  S = clamp(H / 960, state.pip and 0.45 or 0.7, 2.4)
  FONT = ud("font", "Segoe UI Semibold")
  ICON = ud("icons", "Segoe Fluent Icons")
  state.shader = ud("glass_shader", false)
  state.glass_rects = {}
  ov.res_x, ov.res_y = W, H
  local ass = assdraw.ass_new()
  local t = now()
  local paused = prop("pause", false)

  -- fade controls in/out: hide after ~2.5 s without the mouse moving (or right away when it leaves)
  local idle = (not paused) and (t - state.last_move) > 2.5 and not state.menu and not state.scrub and not state.vol_drag
    and not state.clip and not state.prompt
  if state.held or state.mini then idle = true end  -- the credits tile shows just the picture
  local target = idle and 0 or 1
  state.visible = lerp(state.visible, target, target > state.visible and 0.3 or 0.16)
  if math.abs(state.visible - target) < 0.01 then state.visible = target end
  local v = state.visible
  set_cursor(v > 0.05 or not state.inside)

  local pos, dur = prop("time-pos", 0) or 0, prop("duration", 0) or 0
  local m = 24 * S

  -- the glass capsule that holds the progress bar and the control row
  local cap_h = 100 * S
  local cap_x, cap_w = m, W - 2 * m
  local cap_y = H - 14 * S - cap_h + (1 - ease(v)) * 12 * S
  state.clip_h = state.clip and (state.clip.slices[1] and 112 or 66) * S or 0
  state.above = lerp(H - 28 * S, cap_y - 14 * S - (state.clip_h > 0 and state.clip_h + 8 * S or 0), v)   -- where pills, cards and menus sit

  if state.pip then
    state.bar_hover = false
    state.above = H - 12 * S
    local P = clamp(H / 300, 0.6, 1.6)
    if v > 0.003 then
      box(ass, 0, 0, W, H, 0, "000000", 0.38 * v)
      local d = 58 * P
      local cx, cy = W / 2, H / 2
      local b, hv = button("pip-play", cx - d / 2, cy - d / 2, d, d, toggle_pause)
      glass_circle(ass, b.x, b.y, d, v, hv)
      icon(ass, cx, cy, 24 * P, paused and G.playf or G.pausef, v)
      local sd = 44 * P
      local step = ud("seek_step", 5)
      for _, side in ipairs({-1, 1}) do
        local sx = cx + side * 84 * P
        local sb, shv = button("pip-seek" .. side, sx - sd / 2, cy - sd / 2, sd, sd, function() seek(side * step) end)
        if shv then glass_circle(ass, sb.x, sb.y, sd, v, true) end
        icon(ass, sx, cy - 4 * P, 17 * P, side < 0 and G.rew or G.ff, v)
        text(ass, sx, cy + 13 * P, 5, 11 * P, tostring(step), WHITE, 0.85 * v, FONT)
      end
      local cd = 34 * P
      local xb, xhv = button("pip-close", 10 * P, 10 * P, cd, cd, function() send("close") end)
      glass_circle(ass, xb.x, xb.y, cd, v * (xhv and 1 or 0.7), xhv)
      icon(ass, xb.x + cd / 2, xb.y + cd / 2, 13 * P, G.close, v)
      local fb, fhv = button("pip-full", W - 10 * P - cd, 10 * P, cd, cd, function() send("pip") end)
      glass_circle(ass, fb.x, fb.y, cd, v * (fhv and 1 or 0.7), fhv)
      icon(ass, fb.x + cd / 2, fb.y + cd / 2, 14 * P, G.unpip, v)
    end
    -- the progress line stays, like a mini player's
    local lh = math.max(2, 3 * P)
    box(ass, 0, H - lh, W, lh, 0, "FFFFFF", 0.25)
    if dur > 0 then box(ass, 0, H - lh, W * clamp(pos / dur, 0, 1), lh, 0, RED, 1) end
  elseif v > 0.003 then
    -- a soft scrim behind the capsule and the title
    -- (bands on whole pixels that touch but never overlap: overlaps show up as dark lines)
    local bands = 32
    local hb, ht = 190 * S / bands, 130 * S / bands
    for i = 0, bands - 1 do
      local a = 0.45 * v * (1 - (i + 0.5) / bands) ^ 2
      local y0, y1 = math.floor(H - (i + 1) * hb + 0.5), math.floor(H - i * hb + 0.5)
      box(ass, 0, y0, W, y1 - y0, 0, "000000", a)
      y0, y1 = math.floor(i * ht + 0.5), math.floor((i + 1) * ht + 0.5)
      box(ass, 0, y0, W, y1 - y0, 0, "000000", a)
    end

    -- top: back (glass circle) + title
    local b, hv = button("back", 18 * S, 16 * S, 50 * S, 50 * S, function() send("close") end)
    glass_circle(ass, b.x, b.y, b.w, v * (hv and 1 or 0.75), hv)
    icon(ass, b.x + b.w / 2, b.y + b.h / 2, 20 * S, G.back, v)
    shadow_text(ass, 82 * S, 18 * S, 7, 27 * S, esc(ud("title", prop("media-title", ""))), WHITE, v, FONT)
    local sub = ud("subtitle", "")
    if sub ~= "" then shadow_text(ass, 82 * S, 52 * S, 7, 19 * S, esc(sub), WHITE, 0.82 * v, FONT) end

    glass(ass, cap_x, cap_y, cap_w, cap_h, 26 * S, v)

    -- progress bar (inside the capsule, along its top)
    local pad = 26 * S
    local bx, bw = cap_x + pad, cap_w - 2 * pad
    local by = cap_y + 26 * S
    local bar = {x = bx, y = by - 12 * S, w = bw, h = 24 * S}
    state.bar = bar
    state.bar_hover = hit(bar) or state.scrub ~= nil
    local bh = (state.bar_hover and 6 or 4) * S
    local frac = dur > 0 and clamp((state.scrub or pos) / dur, 0, 1) or 0
    local cache = prop("demuxer-cache-time", pos) or pos
    local cfrac = dur > 0 and clamp(math.max(cache, pos) / dur, 0, 1) or 0
    box(ass, bx, by - bh / 2, bw, bh, bh / 2, "FFFFFF", 0.22 * v)
    box(ass, bx, by - bh / 2, bw * cfrac, bh, bh / 2, "FFFFFF", 0.3 * v)
    for _, sg in ipairs(ud("segments", {})) do  -- intro marks
      if dur > 0 then box(ass, bx + bw * sg.s / dur, by - bh / 2, bw * (sg.e - sg.s) / dur, bh, 0, "8AE0FF", 0.6 * v) end
    end
    if state.bar_hover then  -- hover preview of the part under the mouse
      local mx = clamp(state.mouse.x, bx, bx + bw)
      if mx > bx + bw * frac then box(ass, bx + bw * frac, by - bh / 2, mx - bx - bw * frac, bh, 0, "FFFFFF", 0.3 * v) end
    end
    box(ass, bx, by - bh / 2, math.max(bw * frac, bh), bh, bh / 2, RED, v)
    if dur > 0 then
      -- chapter breaks: small gaps in the bar, like YouTube's
      for i, c in ipairs(prop("chapter-list", {})) do
        if i > 1 and c.time > 0 and c.time < dur then
          box(ass, bx + bw * c.time / dur - 1.5 * S, by - bh / 2 - 1, 3 * S, bh + 2, 0, "000000", 0.75 * v)
        end
      end
      -- bookmarks: little pins above the bar
      for _, b in ipairs(ud("bookmarks", {})) do
        local x = bx + bw * clamp(b.ms / 1000 / dur, 0, 1)
        box(ass, x - 3.5 * S, by - bh / 2 - 11 * S, 7 * S, 7 * S, 3.5 * S, "8AE0FF", v, 1, "000000", 0.4 * v)
      end
      -- clip: the IN-OUT range and the saved slices, with handles
      if state.clip then
        local c = state.clip
        for _, sl in ipairs(c.slices) do
          box(ass, bx + bw * sl[1] / dur, by - bh / 2 - 2 * S, bw * (sl[2] - sl[1]) / dur, bh + 4 * S, 2 * S, "FFFFFF", 0.45 * v)
        end
        local a, b = c.inp, c.out
        if a and b and b > a then
          box(ass, bx + bw * a / dur, by - bh / 2 - 3 * S, bw * (b - a) / dur, bh + 6 * S, 3 * S, "3FD7FF", 0.85 * v)
        end
        for _, h in ipairs({{"in", a}, {"out", b}}) do
          if h[2] then
            local x = bx + bw * h[2] / dur
            box(ass, x - 3 * S, by - 14 * S, 6 * S, 28 * S, 3 * S, "3FD7FF", v, 1, WHITE, 0.9 * v)
            table.insert(state.buttons, {id = "clip-" .. h[1], x = x - 9 * S, y = by - 16 * S, w = 18 * S, h = 32 * S,
              handle = h[1]})
          end
        end
      end
    end
    local dot = (state.bar_hover and 7.5 or 0) * S
    if dot > 0 then box(ass, bx + bw * frac - dot, by - dot, dot * 2, dot * 2, dot, RED, v) end
    if state.bar_hover and dur > 0 then
      local mx = clamp(state.scrub and (bx + bw * frac) or state.mouse.x, bx, bx + bw)
      local ht = dur * (mx - bx) / bw
      if state.clip and state.clip.drag then ht = state.clip[state.clip.drag == "in" and "inp" or "out"] or ht end
      local tip = fmt(ht)
      local chap = nil
      for _, c in ipairs(prop("chapter-list", {})) do if c.time <= ht then chap = c.title end end
      if chap and chap ~= "" then tip = tip .. "  ·  " .. chap end
      for _, b in ipairs(ud("bookmarks", {})) do
        if math.abs(bx + bw * b.ms / 1000 / dur - mx) < 6 * S then
          tip = fmt(b.ms / 1000) .. "  ·  " .. ((b.note or "") ~= "" and b.note or (b.tag ~= "" and b.tag or "Bookmark"))
        end
      end
      local lift = state.clip and state.clip_h or 0
      local tw = (ulen(tip) * 18 * 0.5 + 30) * S
      local tx = clamp(mx - tw / 2, cap_x, cap_x + cap_w - tw)
      glass(ass, tx, cap_y - 46 * S - lift, tw, 34 * S, 17 * S, v, {tint = 0.62})
      text(ass, tx + tw / 2, cap_y - 29 * S - lift, 5, 18 * S, esc(tip), WHITE, v, FONT)
      -- the preview frame: a glass frame here, the picture itself is an overlay the app puts inside it
      local aspect = prop("video-params/aspect", 16 / 9) or 16 / 9
      local pw = math.floor(232 * S / 2) * 2
      local ph = math.floor(pw / aspect / 2) * 2
      local pad = math.floor(5 * S)
      local px = math.floor(clamp(mx - pw / 2, cap_x + pad, cap_x + cap_w - pw - pad))
      local py = math.floor(cap_y - 46 * S - 10 * S - ph - (state.clip and state.clip_h or 0))
      if v > 0.9 and not state.menu and prop("video-params/w", 0) > 0 then
        glass(ass, px - pad, py - pad, pw + 2 * pad, ph + 2 * pad, 14 * S, v, {tint = 0.6})
        local key = string.format("%.1f %d %d %d %d", ht, px, py, pw, ph)
        if key ~= state.thumb_key then
          state.thumb_key = key
          mp.commandv("script-message", "marquee", "thumb", string.format("%.2f", ht), tostring(px), tostring(py),
            tostring(pw), tostring(ph), tostring(math.floor(10 * S)))
        end
        state.thumb_on = true
      end
    end

    -- control row (left)
    local cy = cap_y + 66 * S
    local sz = 44 * S
    local x = cap_x + 12 * S
    local function ctl(id, glyph, size, action)
      local bb, hov = button(id, x, cy - sz / 2, sz, sz, action)
      if hov then glass_circle(ass, bb.x, bb.y, sz, v, true) end
      icon(ass, bb.x + sz / 2, cy, size, glyph, v, WHITE)
      x = x + sz + 4 * S
      return bb
    end
    ctl("play", paused and G.play or G.pause, 22 * S, toggle_pause)
    if ud("has_next", false) then ctl("next", G.next, 19 * S, function() send("next") end) end
    -- volume: icon + slider that slides open on hover
    local volb = {x = x, y = cy - sz / 2, w = sz + state.vol_open * 108 * S, h = sz}
    local vol_hover = hit(volb) or state.vol_drag
    state.vol_open = lerp(state.vol_open, vol_hover and 1 or 0, 0.25)
    if state.vol_open < 0.01 then state.vol_open = 0 end
    local vol, muted = prop("volume", 100), prop("mute", false)
    local vg = (muted or vol <= 0) and G.mute or (vol < 34 and G.vol1 or (vol < 67 and G.vol2 or G.vol))
    ctl("mute", vg, 19 * S, function() mp.commandv("cycle", "mute") end)
    x = x - 4 * S
    if state.vol_open > 0 then
      local sw = 92 * S * state.vol_open
      local sx = x + 6 * S
      local va = v * state.vol_open
      box(ass, sx, cy - 2 * S, sw, 4 * S, 2 * S, "FFFFFF", 0.3 * va)
      local vf = muted and 0 or clamp(vol / 100, 0, 1)
      box(ass, sx, cy - 2 * S, sw * vf, 4 * S, 2 * S, WHITE, va)
      box(ass, sx + sw * vf - 6.5 * S, cy - 6.5 * S, 13 * S, 13 * S, 6.5 * S, WHITE, va)
      table.insert(state.buttons, {id = "volslider", x = sx - 6 * S, y = cy - 14 * S, w = sw + 12 * S, h = 28 * S, slider = true,
        sx = sx, sw = sw})
      x = x + sw + 16 * S
    else x = x + 6 * S end
    text(ass, x + 4 * S, cy, 4, 18 * S, fmt(pos) .. "  /  " .. fmt(dur), WHITE, 0.95 * v, FONT)

    -- right side: fullscreen, settings, captions (right to left)
    local rx = cap_x + cap_w - 12 * S - sz
    local function rctl(id, glyph, size, action, active, extra, lit)
      local bb, hov = button(id, rx, cy - sz / 2, sz, sz, action)
      if hov or lit then glass_circle(ass, bb.x, bb.y, sz, v, true) end
      icon(ass, bb.x + sz / 2, cy, size, glyph, v, WHITE, extra)
      if active then box(ass, bb.x + sz * 0.3, cy + 14 * S, sz * 0.4, 3 * S, 1.5 * S, RED, v) end
      rx = rx - sz - 4 * S
      return bb
    end
    rctl("full", ud("fullscreen", false) and G.unfull or G.full, 18 * S, function() send("fullscreen") end)
    rctl("pip", G.pip, 19 * S, function() send("pip") end)
    if ud("cast_on", false) then
      rctl("cast", G.cast, 19 * S, function() open_menu("cast") end, false, nil, state.menu == "cast")
    end
    local spin = clamp((t - state.gear_spin) / 0.5, 0, 1)
    local gear_menu = state.menu and state.menu ~= "captions" and state.menu ~= "cast"
    local deg = gear_menu and ease(spin) * 90 or (1 - ease(spin)) * 90
    rctl("settings", G.gear, 20 * S, function() open_menu("settings") end, false,
      string.format("\\frz%.1f", -deg), gear_menu)
    local sid = prop("sid", false)
    rctl("captions", G.cc, 20 * S, function() open_menu("captions") end, sid ~= "no" and sid ~= false, nil,
      state.menu == "captions")
    rctl("atlas", G.sparkle, 20 * S, function() ask_atlas() end, false, nil, state.atlas ~= nil)
  else
    state.bar_hover = false
  end

  -- hold-to-2x pill (top centre)
  if state.held then
    local pw, ph = 112 * S, 40 * S
    glass(ass, W / 2 - pw / 2, 24 * S, pw, ph, ph / 2, 1, {tint = 0.6, noshader = true})
    text(ass, W / 2 - 14 * S, 24 * S + ph / 2, 5, 21 * S, "2x", WHITE, 1, FONT)
    icon(ass, W / 2 + 20 * S, 24 * S + ph / 2, 18 * S, G.ff, 1)
  end

  -- centre flash (play/pause) and seek ripples
  if state.flash then
    local k = (t - state.flash_t) / 0.65
    if k < 1 then
      local r = lerp(36, 58, ease(k)) * S
      glass(ass, W / 2 - r, H / 2 - r, r * 2, r * 2, r, 1 - k, {tint = 0.55, flat = true})
      icon(ass, W / 2, H / 2, r * 0.8, state.flash, 1 - k)
    else state.flash = nil end
  end
  if state.seek_flash then
    local k = (t - state.seek_t) / 0.8
    if k < 1 then
      local fwd = state.seek_flash > 0
      local cx = fwd and W * 0.8 or W * 0.2
      local r = lerp(60, 130, ease(math.min(1, k * 1.4))) * S * (state.pip and 0.5 or 1)
      box(ass, cx - r, H / 2 - r, r * 2, r * 2, r, "FFFFFF", 0.16 * (1 - k))
      local a = k < 0.6 and 1 or (1 - (k - 0.6) / 0.4)
      -- three chevrons lighting up in turn, like YouTube's double-tap ripple
      local glyph = fwd and G.chev or G.chevl
      for i = 0, 2 do
        local lit = ((t - state.seek_t) * 6 - i) % 3 < 1
        local gx = cx + (fwd and (i - 1) or (1 - i)) * 16 * S
        icon(ass, gx, H / 2 - 14 * S, 20 * S, glyph, a * (lit and 1 or 0.4))
      end
      shadow_text(ass, cx, H / 2 + 20 * S, 5, 19 * S, math.abs(state.seek_flash) .. " seconds", WHITE, a, FONT)
    else state.seek_flash = nil; state.seek_acc = 0 end
  end

  -- volume indicator (keys / wheel)
  local vk = (t - state.vol_flash_t) / 0.9
  if vk < 1 then
    local a = vk < 0.7 and 1 or (1 - (vk - 0.7) / 0.3)
    local vol, muted = prop("volume", 100), prop("mute", false)
    local label = muted and "Muted" or string.format("%d%%", math.floor(vol + 0.5))
    local pw, ph = (ulen(label) * 11 + 66) * S, 44 * S
    glass(ass, W / 2 - pw / 2, H * 0.14, pw, ph, ph / 2, a, {tint = 0.6, noshader = true})
    icon(ass, W / 2 - pw / 2 + 26 * S, H * 0.14 + ph / 2, 18 * S, muted and G.mute or G.vol, a)
    text(ass, W / 2 + 14 * S, H * 0.14 + ph / 2, 5, 19 * S, label, WHITE, a, FONT)
  end

  -- toast (quality / upscaler changes)
  if state.toast then
    local k = (t - state.toast_t) / 3.2
    if k < 1 then
      local a = k < 0.08 and k / 0.08 or (k < 0.85 and 1 or (1 - (k - 0.85) / 0.15))
      local tw = (ulen(state.toast) * 18 * 0.4 + 56) * S
      local ty = H * 0.13 - (1 - ease(math.min(1, k / 0.08))) * 10 * S
      glass(ass, W / 2 - tw / 2, ty, tw, 44 * S, 22 * S, a, {tint = 0.62, noshader = true})
      text(ass, W / 2, ty + 22 * S, 5, 18 * S, esc(state.toast), WHITE, a, FONT)
    else state.toast = nil end
  end

  -- skip intro pill (shown even while the controls are hidden, like YouTube's skip button)
  local intro = ud("intro", nil)
  if intro and not state.pip and pos >= intro.s and pos < intro.e - 1 then
    local pw, ph = 156 * S, 46 * S
    local px, py = W - pw - m, state.above - ph
    local b, hv = button("skip", px, py, pw, ph, function() mp.commandv("seek", tostring(intro.e), "absolute", "exact") end)
    if hv then box(ass, px, py, pw, ph, ph / 2, "FFFFFF", 0.95, 1, WHITE, 0.6)
    else glass(ass, px, py, pw, ph, ph / 2, 1, {tint = 0.6}) end
    text(ass, px + pw / 2 - 10 * S, py + ph / 2, 5, 19 * S, "Skip intro", hv and GLASS or WHITE, 1, FONT)
    icon(ass, px + pw - 26 * S, py + ph / 2, 14 * S, G.next, 1, hv and GLASS or WHITE)
  end

  -- skip to the scene after the credits (shown during the credits, until a few seconds before that scene)
  local ac, cat = ud("after_credits", nil), ud("credits_at", nil)
  if ac and cat and #ac > 0 and not state.pip and not state.mini and pos >= cat then
    local target
    for _, t in ipairs(ac) do if pos < t - 5 then target = t; break end end
    if target then
      local pw, ph = 268 * S, 46 * S
      local px, py = W - pw - m, state.above - ph
      local b, hv = button("aftercredits", px, py, pw, ph, function() mp.commandv("seek", tostring(target), "absolute", "exact") end)
      if hv then box(ass, px, py, pw, ph, ph / 2, "FFFFFF", 0.95, 1, WHITE, 0.6)
      else glass(ass, px, py, pw, ph, ph / 2, 1, {tint = 0.6}) end
      text(ass, px + pw / 2 - 12 * S, py + ph / 2, 5, 18 * S, "Skip to after-credits scene", hv and GLASS or WHITE, 1, FONT)
      icon(ass, px + pw - 24 * S, py + ph / 2, 14 * S, G.next, 1, hv and GLASS or WHITE)
    end
  end

  -- next episode card near the end (credits if Jellyfin knows them, else the last 30 s)
  local nxt = ud("next", nil)
  if nxt and dur > 60 and not state.next_cancelled and not state.pip then
    local hidden_card = state.mini
    local start = (nxt.credits and nxt.credits > 0) and nxt.credits or (dur - 30)
    if pos >= start and pos < dur - 0.2 then
      local autoplay = ud("autoplay", true)
      local left = math.max(0, math.ceil(10 - (pos - start)))
      local cw, ch = 360 * S, 112 * S
      local cx, cyy = W - cw - m, state.above - ch
      local k = hidden_card and 0 or ease(clamp((pos - start) / 0.4, 0, 1))  -- in the tile: countdown only
      cx = cx + (1 - k) * 30 * S
      glass(ass, cx, cyy, cw, ch, 22 * S, k, {tint = 0.62})
      text(ass, cx + 18 * S, cyy + 16 * S, 7, 16 * S, autoplay and ("Next episode in " .. left) or "Next episode", WHITE, 0.75 * k, FONT)
      text(ass, cx + 18 * S, cyy + 40 * S, 7, 20 * S, esc(nxt.title or ""), WHITE, k, FONT, "\\q2")
      if autoplay then  -- countdown line
        box(ass, cx + 18 * S, cyy + 34 * S, (cw - 36 * S), 2 * S, 1 * S, "FFFFFF", 0.2 * k)
        box(ass, cx + 18 * S, cyy + 34 * S, (cw - 36 * S) * clamp((pos - start) / 10, 0, 1), 2 * S, 1 * S, RED, k)
      end
      local pb, phov = button("playnext", cx + cw - 128 * S, cyy + ch - 46 * S, 110 * S, 34 * S, function() send("next") end)
      box(ass, pb.x, pb.y, pb.w, pb.h, 17 * S, phov and "FFFFFF" or RED, k)
      icon(ass, pb.x + 20 * S, pb.y + pb.h / 2, 13 * S, G.play, k, phov and "000000" or WHITE)
      text(ass, pb.x + pb.w / 2 + 10 * S, pb.y + pb.h / 2, 5, 16 * S, "Play now", phov and "000000" or WHITE, k, FONT)
      local xb, xhov = button("nextcancel", cx + 10 * S, cyy + ch - 46 * S, 92 * S, 34 * S, function() state.next_cancelled = true end)
      if xhov then glass(ass, xb.x, xb.y, xb.w, xb.h, 17 * S, k, {flat = true, tint = 0.2, white = 0.12}) end
      text(ass, xb.x + xb.w / 2, xb.y + xb.h / 2, 5, 16 * S, "Cancel", WHITE, 0.85 * k, FONT)
      if autoplay and left <= 0 and not state.next_sent then state.next_sent = true; send("next") end
    end
  end

  -- the clip bar: above the capsule while Clip mode is on
  if state.clip and not state.pip and v > 0.01 then
    local c = state.clip
    local ch = state.clip_h - 8 * S
    local cx, cw = m, W - 2 * m
    local cyy = cap_y - 10 * S - ch
    glass(ass, cx, cyy, cw, ch, 22 * S, v, {tint = 0.66, keep = 0.6})
    local ry = cyy + 29 * S
    icon(ass, cx + 26 * S, ry, 18 * S, G.scissors, v)
    local inout = (c.inp and fmt(c.inp) or "--:--") .. "  →  " .. (c.out and fmt(c.out) or "--:--")
    if c.inp and c.out and c.out > c.inp then inout = inout .. string.format("   (%.1f s)", c.out - c.inp) end
    text(ass, cx + 46 * S, ry, 4, 17 * S, "Clip  " .. inout, WHITE, v, FONT)
    if c.status then text(ass, cx + 46 * S + (ulen(inout) + 7) * 17 * 0.5 * S + 24 * S, ry, 4, 15 * S, esc(c.status), WHITE, 0.7 * v, FONT) end
    -- buttons, right to left
    local bx2 = cx + cw - 16 * S
    local function cbtn(id, label, action, on, accent)
      local w = (ulen(label) * 15 * 0.52 + 26) * S
      bx2 = bx2 - w
      local b, hv = button(id, bx2, ry - 15 * S, w, 30 * S, action)
      box(ass, b.x, b.y, b.w, b.h, 15 * S, accent and RED or "FFFFFF", (accent and 1 or (on and 0.34 or (hv and 0.22 or 0.1))) * v, 1, WHITE, (on and 0.6 or 0.25) * v)
      text(ass, b.x + w / 2, ry, 5, 15 * S, label, WHITE, v, FONT)
      bx2 = bx2 - 8 * S
    end
    cbtn("clip-close", "Done", function() clip_mode(false) end)
    cbtn("clip-save", c.saving and "Saving…" or "Save", function() clip_save() end, false, not c.saving)
    cbtn("clip-join", "Join", function() c.join = not c.join end, c.join)
    cbtn("clip-gif", "GIF", function() c.gif = not c.gif end, c.gif)
    cbtn("clip-add", "+ Slice", function() clip_add() end)
    cbtn("clip-out", "OUT  (O)", function() clip_set("out") end)
    cbtn("clip-in", "IN  (I)", function() clip_set("in") end)
    -- the slices as chips: length, preview-loop, remove
    if c.slices[1] then
      local sx = cx + 22 * S
      local sy = cyy + 56 * S
      for i, sl in ipairs(c.slices) do
        local label = string.format("%d · %s–%s  %.1f s", i, fmt(sl[1]), fmt(sl[2]), sl[2] - sl[1])
        local w = (ulen(label) * 15 * 0.5 + 76) * S
        if sx + w > cx + cw - 16 * S then break end
        local looping = c.loop == i
        box(ass, sx, sy, w, 32 * S, 16 * S, "FFFFFF", looping and 0.3 or 0.12, 1, WHITE, 0.3)
        text(ass, sx + 14 * S, sy + 16 * S, 4, 15 * S, label, WHITE, 0.95, FONT)
        local pb = button("clip-loop" .. i, sx + w - 58 * S, sy + 3 * S, 26 * S, 26 * S, function() clip_loop(i) end)
        icon(ass, pb.x + 13 * S, pb.y + 13 * S, 12 * S, looping and G.stop or G.loop, 0.9)
        local xb = button("clip-del" .. i, sx + w - 30 * S, sy + 3 * S, 26 * S, 26 * S, function()
          table.remove(c.slices, i); if c.loop == i then clip_loop(nil) end end)
        icon(ass, xb.x + 13 * S, xb.y + 13 * S, 11 * S, G.close, 0.9)
        sx = sx + w + 8 * S
      end
    end
    -- export progress
    if c.saving and c.progress then
      box(ass, cx + 20 * S, cyy + ch - 7 * S, (cw - 40 * S), 3 * S, 1.5 * S, "FFFFFF", 0.2 * v)
      box(ass, cx + 20 * S, cyy + ch - 7 * S, (cw - 40 * S) * c.progress, 3 * S, 1.5 * S, RED, v)
    end
  end
  -- after saving: where the files went, with Open folder
  if state.clip_done then
    local d = state.clip_done
    local k = (t - d.t) / 9
    if k < 1 then
      local a = math.min(1, (t - d.t) / 0.25) * (k < 0.9 and 1 or (1 - (k - 0.9) / 0.1))
      local bw = math.min(680 * S, W * 0.6)
      local bh = 54 * S
      local bx3, by3 = W / 2 - bw / 2, H * 0.13 + 52 * S
      glass(ass, bx3, by3, bw, bh, 27 * S, a, {tint = 0.7, noshader = true})
      icon(ass, bx3 + 28 * S, by3 + bh / 2, 16 * S, d.folder ~= "" and G.check or G.close, a)
      text(ass, bx3 + 50 * S, by3 + bh / 2, 4, 16 * S, esc(ellipsize(d.text, math.floor((bw - 230 * S) / (16 * S * 0.5)))), WHITE, a, FONT)
      if d.folder ~= "" then
        local ob, ohv = button("clip-open", bx3 + bw - 150 * S, by3 + 11 * S, 136 * S, 32 * S, function() send("clip-open", d.folder) end)
        box(ass, ob.x, ob.y, ob.w, ob.h, 16 * S, ohv and "FFFFFF" or RED, a)
        icon(ass, ob.x + 22 * S, ob.y + ob.h / 2, 13 * S, G.folder, a, ohv and "000000" or WHITE)
        text(ass, ob.x + ob.w / 2 + 12 * S, ob.y + ob.h / 2, 5, 15 * S, "Open folder", ohv and "000000" or WHITE, a, FONT)
      end
    else state.clip_done = nil end
  end

  -- the bookmark prompt: type a note, pick a tag
  if state.prompt and state.prompt.kind == "ask" then
    local pr = state.prompt
    local pw = math.min(640 * S, W * 0.8)
    local ph = 128 * S
    local px, py = W / 2 - pw / 2, H / 2 - ph / 2
    glass(ass, px, py, pw, ph, 22 * S, 1, {tint = 0.76, keep = 0.8})
    icon(ass, px + 28 * S, py + 30 * S, 16 * S, G.sparkle, 1)
    text(ass, px + 50 * S, py + 30 * S, 4, 18 * S, "Ask about this moment", WHITE, 1, FONT)
    box(ass, px + 20 * S, py + 52 * S, pw - 40 * S, 40 * S, 12 * S, "FFFFFF", 0.1, 1, WHITE, 0.35)
    local shown = pr.text ~= "" and pr.text or "Who's that? · Go to the part where... · Is there a jumpscare soon?"
    local caret = (math.floor(t * 2) % 2 == 0) and "|" or " "
    text(ass, px + 34 * S, py + 72 * S, 4, 17 * S, esc(ellipsize(shown, math.floor((pw - 80 * S) / (17 * S * 0.5)))) .. (pr.text ~= "" and caret or ""),
      WHITE, pr.text ~= "" and 1 or 0.5, FONT)
    text(ass, px + pw - 20 * S, py + 110 * S, 6, 14 * S, "Enter ask (empty: what's on screen) · Esc cancel · F8 to speak", WHITE, 0.55, FONT)
  elseif state.prompt then
    local pr = state.prompt
    local pw = math.min(560 * S, W * 0.7)
    local ph = 156 * S
    local px, py = W / 2 - pw / 2, H / 2 - ph / 2
    glass(ass, px, py, pw, ph, 22 * S, 1, {tint = 0.76, keep = 0.8})
    icon(ass, px + 28 * S, py + 30 * S, 16 * S, G.bookmark, 1)
    text(ass, px + 50 * S, py + 30 * S, 4, 18 * S, "Bookmark at " .. fmt(pr.ms / 1000), WHITE, 1, FONT)
    -- the note field
    box(ass, px + 20 * S, py + 52 * S, pw - 40 * S, 40 * S, 12 * S, "FFFFFF", 0.1, 1, WHITE, 0.35)
    local shown = pr.text ~= "" and pr.text or "Add a note (optional) · #tag for your own tag"
    local caret = (math.floor(t * 2) % 2 == 0) and "|" or " "
    text(ass, px + 34 * S, py + 72 * S, 4, 17 * S, esc(ellipsize(shown, math.floor((pw - 80 * S) / (17 * S * 0.5)))) .. (pr.text ~= "" and caret or ""),
      WHITE, pr.text ~= "" and 1 or 0.5, FONT)
    -- tags
    local tx3 = px + 20 * S
    local custom = pr.text:match("#(%S+)")
    local list = {}
    for _, tg in ipairs(TAGS) do table.insert(list, tg) end
    if custom then table.insert(list, custom) end
    for i, tg in ipairs(list) do
      local w = (ulen(tg) * 15 * 0.52 + 28) * S
      local on = pr.tag == tg or (custom and tg == custom and pr.tag == "#")
      local b, hv = button("tag" .. i, tx3, py + 108 * S, w, 30 * S, function() pr.tag = (custom and tg == custom) and "#" or (pr.tag == tg and "" or tg) end)
      box(ass, b.x, b.y, b.w, b.h, 15 * S, on and RED or "FFFFFF", on and 1 or (hv and 0.2 or 0.1), 1, WHITE, 0.3)
      text(ass, b.x + w / 2, b.y + 15 * S, 5, 15 * S, esc(tg), WHITE, 1, FONT)
      tx3 = tx3 + w + 8 * S
    end
    text(ass, px + pw - 20 * S, py + 123 * S, 6, 14 * S, "Enter save · Esc cancel · Tab tag", WHITE, 0.55, FONT)
  end

  -- Atlas: "thinking" pill, then the answer as a quiet glass banner at the top (shown even with the HUD hidden)
  if state.atlas then
    local at = state.atlas
    local age = t - at.t
    local top = (v > 0.05 and not state.pip) and (78 * S * v + 14 * S * (1 - v)) or 14 * S
    if at.state == "thinking" and age > 45 then state.atlas = nil  -- no answer came
    elseif at.state == "thinking" then
      local a = math.min(1, age / 0.25)
      local label = "Thinking"
      local pw, ph = (ulen(label) * 17 * 0.5 + 92) * S, 42 * S
      local px = W / 2 - pw / 2
      glass(ass, px, top, pw, ph, ph / 2, a, {tint = 0.62, noshader = true})
      -- a sheen sweeping across the pill
      local sweep = (age * 0.8) % 1
      box(ass, px + pw * sweep - 30 * S, top + 2, 60 * S, ph - 4, 0, "FFFFFF", 0.08 * a, 0, nil, 0,
        string.format("\\blur%.1f\\clip(%.1f,%.1f,%.1f,%.1f)", 14 * S, px, top, px + pw, top + ph))
      icon(ass, px + 24 * S, top + ph / 2, 16 * S, G.sparkle, a, nil,
        string.format("\\frz%.1f", math.sin(age * 4) * 12))
      local dots = string.rep(".", 1 + math.floor(age * 2.5) % 3)
      text(ass, px + 44 * S, top + ph / 2, 4, 17 * S, label .. dots, WHITE, a, FONT)
    elseif at.state == "answer" then
      local k = age / at.dur
      if k < 1 then
        local a = math.min(1, age / 0.3) * (k < 0.9 and 1 or (1 - (k - 0.9) / 0.1))
        local bw = math.min(720 * S, W * 0.62)
        local fs = 18 * S
        local lines = wrap(at.text, math.max(16, math.floor((bw - 80 * S) / (fs * 0.44))), 6)
        local lh = fs * 1.38
        local bh = 26 * S + #lines * lh
        local bx = W / 2 - bw / 2
        local by = top - (1 - math.min(1, age / 0.3)) * 10 * S
        glass(ass, bx, by, bw, bh, 20 * S, a, {tint = 0.7, keep = 0.75})
        icon(ass, bx + 26 * S, by + 13 * S + lh / 2, 16 * S, G.sparkle, a)
        for i, ln in ipairs(lines) do
          text(ass, bx + 50 * S, by + 13 * S + (i - 0.5) * lh, 4, fs, esc(ln), WHITE, 0.92 * a, FONT)
        end
        -- the whole banner is the dismiss button
        button("atlas-banner", bx, by, bw, bh, function() state.atlas = nil end)
        -- after a scene jump: an Undo pill on the right (B does the same while it shows)
        local su = state.scene_undo
        if su and t - su.t < su.dur then
          local uw, uh = 104 * S, 30 * S
          local ux, uy = bx + bw - uw - 12 * S, by + bh / 2 - uh / 2
          local ub, uhv = button("scene-undo", ux, uy, uw, uh, scene_undo_now)
          box(ass, ub.x, ub.y, ub.w, ub.h, uh / 2, "FFFFFF", uhv and 0.3 or 0.16, 1, WHITE, 0.35 * a)
          text(ass, ub.x + uw / 2, ub.y + uh / 2, 5, 15 * S, "Undo  ·  B", WHITE, a, FONT)
        end
      else state.atlas = nil end
    end
  end

  -- scene picker: "Which one?" with up to 3 thumbnails (pictures are mpv overlays placed inside the glass tiles)
  if state.scene_pick then
    local sp = state.scene_pick
    local age = t - sp.t
    if age > 45 then scene_pick_close(0)
    else
      local a = math.min(1, age / 0.25)
      local n = #sp.items
      local tw, th = sp.w, sp.h
      local pad, gap = 14 * S, 14 * S
      local cw = n * tw + (n - 1) * gap + 2 * pad
      local ch = 46 * S + th + 56 * S
      local cx = W / 2 - cw / 2
      local cy = (v > 0.05 and not state.pip) and (78 * S * v + 14 * S * (1 - v)) or 14 * S
      glass(ass, cx, cy, cw, ch, 22 * S, a, {tint = 0.7, keep = 0.75})
      icon(ass, cx + pad + 8 * S, cy + 24 * S, 16 * S, G.sparkle, a)
      text(ass, cx + pad + 28 * S, cy + 24 * S, 4, 18 * S, esc(sp.title), WHITE, 0.95 * a, FONT)
      text(ass, cx + cw - pad, cy + 24 * S, 6, 14 * S, "1 · 2 · 3  or click  ·  Esc", WHITE, 0.55 * a, FONT)
      for i, it in ipairs(sp.items) do
        local x = math.floor(cx + pad + (i - 1) * (tw + gap))
        local y = math.floor(cy + 46 * S)
        local b, hv = button("scene-pick" .. i, x, y, tw, th + 48 * S, function() scene_pick_close(i) end)
        box(ass, x - 3, y - 3, tw + 6, th + 6, 12 * S, "FFFFFF", hv and 0.5 or 0.18, 0)
        if it.file ~= "" and (not it.placed or it.placed[1] ~= x or it.placed[2] ~= y) then
          mp.commandv("overlay-add", tostring(19 + i), tostring(x), tostring(y), it.file, "0", "bgra",
            tostring(tw), tostring(th), tostring(tw * 4))
          it.placed = {x, y}
        end
        -- number badge, time and caption under the picture
        box(ass, x + 8 * S, y + 8 * S, 26 * S, 26 * S, 13 * S, "000000", 0.55 * a, 0)
        text(ass, x + 21 * S, y + 21 * S, 5, 15 * S, tostring(i), WHITE, a, FONT)
        text(ass, x + 2, y + th + 16 * S, 4, 16 * S, esc(it.label), WHITE, a, FONT)
        local cap = it.cap or ""
        if ulen(cap) > 34 then cap = cap:sub(1, 33) .. "…" end
        text(ass, x + 2, y + th + 36 * S, 4, 13 * S, esc(cap), WHITE, 0.6 * a, FONT)
      end
    end
  end

  -- an assistant card (Player.show_card): glass, top right, word-wrapped, fades out
  if state.card then
    local c = state.card
    local k = (t - c.t) / c.dur
    if k < 1 then
      local a = math.min(1, (t - c.t) / 0.2) * (k < 0.92 and 1 or (1 - (k - 0.92) / 0.08))
      local cw = math.min(560 * S, W * 0.46)
      local fs = 18 * S
      local per = math.max(12, math.floor((cw - 44 * S) / (fs * 0.44)))
      local lines = {}
      for para in (c.text .. "\n"):gmatch("(.-)\n") do
        local line = ""
        for word in para:gmatch("%S+") do
          if line == "" then line = word
          elseif ulen(line) + 1 + ulen(word) <= per then line = line .. " " .. word
          else table.insert(lines, line); line = word end
        end
        table.insert(lines, line)
      end
      while #lines > 12 do table.remove(lines) end
      local lh = fs * 1.35
      local ch = 58 * S + #lines * lh + 14 * S
      local cx, cyy = W - m - cw, 86 * S - (1 - a) * 8 * S
      glass(ass, cx, cyy, cw, ch, 20 * S, a, {tint = 0.72, keep = 0.75})
      text(ass, cx + 22 * S, cyy + 18 * S, 7, 20 * S, esc(c.title), WHITE, a, FONT)
      for i, ln in ipairs(lines) do
        text(ass, cx + 22 * S, cyy + 52 * S + (i - 1) * lh, 7, fs, esc(ln), WHITE, 0.88 * a)
      end
      local xb, xhv = button("card-close", cx + cw - 42 * S, cyy + 10 * S, 32 * S, 32 * S, function() state.card = nil end)
      if xhv then glass_circle(ass, xb.x, xb.y, xb.w, a, true) end
      icon(ass, xb.x + xb.w / 2, xb.y + xb.h / 2, 12 * S, G.close, 0.85 * a)
    else state.card = nil end
  end

  -- menus (glass panels sliding up above the settings button)
  if state.menu then
    local k = ease(clamp((t - state.menu_anim) / 0.2, 0, 1))
    draw_menu(ass, state.menu, k, m, true)
  else
    state.rows = {}
    if state.closing_menu then
      local k = 1 - clamp((t - state.closing_t) / 0.14, 0, 1)
      if k > 0 then draw_menu(ass, state.closing_menu, k, m, false) else state.closing_menu = nil end
    end
  end

  if state.thumb_on and not (state.bar_hover and v > 0.9 and not state.menu and not state.pip) then
    state.thumb_on = false; state.thumb_key = nil
    send("thumb-hide")
  end
  if ass.text ~= state.last_ass then
    state.last_ass = ass.text
    ov.data = ass.text
    ov:update()
  end
  push_glass()
  -- captions step up out of the way of the controls (like YouTube), and back down when they hide
  local lift = state.pip and 0 or math.floor((H - cap_y + 8 * S + (state.clip_h or 0)) / H * 100 * ease(v) + 0.5)
  if lift ~= state.sub_lift then
    state.sub_lift = lift
    mp.set_property_number("sub-pos", 100 - lift)
  end
end

-- ------------------------------------------------------------------------------- input

local function set_volume(vol)
  mp.set_property_number("volume", clamp(math.floor(vol + 0.5), 0, 100))
  if vol > 0 then mp.set_property_bool("mute", false) end
end

local function nudge_volume(d)
  set_volume(prop("volume", 100) + d)
  state.vol_flash_t = now()
end

local function slider_at(b)
  set_volume(100 * clamp((state.mouse.x - b.sx) / b.sw, 0, 1))
end

local function click_at()
  for _, r in ipairs(state.rows) do
    if hit(r) then
      local row = r.row
      if row.disabled then return true end
      if row.back then state.menu = row.back; state.menu_anim = now()
      elseif row.sub then state.menu = row.sub; state.menu_anim = now()
      elseif row.remove and row.remove_box and hit(row.remove_box) then
        send("bookmark-remove", row.remove)
      elseif row.stepper then
        if row.minus and hit(row.minus) then set_speed(prop("speed", 1) - 0.05)
        elseif row.plus and hit(row.plus) then set_speed(prop("speed", 1) + 0.05) end
      elseif row.act then
        row.act()
        if not (row.toggle or row.cycle) then close_menu() end
      end
      return true
    end
  end
  for i = #state.buttons, 1, -1 do
    local b = state.buttons[i]
    if hit(b) then
      if b.slider then state.vol_drag = b; slider_at(b)
      elseif b.handle and state.clip then state.clip.drag = b.handle
      elseif b.action then b.action() end
      return true
    end
  end
  if state.visible > 0.05 and state.bar and hit(state.bar) then
    local dur = prop("duration", 0)
    state.scrub = dur * clamp((state.mouse.x - state.bar.x) / state.bar.w, 0, 1)
    return true
  end
  return false
end

local down_at, down_on_video = nil, false
local on_left
on_left = function(e)
  poke()
  if e.event == "press" then -- a synthetic click (down and up in one)
    on_left({event = "down"}); on_left({event = "up"})
    return
  end
  if e.event == "down" and state.mini then  -- the credits tile: a click brings the video back
    send("restore"); state.next_cancelled = true  -- keep watching the credits
    down_at = nil
    return
  end
  if e.event == "up" and state.mini then down_at = nil; return end
  if e.event == "down" then
    send("focus")  -- clicking the video gives it the keyboard (Windows doesn't do that for child windows)
    if state.pip then
      state.pip_moved = false
      if click_at() then down_at = nil; down_on_video = false; return end
      -- corners resize, anywhere else drags (once the mouse actually moves)
      -- corners (a bigger grab zone) and edges (12 px) resize; Shift while resizing = any shape
      local mx, my, c, e = state.mouse.x, state.mouse.y, 20 * clamp(H / 300, 0.6, 1.6), 12
      local vert = (my < c and "t" or (my > H - c and "b" or nil))
      local horz = (mx < c and "l" or (mx > W - c and "r" or nil))
      local zone = nil
      if vert and horz then zone = vert .. horz
      elseif mx < e then zone = "l" elseif mx > W - e then zone = "r"
      elseif my < e then zone = "t" elseif my > H - e then zone = "b" end
      if zone then
        state.pip_moved = true
        send("pip-resize", zone)
        down_at = nil; down_on_video = false
        return
      end
      state.pip_press = {x = mx, y = my}
      down_at = now(); down_on_video = true
      return
    end
    if state.menu then
      if not click_at() then close_menu() end
      down_at = nil
      return
    end
    down_on_video = not click_at()
    down_at = now()
    if down_on_video and not state.pip then
      -- press and hold anywhere on the picture: 2x speed until released (like YouTube)
      if state.hold_t then state.hold_t:kill() end
      state.hold_t = mp.add_timeout(0.45, function()
        state.hold_t = nil
        if down_at and not prop("pause", false) then
          state.held = true; state.prev_speed = prop("speed", 1); mp.set_property_number("speed", 2)
        end
      end)
    end
  elseif e.event == "up" then
    state.pip_press = nil
    if state.pip_moved then state.pip_moved = false; down_at = nil; return end
    if state.clip and state.clip.drag then  -- show the exact frame where the handle landed
      local tt = state.clip[state.clip.drag == "in" and "inp" or "out"]
      state.clip.drag = nil
      if state.clip.inp and state.clip.out and state.clip.out < state.clip.inp then
        state.clip.inp, state.clip.out = state.clip.out, state.clip.inp
      end
      if tt then mp.commandv("seek", string.format("%.3f", tt), "absolute", "exact") end
      down_at = nil
      return
    end
    if state.hold_t then state.hold_t:kill(); state.hold_t = nil end
    if state.scrub then mp.commandv("seek", tostring(state.scrub), "absolute", "exact"); state.scrub = nil end
    state.vol_drag = false
    if state.held then
      state.held = false; mp.set_property_number("speed", state.prev_speed)
    elseif down_on_video and down_at and now() - down_at < 0.45 then
      -- a click on the picture pauses, unless it turns out to be a double-click (fullscreen)
      if state.dbl_t and now() - state.dbl_t < 0.5 then state.dbl_t = nil
      else
        if state.click_t then state.click_t:kill() end
        state.click_t = mp.add_timeout(0.24, function() state.click_t = nil; toggle_pause() end)
      end
    end
    down_at = nil
  end
end
mp.add_forced_key_binding("MBTN_LEFT", "marquee-click", on_left, {complex = true})

mp.add_forced_key_binding("MBTN_LEFT_DBL", "marquee-dbl", function()
  if not state.menu and not (state.bar and hit(state.bar)) and down_on_video then
    if state.click_t then state.click_t:kill(); state.click_t = nil end
    if state.hold_t then state.hold_t:kill(); state.hold_t = nil end
    state.dbl_t = now()  -- the second click's release must not pause
    send(state.pip and "pip" or "fullscreen")
  end
end)

mp.observe_property("mouse-pos", "native", function(_, mp_)
  if not mp_ then return end
  local inside = mp_.hover ~= false
  if not inside then
    if state.inside then state.last_move = now() - 1.9 end -- left the window: hide soon
    state.inside = false
    return
  end
  local moved = mp_.x ~= state.mouse.x or mp_.y ~= state.mouse.y
  state.inside = true
  state.mouse = {x = mp_.x, y = mp_.y}
  if moved then poke() end
  if state.scrub and state.bar then
    state.scrub = prop("duration", 0) * clamp((mp_.x - state.bar.x) / state.bar.w, 0, 1)
  end
  if state.vol_drag then slider_at(state.vol_drag) end
  if state.clip and state.clip.drag and state.bar then  -- dragging an IN/OUT handle (the preview follows)
    local tt = prop("duration", 0) * clamp((mp_.x - state.bar.x) / state.bar.w, 0, 1)
    if state.clip.drag == "in" then state.clip.inp = tt else state.clip.out = tt end
  end
  if state.pip_press and not state.pip_moved then  -- PiP: dragging the picture moves the window
    if math.abs(mp_.x - state.pip_press.x) + math.abs(mp_.y - state.pip_press.y) > 4 then
      state.pip_moved = true
      state.pip_press = nil
      send("pip-drag")
    end
  end
end)

local function key_captions()
  local sid = prop("sid", false)
  if sid == "no" or sid == false then
    local subs = track_list("sub")
    if #subs > 0 then mp.set_property_number("sid", subs[1].id) end
    mp.commandv("script-message", "marquee-toast", "Captions on")
  else
    mp.set_property("sid", "no")
    mp.commandv("script-message", "marquee-toast", "Captions off")
  end
end

-- Clip mode -----------------------------------------------------------------------------------------------
function clip_mode(on)
  if on then
    state.clip = state.clip or {slices = {}, gif = false, join = false}
    close_menu()
    state.toast = "Clip: set IN (I) and OUT (O), add slices, Save"; state.toast_t = now()
  else
    if state.clip and state.clip.loop then mp.set_property("ab-loop-a", "no"); mp.set_property("ab-loop-b", "no") end
    state.clip = nil
  end
end

function clip_set(which)
  if not state.clip then clip_mode(true) end
  local t = prop("time-pos", 0) or 0
  if which == "in" then state.clip.inp = t else state.clip.out = t end
  local c = state.clip
  if c.inp and c.out and c.out < c.inp then c.inp, c.out = c.out, c.inp end
end

function clip_add()
  local c = state.clip
  if c and c.inp and c.out and c.out - c.inp >= 0.2 then
    table.insert(c.slices, {c.inp, c.out})
    table.sort(c.slices, function(x, y) return x[1] < y[1] end)
    c.inp, c.out = nil, nil
  else
    state.toast = "Set IN and OUT first"; state.toast_t = now()
  end
end

function clip_loop(i)
  local c = state.clip
  if not c then return end
  if i == nil or c.loop == i then
    c.loop = nil
    mp.set_property("ab-loop-a", "no"); mp.set_property("ab-loop-b", "no")
  else
    c.loop = i
    local sl = c.slices[i]
    mp.set_property_number("ab-loop-a", sl[1]); mp.set_property_number("ab-loop-b", sl[2])
    mp.commandv("seek", string.format("%.3f", sl[1]), "absolute", "exact")
    mp.set_property_bool("pause", false)
  end
end

function clip_save()
  local c = state.clip
  if not c or c.saving then return end
  local list = {}
  for _, sl in ipairs(c.slices) do table.insert(list, sl) end
  if c.inp and c.out and c.out - c.inp >= 0.2 then table.insert(list, {c.inp, c.out}) end
  if #list == 0 then state.toast = "Set IN and OUT first"; state.toast_t = now(); return end
  c.saving, c.progress, c.status = true, 0, "Saving…"
  send("clip-save", utils.format_json({slices = list, join = c.join, gif = c.gif}))
end

-- Bookmark prompt ------------------------------------------------------------------------------------------
local bind_keys, unbind_keys

local function prompt_close(save)
  local pr = state.prompt
  if not pr then return end
  state.prompt = nil
  mp.remove_key_binding("marquee-prompt-text")
  for _, k in ipairs({"BS", "ENTER", "KP_ENTER", "ESC", "TAB"}) do mp.remove_key_binding("marquee-prompt-" .. k) end
  bind_keys()
  if pr.kind == "ask" then
    if save then
      state.atlas = {state = "thinking", t = now()}
      mp.commandv("script-message", "marquee-ask-atlas", pr.text)
    end
    return
  end
  if save then
    local note = pr.text:gsub("#%S+", ""):gsub("^%s+", ""):gsub("%s+$", "")
    local custom = pr.text:match("#(%S+)")
    local tag = pr.tag == "#" and (custom or "") or (pr.tag ~= "" and pr.tag or (custom or ""))
    send("bookmark", tostring(pr.ms), note, tag)
  end
  if pr.resume then mp.set_property_bool("pause", false) end
end

local function bookmark_prompt(kind)
  local pos = prop("time-pos", nil)
  if not pos then return end
  state.prompt = {ms = math.floor(pos * 1000 + 0.5), text = "", tag = "", resume = not prop("pause", false), kind = kind}
  mp.set_property_bool("pause", true)
  unbind_keys()  -- typing must not trigger the player's keys
  mp.add_forced_key_binding("ANY_UNICODE", "marquee-prompt-text", function(e)
    if e.event == "down" or e.event == "repeat" or e.event == "press" then
      if e.key_text and e.key_text ~= "" then state.prompt.text = state.prompt.text .. e.key_text end
    end
  end, {complex = true})
  mp.add_forced_key_binding("BS", "marquee-prompt-BS", function()
    local tx = state.prompt.text
    state.prompt.text = tx:gsub("[%z\1-\127\194-\244][\128-\191]*$", "")
  end, {repeatable = true})
  mp.add_forced_key_binding("ENTER", "marquee-prompt-ENTER", function() prompt_close(true) end)
  mp.add_forced_key_binding("KP_ENTER", "marquee-prompt-KP_ENTER", function() prompt_close(true) end)
  mp.add_forced_key_binding("ESC", "marquee-prompt-ESC", function() prompt_close(false) end)
  mp.add_forced_key_binding("TAB", "marquee-prompt-TAB", function()
    local pr = state.prompt
    if pr.kind == "ask" then return end
    local order = {"", TAGS[1], TAGS[2], TAGS[3]}
    local idx = 1
    for i, tg in ipairs(order) do if tg == pr.tag then idx = i end end
    pr.tag = order[idx % #order + 1]
  end)
end

function scene_pick_close(n)
  local sp = state.scene_pick
  if not sp then return end
  for i, _ in ipairs(sp.items) do mp.commandv("overlay-remove", tostring(19 + i)) end
  state.scene_pick = nil
  mp.commandv("script-message", "marquee-scene-choose", tostring(sp.id), tostring(n or 0))
end

function scene_undo_now()
  state.scene_undo = nil
  state.atlas = nil
  mp.commandv("script-message", "marquee-scene-undo-now")
end

local function scene_undo_live()
  local su = state.scene_undo
  return su and now() - su.t < su.dur
end

function ask_atlas()
  if state.atlas and state.atlas.state == "answer" then state.atlas = nil; return end  -- A again: dismiss
  if state.prompt then return end
  -- type the question (the picture is kept from this moment); speaking stays on the push-to-talk key
  bookmark_prompt("ask")
end

local keys = {
  {"SPACE", toggle_pause}, {"k", toggle_pause}, {"MBTN_MID", toggle_pause}, {"PLAYPAUSE", toggle_pause},
  {"RIGHT", function() seek(ud("seek_step", 5)) end, true}, {"LEFT", function() seek(-ud("seek_step", 5)) end, true},
  {"l", function() seek(10) end, true}, {"j", function() seek(-10) end, true},
  {"UP", function() nudge_volume(5) end, true}, {"DOWN", function() nudge_volume(-5) end, true},
  {"WHEEL_UP", function() nudge_volume(5) end}, {"WHEEL_DOWN", function() nudge_volume(-5) end},
  {"m", function() mp.commandv("cycle", "mute"); state.vol_flash_t = now() end},
  {"f", function() send("fullscreen") end},
  {"p", function() send("pip") end},
  {"b", function() if scene_undo_live() then scene_undo_now() elseif state.mini then send("restore"); state.next_cancelled = true else bookmark_prompt() end end},
  {"B", function() if scene_undo_live() then scene_undo_now() elseif state.mini then send("restore"); state.next_cancelled = true else bookmark_prompt() end end},
  {"1", function() if state.scene_pick and #state.scene_pick.items >= 1 then scene_pick_close(1) end end},
  {"2", function() if state.scene_pick and #state.scene_pick.items >= 2 then scene_pick_close(2) end end},
  {"3", function() if state.scene_pick and #state.scene_pick.items >= 3 then scene_pick_close(3) end end},
  {"x", function() clip_mode(not state.clip) end},
  {"i", function() if state.clip then clip_set("in") end end},
  {"o", function() if state.clip then clip_set("out") end end},
  {"ENTER", function() if state.clip then clip_add() end end},
  {"a", function() ask_atlas() end}, {"A", function() ask_atlas() end},
  {",", function() mp.commandv("frame-back-step"); state.toast = "◀  Frame"; state.toast_t = now() end, true},
  {".", function() mp.commandv("frame-step"); state.toast = "Frame  ▶"; state.toast_t = now() end, true},
  {"[", function() set_speed(prop("speed", 1) - 0.05) end, true},
  {"]", function() set_speed(prop("speed", 1) + 0.05) end, true},
  {"\\", function() set_speed(1) end},
  {"c", key_captions},
  {"n", function() if ud("has_next", false) then send("next") end end},
  {"N", function() if ud("has_next", false) then send("next") end end},
  {"ESC", function()
    if state.scene_pick then scene_pick_close(0)
    elseif state.mini then send("restore"); state.next_cancelled = true
    elseif state.menu then close_menu()
    elseif state.card then state.card = nil
    elseif state.clip then clip_mode(false)
    elseif ud("pip", false) then send("pip")
    elseif ud("fullscreen", false) then send("fullscreen") else send("close") end
  end},
  {"BS", function() send("close") end},
}
bind_keys = function()
  for i, k in ipairs(keys) do
    mp.add_forced_key_binding(k[1], "marquee-key-" .. i, function() poke(); k[2]() end, k[3] and {repeatable = true} or nil)
  end
end
unbind_keys = function()
  for i, _ in ipairs(keys) do mp.remove_key_binding("marquee-key-" .. i) end
end
bind_keys()

mp.register_script_message("marquee-toast", function(msg) state.toast = msg; state.toast_t = now() end)
mp.register_script_message("marquee-open-menu", function(name) open_menu(name ~= "" and name or "settings") end)
mp.register_script_message("marquee-volume", function() state.vol_flash_t = now() end)
mp.register_script_message("marquee-keep-credits", function() state.next_cancelled = true end)
mp.register_script_message("marquee-clip-progress", function(label, f)
  if state.clip then state.clip.progress = tonumber(f) or 0; state.clip.status = label .. string.format(" · %d%%", math.floor((tonumber(f) or 0) * 100)) end
end)
mp.register_script_message("marquee-clip-done", function(folder, msg)
  if state.clip then state.clip.saving = false; state.clip.progress = nil; state.clip.status = nil end
  if folder ~= "" and state.clip then state.clip.slices = {}; state.clip.inp, state.clip.out = nil, nil end
  state.clip_done = {folder = folder or "", text = msg or "", t = now()}
end)
-- the assistant: marquee-atlas thinking|answer|clear [text]
mp.register_script_message("marquee-atlas", function(st, body)
  if st == "thinking" then state.atlas = {state = "thinking", t = now()}
  elseif st == "answer" then
    local n = ulen(body or "")
    state.atlas = {state = "answer", t = now(), text = body or "", dur = clamp(8 + n / 45, 8, 12)}
  else state.atlas = nil end
end)
-- scene jumps: marquee-scene-pick <id> <title> <w> <h> [<time> <caption> <bgra file>]x1..3; marquee-scene-undo <secs>
mp.register_script_message("marquee-scene-pick", function(id, title, w, h, ...)
  if state.scene_pick then scene_pick_close(0) end
  local a, items = {...}, {}
  for i = 1, #a - 2, 3 do
    if #items < 3 then table.insert(items, {label = a[i] or "", cap = a[i + 1] or "", file = a[i + 2] or ""}) end
  end
  if #items == 0 then return end
  state.atlas = nil
  state.scene_pick = {id = id, title = title or "Which one?", w = tonumber(w) or 240, h = tonumber(h) or 134,
    items = items, t = now()}
  poke()
end)
mp.register_script_message("marquee-scene-undo", function(secs)
  local d = tonumber(secs) or 0
  state.scene_undo = d > 0 and {t = now(), dur = d} or nil
end)
mp.register_script_message("marquee-card", function(title, body, secs)
  state.card = {title = title or "", text = body or "", t = now(), dur = tonumber(secs) or 15}
end)
mp.register_script_message("marquee-reset", function()
  state.next_cancelled = false; state.next_sent = false; state.menu = nil; state.closing_menu = nil
  if state.clip then clip_mode(false) end
  state.held = false; state.scrub = nil; poke()
end)

mp.add_periodic_timer(1 / 60, draw)
