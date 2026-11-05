"""Run the real Engine_Pappus class on a live scsynth, beside the one it replaced.

graph_test.py proves the split graph sounds like the single SynthDef. This
proves the ENGINE around it works: the class compiles, alloc builds every
node, every command Lua knows about still exists with the same OSC format,
idle nodes really are paused and come back when they are needed - and it
measures real-time server CPU for the old and the new engine under the same
settings.

Needs sclang AND a running audio server for scsynth to boot (on a desktop
without audio, `jackd -d dummy -r 48000 -p 128` is enough). Off norns there
is no CroneEngine, so a minimal stand-in is written to a temporary class
directory - never into this repo, because norns compiles every .sc under dust
and a second CroneEngine would break its whole class library.

CPU numbers are this machine's, not a Pi 3's. The ratio between old and new is
the claim.

Usage:  python3 test/engine_live_test.py [--lite]
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import defload as D  # noqa: E402

# Commands this engine adds; everything else must match the old engine.
NEW_COMMANDS = {"budget": "f", "sampleload": "is"}

STUB = r'''
CronePollRegistry {
	classvar <polls;
	*register { arg name, func, periodic = true;
		polls = polls ?? { IdentityDictionary.new };
		polls[name] = func;
	}
	*remove { arg name; polls !? { polls.removeAt(name) } }
	*getPollFromName { arg name; ^polls[name] }
}

CroneEngine {
	var <context, <funcs, <formats;
	*new { arg context, doneCallback; ^super.new.init(context, doneCallback) }
	init { arg argContext, doneCallback;
		context = argContext;
		funcs = IdentityDictionary.new;
		formats = IdentityDictionary.new;
		fork { this.alloc; doneCallback.value(this) };
	}
	alloc {}
	free {}
	addPoll { arg name, func, periodic = true;
		CronePollRegistry.register(name.asSymbol, func, periodic);
	}
	addCommand { arg name, format, func;
		name = name.asSymbol;
		if(funcs[name].isNil) { funcs[name] = func; formats[name] = format };
	}
	cmd { arg name ... args; funcs[name.asSymbol].value([name.asSymbol] ++ args) }
	deinit { arg done;
		CronePollRegistry.polls.keys.copy.do({ arg k; CronePollRegistry.remove(k) });
		this.free; done.value(this);
	}
}
'''

SCRIPT = r'''
~out = "%(out)s";
~log = File(~out, "w");
~say = { arg ...xs; var l = xs.collect(_.asString).join(" "); l.postln; ~log.write(l ++ "\n") };

// The settings, as Lua would send them. Arrays as separate args.
~idle = [[\msrc, 2], [\nsrc, 2]];
~typical = ~idle ++ [[\msos, 0.8], [\gates, 1, 1, 1, 1, 0, 0, 0, 0], [\mrate, 16],
	[\pwet, 0.5], [\pmodel, 1], [\swet, 0.4], [\sfb, 0.3], [\rverb, 0.3], [\rtime, 0.4]];
~everything = ~typical ++ [[\nsos, 0.8], [\gates2, 1, 1, 1, 1, 1, 1, 1, 1],
	[\gates, 1, 1, 1, 1, 1, 1, 1, 1], [\mswarm, 0.5], [\nswarm, 0.5],
	[\pgrain, 0.5], [\sdiffuse, 0.5], [\drive, 0.3], [\crush, 0.1],
	[\loss, 0.4], [\noise, 0.3], [\kwow, 0.3], [\rshimmer, 0.3]];
~bank = [[\pfrq] ++ 48.collect({ arg i; 110 * (1 + (i div: 6)) * (1 + (i %% 6)) }),
	[\pamp] ++ 48.collect({ arg i; 0.02 / (1 + (i %% 6)) }),
	[\pvfrq] ++ 8.collect({ arg i; 110 * (i + 1) }), [\pvamp] ++ (0.3 ! 8)];

~apply = { arg e, cmds; cmds.do({ arg c; e.cmd(*c) }) };

~measure = { arg label, secs = 6;
	var avg = 0, peak = 0, n = 0;
	2.wait;   // let the settings and any pauses land
	secs.do({ 1.wait; avg = avg + s.avgCPU; peak = peak.max(s.peakCPU); n = n + 1 });
	~say.("CPU", label, (avg / n).round(0.01), peak.round(0.01));
};

~states = { arg e, label;
	var st = e.slotAt(\nstate);
	~say.("STATE", label, st.keys.asArray.sort.collect({ arg k; k ++ "=" ++ st[k] }).join(" "));
};

s = Server.default;
s.options.numInputBusChannels = 2;
s.options.numOutputBusChannels = 2;
s.options.memSize = 65536;
s.latency = 0.05;
s.waitForBoot {
	var ctx, src, c = Condition.new, e, old;
	SynthDef(\pappus_livesrc, { arg l, r;
		var tr = Impulse.ar(3), env = Decay2.ar(tr, 0.005, 0.3);
		Out.ar(l, SinOsc.ar(220 * [1, 2.01, 3.03]).sum * 0.2 * env);
		Out.ar(r, LFTri.ar(330 * [1, 1.5, 2.02]).sum * 0.15 * env);
	}).add;
	s.sync;
	ctx = (server: s, xg: Group.new(s),
		in_b: [Bus.audio(s, 1), Bus.audio(s, 1)], out_b: Bus.audio(s, 2));
	src = Synth(\pappus_livesrc, [\l, ctx.in_b[0].index, \r, ctx.in_b[1].index],
		ctx.xg, \addBefore);
	s.sync;
	~measure.("baseline-nothing", 3);

	// ---- the new engine
	e = Engine_Pappus.new(ctx, { c.unhang }); c.hang; s.sync;
	~say.("NODES", e.grp.asString, e.slotAt(\nodes).size);
	e.formats.keys.asArray.sort.do({ arg k; ~say.("CMD", k, e.formats[k]) });
	~apply.(e, ~bank);

	~apply.(e, ~idle); ~measure.("new-idle"); ~states.(e, "idle");
	~apply.(e, ~typical); ~measure.("new-typical"); ~states.(e, "typical");
	~apply.(e, ~everything); ~measure.("new-everything"); ~states.(e, "everything");

	// THE GOVERNOR. Everything on is a few %% of this machine, so a budget
	// of 2%% must walk it up the ladder; then off must give it all back.
	e.cmd(\budget, 2); 14.wait;
	~say.("GOV", "low", e.level, e.cut.asArray.sort.join(","), e.gcpu.round(0.1));
	~states.(e, "governed");
	~measure.("new-governed", 4);
	e.cmd(\budget, 0); 2.wait;
	~say.("GOV", "off", e.level, e.cut.asArray.sort.join(","), e.gcpu.round(0.1));
	~states.(e, "ungoverned");
	e.cmd(\budget, 80);

	// SAMPLE LOADING: a stereo file from audio/ lands in both halves of
	// GRAINSWARM 1's capture, and the rest of the buffer is cleared.
	e.cmd(\sampleload, 1, "%(audio)s/rain.wav"); 4.wait;
	~say.("SAMPLERATE", e.buf.sampleRate, e.bufr.sampleRate);
	[e.buf, e.bufr].do({ arg b, side;
		var c = Condition.new, head, tail;
		// getn, not loadToFloatArray: that one round-trips through a temp
		// file and a server sync, and hung here
		b.getn(24000, 1000, { arg a; head = a; c.unhang }); c.hang;
		c = Condition.new;
		b.getn(48000 * 55, 1000, { arg a; tail = a; c.unhang }); c.hang;
		~say.("SAMPLE", side, (head.squared.mean.sqrt * 1000).round, (tail.squared.mean.sqrt * 1000).round);
	});
	// rain.wav is 12.0 s at 44.1 kHz. Resampled, it ends at 12 s of 48 kHz
	// frames; read raw it would end at 11.03 s. So: sound at 11.5, none at 12.5.
	[11.5, 12.5].do({ arg t;
		var c = Condition.new, v;
		e.buf.getn((48000 * t).asInteger, 1000, { arg a; v = a; c.unhang }); c.hang;
		~say.("SAMPLEEND", t, (v.squared.mean.sqrt * 1000).round);
	});

	// switching model must swap which resonator runs
	e.cmd(\pmodel, 2); 1.5.wait; ~states.(e, "string");
	// and everything back to rest must pause it all again
	~apply.(e, [[\msos, 0], [\nsos, 0], [\pwet, 0], [\swet, 0], [\loss, 0],
		[\noise, 0], [\rverb, 0], [\sdiffuse, 0], [\pgrain, 0],
		[\drive, 0], [\crush, 0]]);
	3.wait; ~states.(e, "rest");

	// meters are live
	e.cmd(\msos, 0); 1.wait;
	~say.("METERS", 7.collect({ arg i;
		CronePollRegistry.polls[("meter" ++ (i + 1)).asSymbol].value.round(0.0001) }));

	e.deinit({}); s.sync; 1.wait;
	~measure.("after-free", 3);

	// ---- the old engine, same settings
	c = Condition.new;
	old = Engine_PappusOld.new(ctx, { c.unhang }); c.hang; s.sync;
	~apply.(old, ~bank);
	~apply.(old, ~idle); ~measure.("old-idle");
	~apply.(old, ~typical); ~measure.("old-typical");
	~apply.(old, ~everything); ~measure.("old-everything");
	old.cmd(\msos, 0); 1.wait;
	~say.("OLDMETERS", 7.collect({ arg i;
		CronePollRegistry.polls[("meter" ++ (i + 1)).asSymbol].value.round(0.0001) }));
	old.deinit({});

	~say.("ALL DONE");
	~log.close;
	1.wait;
	0.exit;
};
'''


def main():
    lite = "--lite" in sys.argv
    sclang = D.find_sclang()
    if not sclang:
        print("SKIP: no sclang")
        return 0
    work = tempfile.mkdtemp(prefix="pappus-live-")
    classes = os.path.join(work, "classes")
    os.mkdir(classes)
    open(os.path.join(classes, "CroneStub.sc"), "w").write(STUB)
    oldsrc = subprocess.run(
        ["git", "-C", ROOT, "show",
         "%s:lib/Engine_Pappus.sc" % os.environ.get("PAPPUS_OLD_REF", "main")],
        capture_output=True, text=True, check=True).stdout
    # The old prLiteMode compared PAPPUS_LITE with Array.includes, which
    # tests identity, so no string ever matched and the variable forced
    # nothing. Fixed in the new engine; patched here so --lite compares
    # LITE with LITE.
    oldsrc_run = oldsrc.replace("Engine_Pappus", "Engine_PappusOld").replace(
        '].includes(env))', '].includesEqual(env))')
    open(os.path.join(classes, "Engine_PappusOld.sc"), "w").write(oldsrc_run)
    # the old engine finds its loop files at <class dir>/../audio/
    os.symlink(os.path.join(ROOT, "audio"), os.path.join(work, "audio"))
    conf = os.path.join(work, "sclang_conf.yaml")
    open(conf, "w").write("includePaths:\n  - %s\n  - %s\n"
                          % (classes, os.path.join(ROOT, "lib")))
    out = os.path.join(work, "result.txt")
    scd = os.path.join(work, "live.scd")
    open(scd, "w").write(SCRIPT % dict(out=out, audio=os.path.join(ROOT, "audio")))
    env = dict(os.environ, PAPPUS_LITE="1" if lite else "0")
    try:
        r = subprocess.run([sclang, "-i", "none", "-l", conf, scd], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=300)
        log = r.stdout + r.stderr
    except subprocess.TimeoutExpired as ex:
        part = (ex.stdout or b"") + (ex.stderr or b"")
        part = part.decode(errors="replace") if isinstance(part, bytes) else part
        print(part[-6000:])
        print("FAIL: sclang timed out (a hung Condition, or a syntax error)")
        return 1
    if not os.path.exists(out) or "ALL DONE" not in open(out).read():
        print(log[-5000:])
        print("FAIL: did not finish")
        return 1
    res = open(out).read()
    graphs = re.findall(r"Engine_Pappus(?:Old)?: (FULL|LITE) graph", log)
    print("engines built: %s" % ", ".join(graphs))
    if any(g != ("LITE" if lite else "FULL") for g in graphs):
        print("FAIL: asked for %s" % ("LITE" if lite else "FULL"))
        return 1
    errs = [ln for ln in log.splitlines()
            if re.search(r"^ERROR|FAILURE IN SERVER|not found|exceeded", ln)]

    fails = []
    # Every command the old engine had, with the same format.
    newcmds = dict(re.findall(r"^CMD (\S+) (\S*)$", res, re.M))
    oldcmds = dict(re.findall(r'addCommand\("([a-z0-9_]+)", "([a-z]*)"', oldsrc))
    for k, f in sorted(oldcmds.items()):
        if k not in newcmds:
            fails.append("command %s is gone" % k)
        elif newcmds[k] != f:
            fails.append("command %s was %r, now %r" % (k, f, newcmds[k]))
    for k in sorted(set(newcmds) - set(oldcmds) - set(NEW_COMMANDS)):
        fails.append("new command %s (Lua does not send it)" % k)
    for k, f in NEW_COMMANDS.items():
        if newcmds.get(k) != f:
            fails.append("command %s should be %r, is %r" % (k, f, newcmds.get(k)))

    gov = dict((m[0], m[1:]) for m in re.findall(r"^GOV (\S+) (\d+) (\S*) ([0-9.]+)$", res, re.M))
    if "low" not in gov or int(gov["low"][0]) < 3:
        fails.append("governor did not climb under a 2%% budget: %s" % gov.get("low"))
    if "off" not in gov or int(gov["off"][0]) != 0 or gov["off"][1]:
        fails.append("budget off did not give everything back: %s" % gov.get("off"))

    states = dict(re.findall(r"^STATE (\S+) (.*)$", res, re.M))

    def st(label):
        return dict(kv.split("=") for kv in states.get(label, "").split())

    expect = {
        # idle: only the always-on nodes; every pausable one is off
        "idle": {k: "off" for k in st("idle")},
        "typical": dict(voice1="on", modal_a="on", dtaps="on", reverb="on",
                        string_a="off", loss="off", noise="off", ddiff="off",
                        rnoise="off"),
        "everything": dict(voice1="on", modal_a="on", dtaps="on", ddiff="on",
                           reverb="on", loss="on", noise="on", rnoise="on",
                           string_a="off"),
        "string": dict(string_a="on", modal_a="off"),
        "rest": {k: "off" for k in st("rest")},
    }
    if not lite:
        expect["typical"].update(modal_b="on", voice2="off")
        expect["everything"].update(voice2="on", modal_b="on")
        expect["string"].update(string_b="on", modal_b="off")
    for label, want in expect.items():
        got = st(label)
        if not got:
            fails.append("no state for %s" % label)
        for k, v in want.items():
            # "pending" = fading out, on its way to off
            g = got.get(k)
            if g != v and not (v == "off" and g == "pending"):
                fails.append("%s: %s is %s, expected %s" % (label, k, g, v))

    srs = re.search(r"^SAMPLERATE (\S+) (\S+)$", res, re.M)
    if not srs or any(float(x) != 48000 for x in srs.groups()):
        fails.append("sampleload left the capture buffers at %s Hz, not 48000"
                     % (srs.groups() if srs else "?"))
    smp = re.findall(r"^SAMPLE (\d) ([0-9.]+) ([0-9.]+)$", res, re.M)
    if len(smp) != 2 or any(float(h) == 0 or float(t) != 0 for _, h, t in smp):
        fails.append("sampleload: expected audio at the start and silence at 55 s, got %s" % smp)
    else:
        print("  sampleload: rms x1000 at start L %s R %s, at 55 s %s %s"
              % (smp[0][1], smp[1][1], smp[0][2], smp[1][2]))

    ends = dict(re.findall(r"^SAMPLEEND ([0-9.]+) ([0-9.]+)$", res, re.M))
    if not (float(ends.get("11.5", 0)) > 0 and float(ends.get("12.5", 1)) == 0):
        fails.append("resampled length wrong: rms at 11.5 s / 12.5 s = %s" % ends)
    else:
        print("  sampleload: 12.0 s file ends at 12 s of 48 kHz (rms x1000 at 11.5 s %s, 12.5 s %s)"
              % (ends["11.5"], ends["12.5"]))

    meters = re.search(r"^METERS \[(.*)\]$", res, re.M)
    if not meters or not any(float(x) > 0 for x in meters.group(1).split(",")):
        fails.append("meters are all zero")

    cpu = {m[0]: (float(m[1]), float(m[2]))
           for m in re.findall(r"^CPU (\S+) ([0-9.]+) ([0-9.]+)$", res, re.M)}
    print("%s, real-time scsynth on this machine (avg %% / peak %%):"
          % ("LITE" if lite else "FULL"))
    for label in ("idle", "typical", "everything"):
        o, n = cpu.get("old-" + label), cpu.get("new-" + label)
        if o and n:
            print("  %-11s old %5.1f / %5.1f   new %5.1f / %5.1f   new/old %4.0f%%"
                  % (label, o[0], o[1], n[0], n[1], 100 * n[0] / max(o[0], 1e-9)))
    if "baseline-nothing" in cpu:
        print("  (empty server: %.1f%%; after freeing the engine: %.1f%%)"
              % (cpu["baseline-nothing"][0], cpu.get("after-free", (0, 0))[0]))
    print("  nodes made: %s" % re.search(r"^NODES \S+ (\d+)", res, re.M).group(1))
    for k, v in sorted(gov.items()):
        print("  governor %-4s level %s  cut [%s]  scsynth %s%%" % (k, v[0], v[1], v[2]))
    if "new-governed" in cpu:
        print("  everything, governed: %.1f%% (ungoverned %.1f%%)"
              % (cpu["new-governed"][0], cpu.get("new-everything", (0, 0))[0]))
    for label in ("idle", "typical", "everything", "governed", "ungoverned", "string", "rest"):
        print("  %-10s %s" % (label, states.get(label, "")))
    if meters:
        print("  meters: %s" % meters.group(1))
    om = re.search(r"^OLDMETERS \[(.*)\]$", res, re.M)
    if om:
        print("  old meters at the same point: %s" % om.group(1))
    if errs:
        fails += ["sclang/scsynth: " + e for e in errs[:10]]
    for f in fails:
        print("FAIL: " + f)
    print("PASS" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
