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
	// the governor (prGovTick): budget in % of scsynth's own DSP load, 0 =
	// off; the ladder level in force; each step's measured saving
	var <budget = 80, <level = 0, gsave, gpend, gbefore, glast = 0, gcalm = 0,
		gtask, <gcpu = 0;
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
			if(#["1", "lite", "true", "yes"].includesEqual(env)) { ^true };
			if(#["0", "full", "false", "no"].includesEqual(env)) { ^false };
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
				if(#[\cap2, \voice2, \modal_b, \string_b, \losslite]
					.includes(key)) { skip = true };
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

		gsave = Array.fill(graph[\ladder].size, { nil });
		gtask = Routine({ loop { this.prGovTick; 0.5.wait } }).play(SystemClock);
		this.addCommand("budget", "f", { arg msg;
			budget = msg[1];
			if(budget <= 0 or: { budget >= 100 }) { this.prGovLevel(0) };
		});
		this.addPoll(\governor, { level });

		this.addCommand("epattern", "ffffffffffffffff", { arg msg;
			patbuf.setn(0, msg[1..16]);
		});
		this.addCommand("epattern2", "ffffffffffffffff", { arg msg;
			patbuf2.setn(0, msg[1..16]);
		});

		// Plain values: one name, one number, sent to every node that has
		// a control of that name.
		//
		// The two tables between COMMANDS BEGIN and END are read by the Lua
		// test mock (test/mock_norns.lua) for the list of engine commands:
		// every \name in them is one.
		// COMMANDS BEGIN
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
		// COMMANDS END
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
		// SAMPLE LOADING: a sound file into a GRAINSWARM's capture pair,
		// left channel to the left buffer, right (or the only channel) to
		// the right. Whatever was there is cleared first, so a file shorter
		// than the loop is followed by silence rather than old audio. The
		// script locks the swarm before sending this, or the record head
		// would write over the file as soon as it landed, and sets BUFFR to
		// the file's length. Longer than the 60 s buffer is cut at 60 s.
		this.addCommand("sampleload", "is", { arg msg;
			this.prSampleLoad(msg[1].clip(1, 2), msg[2].asString);
		});

		// Kept so older scripts that send it do not error. It set controls
		// the graph has not had for several versions.
		this.addCommand("route", "iiii", { arg msg; });
	}

	// The capture buffers must stay at the server's rate: the record head,
	// the loop length and every grain position count samples at it. A file
	// at that rate is read straight in. A file at any other rate (the
	// bundled loops are 44.1 kHz) cannot be: /b_readChannel would retag the
	// buffer with the file's rate (measured), so the loop would be the wrong
	// length and recording into it afterwards would skip frames. So it is
	// resampled here, linearly, and sent in at the server's rate.
	//
	// Synchronously, on purpose. Done in a routine that yields every so
	// often, the routine's own variables came back holding the clock's
	// wake-up values after the first yield. Blocking sclang for the length
	// of the resample (about 0.3 s per 20 s of stereo on a desktop, a few
	// seconds on a Pi 3) delays engine commands and polls, not audio: the
	// server keeps running. The script fades out around a load anyway.
	prSampleLoad { arg w, path;
		var srv = context.server;
		var bl = [buf, buf2][w - 1], br = [bufr, buf2r][w - 1];
		var sf = SoundFile.openRead(path);
		var nch, sr, rc, nin, data, ratio, m, l, r;
		if(sf.isNil) {
			("Engine_Pappus: cannot read sample " ++ path).postln;
			^this
		};
		nch = sf.numChannels;
		sr = sf.sampleRate;
		rc = (nch > 1).if(1, 0);
		bl.zero;
		br.zero;
		if(sr == srv.sampleRate) {
			sf.close;
			bl.readChannel(path, 0, -1, 0, false, [0]);
			br.readChannel(path, 0, -1, 0, false, [rc]);
		} {
			nin = sf.numFrames.min((bufdur * sr).asInteger + 2);
			data = FloatArray.newClear(nin * nch);
			sf.readData(data);
			sf.close;
			ratio = sr / srv.sampleRate;
			m = (((nin - 1) / ratio).floor.asInteger).min(bl.numFrames);
			l = FloatArray.newClear(m);
			r = FloatArray.newClear(m);
			m.do({ arg i;
				var x = i * ratio, j = x.asInteger, f = x - j;
				var ia = j * nch, ib = (j + 1).min(nin - 1) * nch;
				l[i] = data[ia] + ((data[ib] - data[ia]) * f);
				r[i] = data[ia + rc] + ((data[ib + rc] - data[ia + rc]) * f);
			});
			// written at the server's rate and read in, so the buffers
			// keep it
			bl.loadCollection(l, 0);
			br.loadCollection(r, 0);
		};
		("Engine_Pappus: GRAINSWARM " ++ w ++ " <- " ++ path
			++ " (" ++ sr ++ " Hz)").postln;
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
	// generation counter catches. A substitute (graph[\subs]) is needed
	// only while the node it stands in for is cut.
	prWant { arg key;
		var cond = graph[\conds][key];
		var subs = graph[\subs] ?? { () };
		var need = cond.isNil or: { cond.value(params) };
		var orig = subs.findKeyForValue(key);
		^if(orig.notNil) {
			need and: { cut.includes(orig) }
		} {
			need and: { cut.includes(key).not }
		}
	}

	prActivity {
		nstate.keysDo({ arg key;
			var want = this.prWant(key);
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

	// THE GOVERNOR. Holds scsynth's DSP load under `budget` by walking the
	// graph's ladder (see pappus_graph.scd): over budget, apply the next
	// step; comfortably under for long enough, take the last one back.
	//
	// It measures rather than predicts. When a step is applied, the load
	// before is remembered and the load once it has settled is compared, and
	// that difference is the step's saving ON THIS MACHINE WITH THIS PATCH.
	// A step only comes back when the load plus its saving (and a margin)
	// would still fit - so it does not take a step back only to need it
	// again a second later. Steps that would change nothing right now (the
	// node is already paused) are passed over in both directions.
	prGovTick {
		var cpu = context.server.avgCPU ? 0, now = Main.elapsedTime;
		var maxl = graph[\ladder].size;
		gcpu = cpu;
		if(budget <= 0 or: { budget >= 100 }) { ^this };
		if(now - glast < 1.2) { ^this };    // let the last change settle
		if(gpend.notNil) {
			gsave[gpend] = (gbefore - cpu).max(1);
			gpend = nil;
		};
		if(cpu > budget and: { level < maxl }) {
			var l = level;
			gbefore = cpu;
			// apply the next step that would actually take something away
			while({ l < maxl and: { this.prStepUseful(l).not } }) { l = l + 1 };
			if(l < maxl) {
				this.prGovLevel(l + 1);
				gpend = l;
				glast = now;
			};
			gcalm = 0;
		} {
			if(level > 0) {
				var s = gsave[level - 1] ? 10;
				if(this.prStepUseful(level - 1).not) {
					// nothing to give back: just step down past it
					this.prGovLevel(level - 1);
					gcalm = 0;
				} {
					if((cpu + (s * 1.2)) < (budget - 5)) { gcalm = gcalm + 1 } { gcalm = 0 };
					if(gcalm >= 4) {
						this.prGovLevel(level - 1);
						glast = now;
						gcalm = 0;
					};
				};
			};
		};
	}

	// Would applying ladder step i take anything away right now?
	prStepUseful { arg i;
		var step = graph[\ladder][i];
		^if(step[0] == \cut) {
			step[1].any({ arg k; this.prCuttable(k) and: { nstate[k] == \on } })
		} {
			#[\voice1, \voice2].any({ arg k; nstate[k] == \on })
		}
	}

	// A node can be cut if it exists - and, if it has a substitute, the
	// substitute exists too (LITE's LOSS is already the cheap one).
	prCuttable { arg k;
		var sub = (graph[\subs] ?? { () })[k];
		^nodes[k].notNil and: { sub.isNil or: { nodes[sub].notNil } }
	}

	prGovLevel { arg l;
		var dupcut = 1, gcap = 1000, vmax = 8;
		level = l.clip(0, graph[\ladder].size);
		cut = Set.new;
		graph[\ladder].keep(level).do({ arg step;
			if(step[0] == \cut) {
				step[1].do({ arg k; if(this.prCuttable(k)) { cut.add(k) } });
			} {
				if(step[1] == \dupcut) { dupcut = step[2] };
				if(step[1] == \gcap) { gcap = step[2] };
				if(step[1] == \vmax) { vmax = step[2] };
			};
		});
		#[\voice1, \voice2].do({ arg k;
			nodes[k] !? { arg n; n.set(\dupcut, dupcut, \gcap, gcap, \vmax, vmax) };
		});
		this.prActivity;
	}

	free {
		gtask.stop;
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
