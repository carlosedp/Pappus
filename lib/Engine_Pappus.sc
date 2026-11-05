// Engine_Pappus
// Pappus - scatter to the wind. A chain of Torso S-4 style devices:
//
//   GRAINSWARM 1 and 2 (parallel) > RESONATOR > DELAY > COLOUR > REVERB >
//   out, where SIGNAL is the routing: each stage has an amount of each
//   granulator fed into it, so either one can skip any part of the chain
//
// GRAINSWARM continuously captures the input into a mono ring buffer and spawns
// grains from a movable playhead. eight independent voices, one per grid row,
// each with its own pitch.
//
// COLOUR is drive > crush > loss > envelope-following noise.

Engine_Pappus : CroneEngine {
	var <grp, <buf, <bufr, <buf2, <buf2r, <dbuf, <envbufs, <patbuf, <patbuf2, <mbus;
	var <loopbufs, graph, abus, nodes, nstate, ngen, params, <cut;
	// LITE. True on a machine that cannot carry the whole graph - see
	// prLiteMode for how that is decided. Read at alloc: LITE builds the
	// graph without GRAINSWARM 2 and the second tier of each resonator
	// model, with the cheap LOSS. Everything else is the same graph, and
	// on both, idle nodes are paused (see lib/pappus_graph.scd).
	var <lite = false;
	// the seven meters' last shared bus read, and when it was taken
	var mlast, mtime = 0;
	var bufdur = 60.0;
	var deldur = 11.0;

	*new { arg context, doneCallback;
		^super.new(context, doneCallback);
	}

	// alloc runs inside a Routine in CroneEngine and doneCallback only fires
	// once it returns. an uncaught error here means the script hangs on
	// "loading..." forever, so wrap it and report instead.
	alloc {
		try {
			this.prAlloc;
			"Engine_Pappus: loaded".postln;
		} { arg err;
			"### Engine_Pappus: alloc failed ###".postln;
			err.reportError;
		};
	}

	// ===================================================================
	// LITE - WHICH MACHINE IS THIS
	// ===================================================================
	// A factory norns is a Raspberry Pi 3 compute module. A shield is
	// whatever the owner put underneath it, which since 2019 is usually a
	// Pi 4. The whole graph - two granulators, forty-eight resonators, a
	// real short-time Fourier transform and one PlayBuf per file in audio/
	// - measures comfortable on a Pi 4 and lands at ninety per cent plus,
	// with audio dropouts, on a Pi 3. That is not a setting anybody should
	// have to find: the def that gets compiled should already fit the
	// machine it is compiled on.
	//
	// So: read the device tree, and if this is not a Pi 4 or newer, build
	// the LITE graph instead. What LITE gives up is listed at each site,
	// and pappus.lua runs THE SAME TEST (see lite_mode there) so the
	// script's own pages agree with the engine about what exists.
	//
	// Three inputs, in order of authority:
	//
	//   PAPPUS_LITE      an environment variable, for the test harness -
	//                    1/lite forces LITE, 0/full forces FULL
	//   data/pappus/     mode.txt, holding "lite" or "full" - what the
	//                    ENGINE param on PARAMS > PERFORMANCE writes, for
	//                    an owner who disagrees with the detection
	//   the device tree  bcm2711 (Pi 4, 400, CM4) and bcm2712 (Pi 5, CM5)
	//                    are FULL; any other Raspberry Pi is LITE
	//
	// A machine with no device tree at all - a desktop running scsynth,
	// which is where the tests run - is FULL. Unknown is not the same as
	// slow, and crippling a developer's laptop to be safe would hide the
	// full graph from the only place it is easy to look at.
	prLiteMode {
		var env, path, txt, dt = "";
		env = "PAPPUS_LITE".getenv;
		if(env.notNil) {
			env = env.asString.toLower;
			if(#["1", "lite", "true", "yes"].includes(env)) { ^true };
			if(#["0", "full", "false", "no"].includes(env)) { ^false };
		};
		path = Platform.userHomeDir ++ "/dust/data/pappus/mode.txt";
		if(File.exists(path)) {
			txt = File.readAllString(path);
			if(txt.notNil) {
				txt = txt.toLower;
				if(txt.contains("lite")) { ^true };
				if(txt.contains("full")) { ^false };
			};
		};
		// /proc/device-tree/compatible is NUL-separated - "raspberrypi,
		// 4-model-b\0brcm,bcm2711" - and readAllString hands the NULs back
		// as ordinary characters, so a substring search still finds either
		// half. Both files are read because a Compute Module names itself
		// in `model` and its SoC only in `compatible`.
		#["/proc/device-tree/compatible", "/proc/device-tree/model"].do { arg f;
			if(File.exists(f)) {
				var t = File.readAllString(f);
				if(t.notNil) { dt = dt ++ t.toLower ++ " " };
			};
		};
		if(dt.contains("bcm").not and: { dt.contains("raspberry").not }) { ^false };
		^#["bcm2711", "bcm2712", "pi 4", "pi 5",
			"compute module 4", "compute module 5"].any({ arg k;
				dt.contains(k) }).not;
	}

	// mkdir -p, one level at a time. File.mkdir will not create parents.
	prMakeDir { arg path;
		var parts, acc;
		if(path.isNil or: { path.isEmpty }) { ^this };
		parts = path.split($/);
		acc = "";
		parts.do { arg p;
			if(p.isEmpty.not) {
				acc = acc ++ "/" ++ p;
				if(File.exists(acc).not) { File.mkdir(acc) };
			};
		};
	}

	prAlloc {
		var srv = context.server;
		var envnums, patvals, audioDir;

		// FIRST, before anything is allocated: which graph is this going to
		// be. Everything below reads it.
		lite = this.prLiteMode;
		("Engine_Pappus: " ++ (lite.if({ "LITE" }, { "FULL" })) ++ " graph")
			.postln;

		// GRAINSWARM capture. TWO MONO BUFFERS PER GRANULATOR, left and
		// right, because GrainBuf reads a mono buffer - "the buffer holding a
		// mono audio signal", says its own help - and handing it a two
		// channel one would read the interleave as if it were a waveform.
		//
		// So the capture is genuinely stereo and the cost is one extra
		// interpolated buffer read per voice: the MAIN grain is read twice,
		// once per side. The SWARM duplicates are not - there are two of them
		// already, so one is pointed at each side and the pair costs exactly
		// what it did before while still carrying both.
		//
		// With a mono source the two buffers hold the same samples and every
		// number below comes out where it always did.
		buf = Buffer.alloc(srv, (bufdur * srv.sampleRate).asInteger, 1);
		bufr = Buffer.alloc(srv, (bufdur * srv.sampleRate).asInteger, 1);
		// ...and a second pair, because two granulators sharing a capture
		// buffer are not two granulators, they are one buffer read twice.
		// Separate SOS, separate LOCK, separate TILT baked into the recording:
		// that is the whole point of the pair.
		//
		// LITE has no second granulator, so this pair is a tenth of a second
		// rather than sixty - twenty-three megabytes of resident memory that
		// nothing would ever read. It is allocated rather than left nil so
		// that bufclear, snapwrite and snapread stay valid commands with a
		// real buffer at index 3 and 4: a snapshot saved on a Pi 4 and loaded
		// on a factory norns should load its first granulator and quietly
		// ignore the second, not raise inside the engine.
		buf2 = Buffer.alloc(srv,
			((lite.if({ 0.1 }, { bufdur })) * srv.sampleRate).asInteger, 1);
		buf2r = Buffer.alloc(srv,
			((lite.if({ 0.1 }, { bufdur })) * srv.sampleRate).asInteger, 1);

		// DELAY delay line. Mono: taps are panned out to stereo, and keeping
		// the feedback path mono is what stops the image wandering as it
		// regenerates.
		dbuf = Buffer.alloc(srv, (deldur * srv.sampleRate).asInteger, 1);

		// NOISE's loop sources - every .wav file dropped into audio/, next
		// to this class file's own directory, becomes a noise-type option,
		// in alphabetical order by filename. Lua runs the identical scan
		// (same folder, same filter, same sort - see scan_noise_loops in
		// pappus.lua) to build the matching menu labels, so nobody has to
		// keep two lists in sync by hand: drop a file into audio/ and it
		// shows up on both sides in the same slot. There is no built-in
		// level match between them - a dropped-in file is only as loud, in
		// this mix, as its own recorded peak.
		audioDir = PathName(Engine_Pappus.filenameSymbol.asString).pathOnly
			++ "../audio/";
		loopbufs = [];
		if(File.exists(audioDir)) {
			var files = PathName(audioDir).files.select({ arg pn;
				pn.extension.toLower == "wav" });
			files = files.sort({ arg a, b;
				a.fileName.toLower < b.fileName.toLower });
			loopbufs = files.collect({ arg pn; Buffer.read(srv, pn.fullPath) });
		};
		srv.sync;

		// EUCLID's gate pattern, sixteen steps. Starts all-open, which is what
		// EUCLID off means; Lua runs Bjorklund and overwrites it whenever the
		// density or the length moves.
		patvals = Array.fill(16, { 1 });

		// 17 grain envelope shapes, down-ramp .. sine .. up-ramp. Precomputed
		// so CONTOUR is a buffer selection at grain spawn rather than a buffer
		// rewrite on every encoder tick.
		//
		// Allocate everything first, sync once, then fill. Buffer.sendCollection
		// syncs with the server per buffer, which on a Pi is eighteen round
		// trips inside alloc; alloc blocking is what leaves norns on
		// "loading..." forever, so keep it to two.
		envbufs = (0..16).collect { Buffer.alloc(srv, 256, 1) };
		patbuf = Buffer.alloc(srv, patvals.size, 1);
		patbuf2 = Buffer.alloc(srv, patvals.size, 1);
		// SEVEN METERS, on a control bus. SIGNAL draws the signal flow with a
		// live level on every box, and a level Lua guessed at would be a
		// drawing of something nobody measured.
		//   1 GRAINSWARM 1   2 GRAINSWARM 2   3 RESONATOR
		//   4 DELAY        5 COLOUR         6 REVERB
		//   7 OUT
		mbus = Bus.control(srv, 7);
		mlast = Array.fill(7, 0);
		srv.sync;

		envbufs.do { arg b, i;
			var c = (i / 8) - 1;
			var p = (0.5 + (c * 0.5)).clip(0.04, 0.96);
			b.setn(0, Env([0, 1, 0], [p, 1 - p], \sine).discretize(256).as(Array));
		};
		patbuf.setn(0, patvals);
		patbuf2.setn(0, patvals);
		envnums = envbufs.collect({ arg b; b.bufnum });

		srv.sync;


		// THE GRAPH. Built from lib/pappus_graph.scd - see the notes at the
		// top of that file for why Pappus is many small nodes rather than
		// one SynthDef, and what makes pausing them inaudible.
		graph = (PathName(Engine_Pappus.filenameSymbol.asString).pathOnly
			++ "pappus_graph.scd").load;
		graph[\defs].value((lite: lite, bufdur: bufdur,
			nloops: loopbufs.size)).do({ arg d; d.add });
		srv.sync;

		abus = ();
		graph[\buses].keysValuesDo({ arg key, n;
			abus[key] = Bus.audio(srv, n);
		});

		params = graph[\defaults].copy;
		nodes = ();
		nstate = ();
		ngen = ();
		cut = Set.new;
		grp = Group.new(context.xg, \addToTail);

		graph[\order].do({ arg row;
			var key = row[0], def = row[1], pausable = row[2], wires = row[3];
			var args, skip = false;
			// LITE builds a smaller instrument: no second granulator, only
			// the first tier of each resonator model, the cheap LOSS.
			if(lite) {
				if(#[\cap2, \voice2, \modal_b, \string_b].includes(key)) {
					skip = true };
				if(key == \loss) { def = \pappus_losslite };
			};
			if(skip.not) {
				args = this.prCommonArgs;
				wires.pairsDo({ arg ctl, what;
					args = args ++ [ctl, this.prResolve(what)];
				});
				nodes[key] = Synth.new(def, args, grp, \addToTail);
				if(pausable) { nstate[key] = \on; ngen[key] = 0 };
			};
		});
		srv.sync;
		// Everything starts running; whatever the defaults leave silent is
		// paused once its hold has passed.
		this.prActivity;

		this.addCommand("epattern", "ffffffffffffffff", { arg msg;
			patbuf.setn(0, msg[1..16]);
		});
		this.addCommand("epattern2", "ffffffffffffffff", { arg msg;
			patbuf2.setn(0, msg[1..16]);
		});

		// Plain values: one name, one number, sent to every node that has
		// a control of that name.
		#[
			\mrate, \msize, \mcontour, \mscan, \mscanmode, \mdelay, \mspray,
			\mspraymode, \melen, \mephase, \mswarm, \mswarmmode, \mlock,
			\msos, \mbuflen, \mwinstart, \mwinend, \mstrum, \mtilt, \msrc,
			\nrate, \nsize, \ncontour, \nscan, \nscanmode, \ndelay, \nspray,
			\nspraymode, \nelen, \nephase, \nswarm, \nswarmmode, \nlock,
			\nsos, \nbuflen, \nwinstart, \nwinend, \nstrum, \ntilt, \nsrc,
			\pdamp, \pbright, \pstruct, \ppos, \pmodel, \pgrain, \pgraintype,
			\pwet, \kwow, \rverb, \rtime, \rshimmer, \rshimmersemi,
			\pin1, \pin2, \sin1, \sin2, \kin1, \kin2, \oin1, \oin2,
			\mcomp, \fade, \run, \scycle, \sfb, \stilt, \stiltxover,
			\sdiffuse, \swet, \shold, \drive, \crush, \crushmode, \loss,
			\noise, \noisetype, \noisedecay, \noisetone, \noisedyn,
			\bypass, \amp, \ingain, \limceil
		].do({ arg name;
			var ints = #[\mcontour, \mscanmode, \mspraymode, \mswarmmode,
				\mlock, \msrc, \ncontour, \nscanmode, \nspraymode, \nswarmmode,
				\nlock, \nsrc, \pmodel, \pgraintype, \run, \shold,
				\crushmode, \noisetype, \bypass];
			this.addCommand(name, if(ints.includes(name)) { "i" } { "f" },
				{ arg msg; this.prSet(name, msg[1]) });
		});

		// Arrays.
		[
			[\pfrq, 48], [\pamp, 48], [\pvfrq, 8], [\pvamp, 8],
			[\pitches, 8], [\probs, 8], [\gates, 8],
			[\pitches2, 8], [\probs2, 8], [\gates2, 8],
			[\taptimes, 8], [\taplevels, 8], [\tappans, 8], [\tappitch, 8]
		].do({ arg row;
			var name = row[0], n = row[1];
			this.addCommand(name, String.fill(n, $f), { arg msg;
				this.prSetn(name, msg[1..n]);
			});
		});

		this.addCommand("sync", "i", { arg msg; grp.set(\t_sync, 1); });

		7.do { arg i;
			this.addPoll(("meter" ++ (i + 1)).asSymbol, {
				var now = Main.elapsedTime;
				if(now - mtime > 0.005) {
					mlast = mbus.getnSynchronous(7);
					mtime = now;
				};
				mlast[i];
			});
		};
		this.addCommand("bufclear", "i", { arg msg;
			buf.zero; bufr.zero; buf2.zero; buf2r.zero;
		});
		this.addCommand("delayclear", "i", { arg msg;
			dbuf.zero;
		});
		this.addCommand("snapwrite", "isf", { arg msg;
			var b = [buf, bufr, buf2, buf2r][(msg[1] - 1).clip(0, 3)];
			var path = msg[2].asString;
			var n = (msg[3] * context.server.sampleRate).asInteger
				.clip(1, b.numFrames);
			this.prMakeDir(path.dirname);
			b.write(path, "wav", "int16", n, 0, false);
		});
		this.addCommand("snapread", "is", { arg msg;
			var b = [buf, bufr, buf2, buf2r][(msg[1] - 1).clip(0, 3)];
			var path = msg[2].asString;
			if (File.exists(path)) { b.read(path, 0, -1, 0, false) };
		});
		// Kept so older scripts that send it do not error. It set controls
		// the graph has not had for several versions.
		this.addCommand("route", "iiii", { arg msg; });
	}

	// Controls every node is given at creation; a node without that control
	// ignores it.
	prCommonArgs {
		var a = [\bg1, abus[\g1].index, \bg2, abus[\g2].index,
			\inbusl, context.in_b[0].index, \inbusr, context.in_b[1].index,
			\envnums, envbufs.collect({ arg b; b.bufnum })];
		if(loopbufs.size > 0) {
			a = a ++ [\loopnums, loopbufs.collect({ arg b; b.bufnum })];
		};
		^a
	}

	prResolve { arg what;
		if(what.isArray and: { what.isString.not }) {
			// [\mbus, slot]
			^mbus.index + what[1]
		};
		^switch(what,
			\buf, { buf.bufnum }, \bufr, { bufr.bufnum },
			\buf2, { buf2.bufnum }, \buf2r, { buf2r.bufnum },
			\dbuf, { dbuf.bufnum },
			\patbuf, { patbuf.bufnum }, \patbuf2, { patbuf2.bufnum },
			\outbus, { context.out_b.index },
			{ abus[what].index }
		)
	}

	prSet { arg name, val;
		grp.set(name, val);
		if(params.includesKey(name)) {
			params[name] = val;
			this.prActivity;
		};
	}

	prSetn { arg name, vals;
		grp.setn(name, vals);
		if(params.includesKey(name)) {
			params[name] = vals;
			this.prActivity;
		};
	}

	// PAUSING. For each pausable node: is it needed (its cond, and not cut
	// by the governor)? Needed and paused: run it and fade it in. Not
	// needed and running: fade it out, and pause it once its hold has
	// passed - unless it was needed again in the meantime, which the
	// generation counter catches.
	prActivity {
		nstate.keysDo({ arg key;
			var cond = graph[\conds][key];
			var want = (cond.isNil or: { cond.value(params) })
				and: { cut.includes(key).not };
			var n = nodes[key];
			if(want) {
				if(nstate[key] != \on) {
					ngen[key] = ngen[key] + 1;
					if(nstate[key] == \off) { n.run(true) };
					n.set(\act, 1);
					nstate[key] = \on;
				};
			} {
				if(nstate[key] == \on) {
					var gen, hold = graph[\holds][key];
					hold = if(hold.isNil) { 0.25 } { hold.value(params) };
					ngen[key] = ngen[key] + 1;
					gen = ngen[key];
					n.set(\act, 0);
					nstate[key] = \pending;
					SystemClock.sched(hold, {
						if(ngen[key] == gen and: { nstate[key] == \pending }) {
							n.run(false);
							nstate[key] = \off;
						};
						nil
					});
				};
			};
		});
	}

	free {
		grp.free;
		abus.do({ arg b; b.free });
		buf.free;
		bufr.free;
		buf2.free;
		buf2r.free;
		mbus.free;
		dbuf.free;
		patbuf.free;
		patbuf2.free;
		envbufs.do({ arg b; b.free });
		loopbufs.do({ arg b; b.free });
	}
}
