"""The split graph (lib/pappus_graph.scd) against the single SynthDef it replaced.

Pappus used to be one SynthDef; it is now ~20 small nodes, most of which can
be paused. This checks three claims, all offline in NRT:

1. SAME SOUND. For each scenario, render the old single def and the new
   graph from the same input with the same settings, and measure the
   difference. Deterministic paths (no random UGens audible) must null to
   well below audibility. Paths with random UGens (grain jitter, noise) can
   not null sample for sample, so they are compared by level instead.

2. PAUSING IS INAUDIBLE. Render the new graph twice per scenario: every node
   running, and the idle nodes paused as the engine would pause them. These
   must null exactly where the scenario is deterministic.

3. IT IS CHEAPER. Time the NRT render of each - wall-clock, on this machine.
   That is not a Pi 3 number, but the ratio between graphs is the claim.

The old def is taken from git (main by default, PAPPUS_OLD_REF to change),
so the comparison is against what shipped rather than a copy.

Usage:  python3 test/graph_test.py [--quick] [--lite]
"""
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
GRAPH = os.path.join(ROOT, "lib", "pappus_graph.scd")
sys.path.insert(0, HERE)
import harness as H  # noqa: E402
import defload as D  # noqa: E402

SR = 48000
DUR = 6.0          # comparison renders
BENCH = 30.0       # timing renders

# Buffers: same numbering the old harnesses use.
CAP, CAPR, CAP2, CAP2R = H.CAPTURE_BUF, H.CAPTURE_BUFR, H.CAPTURE_BUF2, H.CAPTURE_BUF2R
PAT, PAT2, DEL = H.PAT_BUF, H.PAT_BUF2, H.DEL_BUF
ENV0 = H.ENV_BUF0
MBUS = 60
SRC_L, SRC_R = 16, 17   # test input, on private buses
BUS0 = 64               # the new graph's private buses start here

PFRQ = [110 * (1 + (i // 6)) * (1 + (i % 6)) for i in range(48)]
# quiet enough that RESONATOR's per-channel limiters stay out of it: they
# are nonlinear, so with them working a mono comparison of a re-panned bank
# would measure the limiters rather than the bank.
PAMP = [0.02 / (1 + (i % 6)) for i in range(48)]
PVFRQ = [110 * (i + 1) for i in range(8)]
PVAMP = [0.3] * 8

# name, settings, kind:
#   exact    must null against the old def
#   mono     must null in mono (L+R) - the stereo placement changed on purpose
#   changed  intentionally different now; reported, not judged
#   level    random UGens: same level, not the same samples
SCENARIOS = [
    ("default (dry chain)", {}, "exact"),
    ("resonator MODAL", {"pwet": 1, "pmodel": 1}, "mono"),
    ("resonator STRING", {"pwet": 1, "pmodel": 2, "pstruct": 0.5}, "exact"),
    ("delay + diffuse", {"swet": 0.6, "sfb": 0.4, "sdiffuse": 0.6,
                         "scycle": 0.5}, "changed"),
    ("drive + crush", {"drive": 0.4, "crush": 0.15, "crushmode": 1}, "exact"),
    ("drive only", {"drive": 0.4}, "exact"),
    ("capture TILT", {"mtilt": 0.6}, "exact"),
    ("LOSS", {"loss": 0.5}, "exact"),
    ("reverb", {"rverb": 0.5, "rtime": 0.4}, "exact"),
    ("compressor + sends", {"mcomp": 0.7, "oin1": 0.5, "kin1": 0.3}, "exact"),
    ("grains 1 (SOS 0.8)", {"msos": 0.8, "mrate": 20, "msize": 0.1}, "level"),
    # The voices compute their per-grain values at control rate now (see
    # voicedef in pappus_graph.scd): random draws differ, so level only -
    # across the modes that use those values.
    ("grains spray+4 voices", {"msos": 0.8, "mrate": 30, "msize": 0.08,
                               "mspray": 0.6, "mspraymode": 1,
                               "gates": [1, 1, 1, 1, 0, 0, 0, 0],
                               "pitches": [0, 7, 12, -12, 0, 0, 0, 0]}, "level"),
    ("grains swarm+strum", {"msos": 0.8, "mrate": 8, "msize": 0.2,
                            "mswarm": 0.7, "mswarmmode": 3, "mstrum": 0.1,
                            "gates": [1, 1, 1, 1, 1, 1, 0, 0]}, "level"),
    ("grains delay-scan 200Hz", {"msos": 0.8, "mrate": 200, "msize": 0.03,
                                 "mscanmode": 3, "mdelay": 0.5}, "level"),
    ("grains prob 0.5 warp", {"msos": 0.8, "mrate": 40, "msize": 0.05,
                              "mspraymode": 3, "mspray": 0.5,
                              "probs": [0.5] * 8, "gates": [1] * 8}, "level"),
    ("noise", {"noise": 0.5, "noisetype": 1}, "level"),
    ("everything", {"msos": 0.8, "nsos": 0.8,
                    "mswarm": 0.5, "pwet": 0.7, "pgrain": 0.5, "swet": 0.5,
                    "sdiffuse": 0.5, "drive": 0.3, "loss": 0.4, "noise": 0.3,
                    "rverb": 0.4, "kwow": 0.3,
                    "gates": [1] * 8, "gates2": [1] * 8}, "changed"),
]

# The old def's delay taps never moved off the write head: the per-tap
# Phasor was meant to jump to (head - tap time) on an Impulse.ar(0), but a
# Phasor takes its first trigger value as its previous one, so that first-
# sample impulse is never an edge. Every tap read at zero delay. The new graph
# puts the taps where TIME says, so it cannot null against that - so the delay
# is ALSO checked against a copy of the new graph with the old zero-delay read
# patched back in. That one must null, which proves the rest of the delay
# (write, feedback, tilt, diffusion, pitch drift, mix) is the same machine.
TAP_FIX = "(dph - (t.max(0.0005) * SampleRate.ir) + drift)"
TAP_OLD = "(dph + drift)"
COMPAT = [
    ("delay, old tap read", {"swet": 0.6, "sfb": 0.4, "sdiffuse": 0.6,
                             "stilt": 0.4, "scycle": 0.5}, "exact"),
    ("delay pitch, old read", {"swet": 0.6, "tappitch": [0, 3, -5, 7, 0, 0, 0, 0],
                               "taplevels": [1, 0.5, 0.5, 0.5, 0, 0, 0, 0]},
     "near"),
]


# Grain scenarios run a 2 s buffer and are measured from 3 s, i.e. after the
# buffer has filled once. Before that, everything ahead of the record head is
# silence - which the old code let grains read and the seam guard does not -
# so a first-lap comparison measures the guard, not the graph.
GRAIN_STEADY = {"mbuflen": 2}
for _i, (_n, _s, _k) in enumerate(SCENARIOS):
    if _k == "level" and "msos" in _s:
        SCENARIOS[_i] = (_n, dict(GRAIN_STEADY, **_s), _k)


def sc_val(v):
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(sc_val(x) for x in v) + "]"
    return repr(float(v)) if isinstance(v, float) else str(v)


def old_body(ref):
    src = subprocess.run(["git", "-C", ROOT, "show", "%s:lib/Engine_Pappus.sc" % ref],
                         capture_output=True, text=True, check=True).stdout
    tmp = tempfile.NamedTemporaryFile("w", suffix=".sc", delete=False)
    tmp.write(src)
    tmp.close()
    D.ENGINE = tmp.name
    return D.synthdef_body()


BASE = {"msrc": 2, "nsrc": 2}


def script(work, lite, scen, quick, graph=None):
    old = old_body(os.environ.get("PAPPUS_OLD_REF", "main"))
    allocs = ",\n".join(H.alloc_msgs(True))
    # Every scenario's settings as an sclang Event
    sc_scen = ",\n".join(
        "[%s, (%s), %s]" % (
            '"%s"' % name,
            ", ".join("%s: %s" % (k, sc_val(v)) for k, v in dict(BASE, **s).items()),
            "true")
        for name, s, _kind in scen)
    return r'''
~work = "%(work)s";
~forcepause = [];
~lite = %(lite)s;
~quick = %(quick)s;
~mk = { arg n; (bufnum: n) };
~buf = ~mk.(%(cap)d); ~bufr = ~mk.(%(capr)d); ~buf2 = ~mk.(%(cap2)d); ~buf2r = ~mk.(%(cap2r)d);
~dbuf = ~mk.(%(del)d); ~patbuf = ~mk.(%(pat)d); ~patbuf2 = ~mk.(%(pat2)d);
~mbus = %(mbus)d; ~envnums = (%(env0)d..%(env1)d);
~bufdur = 60.0; ~deldur = 11.0; ~loopbufs = [];
~alloc = [%(allocs)s];

~olddef = %(old)s;
~olddef.writeDefFile(~work);

~g = "%(graph)s".load;
~g[\defs].value((lite: ~lite, bufdur: 60.0, nloops: 0)).do({ arg d;
	d.writeDefFile(~work) });

~srcdef = SynthDef(\pappus_testsrc, {
	// A stereo source with an envelope, so followers and SOS have
	// something to do: a plucked-ish tone left, a brighter one right.
	var tr = Impulse.ar(3);
	var env = Decay2.ar(tr, 0.005, 0.3);
	var l = SinOsc.ar(220 * [1, 2.01, 3.03]).sum * 0.2 * env;
	var r = LFTri.ar(330 * [1, 1.5, 2.02]).sum * 0.15 * env;
	Out.ar(%(srcl)d, l + (SinOsc.ar(97) * 0.05));
	Out.ar(%(srcr)d, r + (SinOsc.ar(131) * 0.05));
});
~srcdef.writeDefFile(~work);

// Bus layout: same table the engine reads.
~bus = (); ~next = %(bus0)d;
~g[\buses].keysValuesDo({ arg k, n; ~bus[k] = ~next; ~next = ~next + n; });
~resolve = { arg w;
	if(w.isArray and: { w.isString.not }) { ~mbus + w[1] } {
		switch(w,
			\buf, { ~buf.bufnum }, \bufr, { ~bufr.bufnum },
			\buf2, { ~buf2.bufnum }, \buf2r, { ~buf2r.bufnum },
			\dbuf, { ~dbuf.bufnum },
			\patbuf, { ~patbuf.bufnum }, \patbuf2, { ~patbuf2.bufnum },
			\outbus, { 0 },
			{ ~bus[w] })
	}
};

// A setting as /n_set or /n_setn messages to a node or group.
~setmsgs = { arg id, s;
	var out = [];
	s.keysValuesDo({ arg k, v;
		if(v.isArray) { out = out.add(['/n_setn', id, k, v.size] ++ v) }
		{ out = out.add(['/n_set', id, k, v]) };
	});
	out
};

~common = [\pfrq, %(pfrq)s, \pamp, %(pamp)s, \pvfrq, %(pvfrq)s, \pvamp, %(pvamp)s];

~heads = { [
	[0.0, ['/d_load', ~work ++ "/*.scsyndef"]],
	[0.0, ['/s_new', \pappus_testsrc, 999, 0, 0]]
] };

~oldscore = { arg s, dur;
	Score(~alloc ++ ~heads.value ++ [
		[0.01, ['/s_new', \pappus, 1000, 3, 999,
			\inbusl, %(srcl)d, \inbusr, %(srcr)d, \outbus, 0]],
		[0.01, ['/n_setn', 1000, \pfrq, 48] ++ %(pfrq)s],
		[0.01, ['/n_setn', 1000, \pamp, 48] ++ %(pamp)s],
		[0.01, ['/n_setn', 1000, \pvfrq, 8] ++ %(pvfrq)s],
		[0.01, ['/n_setn', 1000, \pvamp, 8] ++ %(pvamp)s]
	] ++ ~setmsgs.(1000, s).collect({ arg m; [0.01, m] })
	  ++ [[dur, ['/c_set', 0, 0]]])
};

// The new graph. pause: evaluate the conds the way the engine does and
// pause whatever is not needed.
~newscore = { arg s, dur, pause;
	var msgs = [[0.01, ['/g_new', 2000, 3, 999]]], id = 2001, p, run = [];
	p = ~g[\defaults].copy;
	s.keysValuesDo({ arg k, v; if(p.includesKey(k)) { p[k] = v } });
	~g[\order].do({ arg row;
		var key = row[0], def = row[1], pausable = row[2], args;
		var skip = false;
		if(~lite) {
			if(~g[\liteskip].includes(key)) { skip = true };
			def = ~g[\liteswap][key] ? def;
		};
		if(skip.not) {
			args = [\bg1, ~bus[\g1], \bg2, ~bus[\g2], \envnums, ~envnums,
				\inbusl, %(srcl)d, \inbusr, %(srcr)d];
			row[3].pairsDo({ arg c, w; args = args ++ [c, ~resolve.(w)] });
			msgs = msgs.add([0.01, ['/s_new', def, id, 1, 2000] ++ args.asOSCArgArray]);
			if(pausable and: {
				(pause and: { ~g[\conds][key].value(p).not })
				or: { ~forcepause.includes(key) }
				// a substitute only runs in place of the node it stands in for
				or: { ~g[\subs].notNil and: { ~g[\subs].includes(key) }
					and: { ~forcepause.includes(~g[\subs].findKeyForValue(key)).not } }
			}) {
				run = run.add([0.02, ['/n_run', id, 0]]);
			};
			id = id + 1;
		};
	});
	Score(~alloc ++ ~heads.value ++ msgs
		++ ~setmsgs.(2000, (pfrq: %(pfrq)s, pamp: %(pamp)s,
			pvfrq: %(pvfrq)s, pvamp: %(pvamp)s)).collect({ arg m; [0.01, m] })
		++ ~setmsgs.(2000, s).collect({ arg m; [0.01, m] })
		++ run ++ [[dur, ['/c_set', 0, 0]]])
};

~opts = ServerOptions.new.numOutputBusChannels_(2).numInputBusChannels_(2)
	.numWireBufs_(64).memSize_(65536).numAudioBusChannels_(256);

~render = { arg score, name;
	var c = Condition.new, t0 = Main.elapsedTime, t;
	score.recordNRT(~work ++ "/" ++ name ++ ".osc", ~work ++ "/" ++ name ++ ".wav",
		nil, 48000, "WAV", "float", ~opts, action: { c.unhang });
	c.hang;
	t = Main.elapsedTime - t0;
	("TIME " ++ name ++ " " ++ t).postln;
};

~scen = [%(scen)s];

fork {
	~scen.do({ arg row, i;
		var s = row[1];
		~render.(~oldscore.(s, %(dur)s), "old" ++ i);
		~render.(~newscore.(s, %(dur)s, false), "all" ++ i);
		~render.(~newscore.(s, %(dur)s, true), "new" ++ i);
	});
	if(~quick.not) {
		// Timing: idle (defaults) and everything, at length.
		[0, ~scen.size - 1].do({ arg i;
			var s = ~scen[i][1];
			~render.(~oldscore.(s, %(bench)s), "bold" ++ i);
			~render.(~newscore.(s, %(bench)s, true), "bnew" ++ i);
		});
	};
	"ALL DONE".postln;
	0.exit;
};
''' % dict(work=work, lite="true" if lite else "false",
           quick="true" if quick else "false",
           cap=CAP, capr=CAPR, cap2=CAP2, cap2r=CAP2R, del_=DEL, pat=PAT, pat2=PAT2,
           mbus=MBUS, env0=ENV0, env1=ENV0 + 16, allocs=allocs, old=old,
           graph=graph or GRAPH, srcl=SRC_L, srcr=SRC_R, bus0=BUS0,
           pfrq=sc_val(PFRQ), pamp=sc_val(PAMP), pvfrq=sc_val(PVFRQ),
           pvamp=sc_val(PVAMP), scen=sc_scen, dur=DUR, bench=BENCH,
           **{"del": DEL})


def read_wav(path):
    """Float32 WAV -> (left, right) lists, without numpy."""
    data = open(path, "rb").read()
    pos = 12
    ch = 2
    while pos < len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            ch = struct.unpack("<H", data[pos + 10:pos + 12])[0]
        if cid == b"data":
            n = size // 4
            vals = struct.unpack("<%df" % n, data[pos + 8:pos + 8 + n * 4])
            return [vals[c::ch] for c in range(ch)]
        pos += 8 + size + (size & 1)
    raise ValueError("no data chunk in " + path)


def rms(xs):
    import math
    return math.sqrt(sum(x * x for x in xs) / max(1, len(xs)))


def compare(a, b, skip=SR // 2, mono=False):
    """(level of a dBFS, level of b, difference relative to a in dB)."""
    import math
    if mono:
        sa = [x + y for x, y in zip(a[0][skip:], a[1][skip:])]
        sb = [x + y for x, y in zip(b[0][skip:], b[1][skip:])]
    else:
        sa = a[0][skip:] + a[1][skip:]
        sb = b[0][skip:] + b[1][skip:]
    n = min(len(sa), len(sb))
    sa, sb = sa[:n], sb[:n]
    ra, rb = rms(sa), rms(sb)
    rd = rms([x - y for x, y in zip(sa, sb)])
    db = lambda v: 20 * math.log10(max(v, 1e-12))  # noqa: E731
    return db(ra), db(rb), db(rd) - db(max(ra, 1e-12))


def render(work, scen, lite, quick, graph=None):
    sclang = D.find_sclang()
    path = os.path.join(work, "graph.scd")
    open(path, "w").write(script(work, lite, scen, quick, graph))
    out = subprocess.run([sclang, "-i", "none", path], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=1800)
    log = out.stdout + out.stderr
    if "ALL DONE" not in log:
        print(log[-4000:])
        raise SystemExit("FAIL: harness did not finish")
    errs = [ln for ln in log.splitlines()
            if re.search(r"ERROR|FAILURE|not found|exceeded", ln)
            and "QtWebEngine" not in ln and "resource" not in ln]
    if errs:
        print("\n".join(errs[:20]))
    return dict(re.findall(r"TIME (\S+) ([0-9.]+)", log))


def report(work, scen, lite):
    fails = 0
    print("%-24s %8s %8s %10s %12s" % ("scenario", "old dB", "new dB",
                                       "old-new", "paused-all"))
    for i, (name, _s, kind) in enumerate(scen):
        old = read_wav(os.path.join(work, "old%d.wav" % i))
        allw = read_wav(os.path.join(work, "all%d.wav" % i))
        new = read_wav(os.path.join(work, "new%d.wav" % i))
        # Grain scenarios are measured in steady state (see GRAIN_STEADY).
        sk = SR * 3 if kind == "level" else SR // 2
        lo, ln, dn = compare(old, new, skip=sk, mono=(kind == "mono"))
        _, _, dp = compare(allw, new, skip=sk)
        deterministic = kind in ("exact", "mono", "near", "changed")
        if lite and kind != "level":
            # LITE's old graph differs from the new one by design: the new
            # one always delays the dry path by LOSS's latency, as FULL
            # always did, so LITE's dry and its direct sends now line up
            # the way FULL's do. Levels are reported; pausing must still be
            # exact.
            ok = dp < -100 or "everything" in name
            kind = kind + "/lite"
        elif kind in ("exact", "mono"):
            ok = dn < -60 and dp < -100
        elif kind == "near":
            ok = dn < -40 and dp < -100
        elif kind == "changed":
            ok = dp < -100 or "everything" in name
        else:
            ok = abs(lo - ln) < 1.5
        fails += 0 if ok else 1
        print("%-24s %8.1f %8.1f %10.1f %12.1f  %-4s %s" % (
            name, lo, ln, dn, dp, "ok" if ok else "FAIL", kind))
    return fails


def main():
    quick = "--quick" in sys.argv
    lite = "--lite" in sys.argv
    if not D.find_sclang():
        print("SKIP: no sclang")
        return 0
    t0 = time.time()
    work = tempfile.mkdtemp(prefix="pappus-graph-")
    times = render(work, SCENARIOS, lite, quick)
    print("%s graph, %.0f s per render\n" % ("LITE" if lite else "FULL", DUR))
    fails = report(work, SCENARIOS, lite)

    if not lite:
        src = open(GRAPH).read()
        assert TAP_FIX in src, "tap read changed - update TAP_FIX"
        cwork = tempfile.mkdtemp(prefix="pappus-graph-compat-")
        cgraph = os.path.join(cwork, "graph_oldtaps.scd")
        open(cgraph, "w").write(src.replace(TAP_FIX, TAP_OLD))
        render(cwork, COMPAT, lite, True, cgraph)
        print("\nnew graph with the old zero-delay tap read restored:")
        fails += report(cwork, COMPAT, lite)

    if not quick:
        print("\nNRT render time for %.0f s of audio (this machine, relative only):"
              % BENCH)
        for i in (0, len(SCENARIOS) - 1):
            to = float(times.get("bold%d" % i, 0))
            tn = float(times.get("bnew%d" % i, 0))
            print("  %-20s old %6.2f s   new %6.2f s   new/old %4.0f%%" % (
                SCENARIOS[i][0], to, tn, 100 * tn / max(to, 1e-9)))
    print("\n%s  (%.0f s)" % ("PASS" if fails == 0 else "%d FAILED" % fails,
                              time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
