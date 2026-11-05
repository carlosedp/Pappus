"""REVERSE: grains really play backwards.

The input is a rising chirp, 200 Hz to 1600 Hz every two seconds. One
voice, short grains with a gap between them, so each grain is an island in
the output. Zero crossings in a grain's first and second halves give its
pitch at its start and its end: a forward grain of a rising chirp rises, a
reversed one falls. REVERSE 0 must be all rising, 1 all falling, and 0.5 a
mix of both.

Usage:  python3 test/reverse_test.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph_test as G  # noqa: E402

SECS = 12.0
CHIRP_SRC = r'''~srcdef = SynthDef(\pappus_testsrc, {
	var ph = LFSaw.ar(0.5).range(0, 1);
	var s = SinOsc.ar(200 * (8 ** ph)) * 0.3;
	Out.ar(%(l)d, s); Out.ar(%(r)d, s);
});'''

# nsrc 1: GRAINSWARM 2 off, or its dry passthrough fills the gaps
ONE = {"msos": 0.8, "mrate": 4, "msize": 0.1, "mbuflen": 4, "nsrc": 1,
       "gates": [1, 0, 0, 0, 0, 0, 0, 0], "pitches": [0] * 8}
CASES = [("REVERSE 0", dict(ONE)), ("REVERSE 1", dict(ONE, mreverse=1)),
         ("REVERSE 0.5", dict(ONE, mreverse=0.5))]


def grains(ch, sr, thresh=0.01):
    """(start, end) sample ranges where the output is above thresh."""
    out, start, quiet = [], None, 0
    for i, v in enumerate(ch):
        if abs(v) > thresh:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet > sr // 100:          # 10 ms of quiet ends a grain
                out.append((start, i - quiet))
                start, quiet = None, 0
    return out


def zc(seg):
    return sum(1 for a, b in zip(seg, seg[1:]) if (a < 0) != (b < 0))


def main():
    work = tempfile.mkdtemp(prefix="pappus-rev-")
    text = G.script(work, False, [(n, s, "level") for n, s in CASES], True)
    i = text.index("~srcdef = SynthDef(")
    j = text.index("~srcdef.writeDefFile")
    text = text[:i] + CHIRP_SRC % dict(l=G.SRC_L, r=G.SRC_R) + "\n" + text[j:]
    k = text.index("fork {")
    text = text[:k] + ('fork {\n~scen.do({ arg row, i;\n\t~render.(~newscore.(row[1], %s, true), "r" ++ i);\n});\n"ALL DONE".postln; 0.exit;\n};\n' % SECS)
    path = os.path.join(work, "rev.scd")
    open(path, "w").write(text)
    out = subprocess.run([G.D.find_sclang(), "-i", "none", path], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=600)
    if "ALL DONE" not in out.stdout + out.stderr:
        print((out.stdout + out.stderr)[-3000:])
        return 1
    res = {}
    for idx, (name, _s) in enumerate(CASES):
        l, _r = G.read_wav(os.path.join(work, "r%d.wav" % idx))
        up = down = 0
        for a, b in grains(l[G.SR * 5:], G.SR):
            if b - a < G.SR * 0.04:
                continue
            mid = (a + b) // 2
            first, second = zc(l[G.SR * 5 + a:G.SR * 5 + mid]), zc(l[G.SR * 5 + mid:G.SR * 5 + b])
            # the chirp's wrap (1600 -> 200 Hz) inside a grain reads as a
            # fall; it is one grain in eight, and the thresholds allow it
            if second > first * 1.05:
                up += 1
            elif first > second * 1.05:
                down += 1
        res[name] = (up, down)
        print("  %-12s grains rising %2d, falling %2d" % (name, up, down))
    u0, d0 = res["REVERSE 0"]
    u1, d1 = res["REVERSE 1"]
    uh, dh = res["REVERSE 0.5"]
    ok = (u0 > 3 * max(d0, 1) and d1 > 3 * max(u1, 1) and uh >= 3 and dh >= 3)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
