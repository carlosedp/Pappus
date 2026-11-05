"""What each pausable node of the graph costs, measured offline.

Renders "everything on" in NRT and times it; then again with each pausable
node paused on its own; the difference is that node's cost. Also the floor
(every pausable node paused - what the always-on nodes cost). Wall-clock on
this machine, so the units are relative: the governor's cost table in
lib/pappus_graph.scd is these numbers, normalised.

Usage:  python3 test/node_cost.py [--reps N] [--secs S]
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph_test as G  # noqa: E402

PAUSABLE = ["voice1", "voice2", "rnoise", "modal_a", "modal_b", "string_a",
            "string_b", "dtaps", "ddiff", "loss", "noise", "reverb"]

EVERYTHING = dict(G.BASE, **dict(G.SCENARIOS[-1][1]))
# STRING and MODAL can not both run (pmodel picks one), so they are measured
# in two passes: MODAL selected, then STRING.
PASSES = [("modal", dict(EVERYTHING, pmodel=1)), ("string", dict(EVERYTHING, pmodel=2))]


def main():
    reps = int(sys.argv[sys.argv.index("--reps") + 1]) if "--reps" in sys.argv else 3
    secs = float(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 30
    work = tempfile.mkdtemp(prefix="pappus-cost-")
    base = G.script(work, False, [("x", PASSES[0][1], "level")], True)
    base = base[:base.index("fork {")]
    jobs = []
    for pname, s in PASSES:
        ev = "(" + ", ".join("%s: %s" % (k, G.sc_val(v)) for k, v in s.items()) + ")"
        jobs.append((pname, "all", "[]", ev))
        for k in PAUSABLE:
            jobs.append((pname, k, "[\\%s]" % k, ev))
        jobs.append((pname, "floor", "[%s]" % ", ".join("\\" + k for k in PAUSABLE), ev))
    body = "fork {\n%d.do({ arg r;\n" % reps
    for pname, label, fp, ev in jobs:
        body += ('\t~forcepause = %s; ~render.(~newscore.(%s, %s, false), "%s_%s_" ++ r);\n'
                 % (fp, ev, secs, pname, label))
    body += '});\n"ALL DONE".postln; 0.exit;\n};\n'
    path = os.path.join(work, "cost.scd")
    open(path, "w").write(base + body)
    out = subprocess.run([G.D.find_sclang(), "-i", "none", path], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=7200)
    log = out.stdout + out.stderr
    if "ALL DONE" not in log:
        print(log[-3000:])
        return 1
    t = {}
    for name, v in \
            [(m[0], float(m[2])) for m in re.findall(r"TIME (\S+?)_(\d+) ([0-9.]+)", log)]:
        t.setdefault(name, []).append(v)
    med = {k: sorted(v)[len(v) // 2] for k, v in t.items()}
    print("NRT seconds to render %.0f s, median of %d (this machine):\n" % (secs, reps))
    for pname, _ in PASSES:
        allt, floor = med["%s_all" % pname], med["%s_floor" % pname]
        print("[%s selected]  all on %.2f s   always-on floor %.2f s" % (pname, allt, floor))
        for k in PAUSABLE:
            c = allt - med["%s_%s" % (pname, k)]
            print("  %-9s %6.2f s  %5.1f%% of all" % (k, c, 100 * c / allt))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
