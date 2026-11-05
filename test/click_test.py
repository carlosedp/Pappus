"""Clicks in the grain clouds, counted.

Reported on the forum: clicks and pops that go away when grains are made
shorter. A granulator reading a buffer that is still being recorded has two
classic places to click, and both get likelier the longer the grain:

- THE RECORD HEAD. A grain read at a pitch above unison moves through the
  buffer faster than the record head and can catch it up; below unison, the
  head can come round the loop and overwrite what the grain is about to
  read. Either way the grain plays across the seam between new and old
  audio - a step in the waveform.
- THE LOOP END. The loop is the first BUFFER seconds of a 60 s buffer. A
  grain that starts near the end of the loop and plays on past it reads
  whatever is beyond - stale audio or silence.

The input is a continuous pair of sines, so the only steps in the output are
the ones the granulator makes. A step shows up in the second difference
x[n] - 2x[n-1] + x[n-2], which for these sines through smooth grain windows
stays far below the threshold.

Usage:  python3 test/click_test.py [--graph PATH]
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph_test as G  # noqa: E402

SECS = 12.0
THRESH = 0.01     # second-difference step that counts as a click

SINE_SRC = r'''~srcdef = SynthDef(\pappus_testsrc, {
	Out.ar(%(l)d, SinOsc.ar(220) * 0.25);
	Out.ar(%(r)d, SinOsc.ar(331) * 0.25);
});'''

LONG = {"msos": 0.8, "mrate": 4, "msize": 1.5, "mbuflen": 4,
        "gates": [1, 1, 0, 0, 0, 0, 0, 0], "pitches": [12, -12, 0, 0, 0, 0, 0, 0]}
# The first and last cases are extremes on purpose: 1.5 s grains at +12 read
# a 3 s span of a 4 s loop, and for part of every lap no start position is
# both clear of the record head and short of the loop end (GrainBuf cannot
# wrap at the loop, only at the end of the 60 s buffer). The guard cuts those
# clicks by 80-90%; it cannot remove them. Everything else must be clean.
EXTREME = ("long grains +-12, recording", "delay-follow +12, recording")

CASES = [
    # name, settings, what it isolates
    ("long grains +-12, recording", dict(LONG), "both"),
    ("0.5 s grains +-12, 8 s buffer", dict(LONG, msize=0.5, mbuflen=8), "both, typical"),
    ("0.5 s, swarm 0.7, 8 s buffer", dict(LONG, msize=0.5, mbuflen=8, mswarm=0.7,
                                          mswarmmode=3), "duplicates too"),
    ("same, frozen after 5 s", dict(LONG, _late={"mlock": 1}), "loop end only"),
    ("long grains unison, recording", dict(LONG, pitches=[0] * 8), "loop end only"),
    ("short grains +-12, recording", dict(LONG, msize=0.08, mrate=20), "reference"),
    ("0.5 s +-12, all reversed", dict(LONG, msize=0.5, mbuflen=8, mreverse=1),
     "reverse"),
    ("0.5 s +-12, half reversed", dict(LONG, msize=0.5, mbuflen=8, mreverse=0.5),
     "reverse"),
    ("delay-follow +12, recording", dict(LONG, mscanmode=3, mdelay=0.3,
                                         pitches=[12, 12, 0, 0, 0, 0, 0, 0]),
     "record head"),
]


def clicks(path, skip=G.SR * 3, times=None):
    l, r = G.read_wav(path)
    n = 0
    peak = 0.0
    for ch in (l, r):
        last_click = -10**9
        for i in range(skip + 2, len(ch)):
            e = abs(ch[i] - 2 * ch[i - 1] + ch[i - 2])
            if e > peak:
                peak = e
            # one click is one event, not every sample of it
            if e > THRESH and i - last_click > 48:
                n += 1
                last_click = i
                if times is not None:
                    times.append(round(i / G.SR, 4))
    secs = (len(l) - skip) / G.SR
    return n / secs, peak, 20 * __import__("math").log10(max(G.rms(l[skip:]), 1e-9))


def main():
    graph = sys.argv[sys.argv.index("--graph") + 1] if "--graph" in sys.argv else None
    work = tempfile.mkdtemp(prefix="pappus-click-")
    scen = [(n, {k: v for k, v in s.items() if k != "_late"}, "level")
            for n, s, _ in CASES]
    text = G.script(work, False, scen, True, graph)
    i = text.index("~srcdef = SynthDef(")
    j = text.index("~srcdef.writeDefFile")
    text = text[:i] + SINE_SRC % dict(l=G.SRC_L, r=G.SRC_R) + "\n" + text[j:]
    # only the new graph, paused as the engine would
    k = text.index("fork {")
    late = []
    for name, st, _ in CASES:
        lt = st.get("_late", {})
        late.append("[" + ", ".join("[5.0, ['/n_set', 2000, \\%s, %s]]" % (k, v)
                                    for k, v in lt.items()) + "]")
    body = ('fork {\n~late = [%s];\n~scen.do({ arg row, i;\n'
            '\tvar sc = ~newscore.(row[1], %s, true);\n'
            '\t~late[i].do({ arg ev; sc.add(ev) }); sc.sort;\n'
            '\t~render.(sc, "c" ++ i);\n});\n"ALL DONE".postln; 0.exit;\n};\n'
            % (", ".join(late), SECS))
    text = text[:k] + body
    path = os.path.join(work, "click.scd")
    open(path, "w").write(text)
    out = subprocess.run([G.D.find_sclang(), "-i", "none", path], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=1200)
    log = out.stdout + out.stderr
    if "ALL DONE" not in log:
        print(log[-3000:])
        return 1
    print("clicks/s (2nd difference > %.3f), from %.0f s of each:\n" % (THRESH, SECS - 3))
    res = []
    for idx, (name, _s, why) in enumerate(CASES):  # noqa: B007
        skip = G.SR * (6 if "_late" in _s else 3)
        tl = []
        c, pk, lvl = clicks(os.path.join(work, "c%d.wav" % idx), skip, tl)
        res.append(c)
        print("  %-32s %6.2f/s  peak %.3f  level %5.1f dB   (%s)" % (name, c, pk, lvl, why))
        if tl and "-v" in sys.argv:
            print("      at s: %s" % ", ".join(str(t) for t in tl[:20]))
    bad = [CASES[i][0] for i, c in enumerate(res)
           if c > (1.5 if CASES[i][0] in EXTREME else 0.1)]
    print("\n%s" % ("PASS" if not bad else "FAIL: " + ", ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
