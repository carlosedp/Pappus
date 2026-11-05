// pappus_graph.scd - the Pappus signal graph, as separate nodes.
//
// WHY THIS FILE EXISTS
//
// Pappus used to be one SynthDef. In scsynth every UGen in a running synth is
// computed every block whether anyone can hear it or not, so the resonator
// with its wet knob at zero, the delay taps with the delay dry, the LOSS
// transform with LOSS at zero, the second granulator with SOS at zero - all
// of it cost the same as if it were cranked. On a Pi 4 that was affordable;
// on a factory norns (Pi 3) the idle cost alone was most of the machine.
//
// Here every module is its own small SynthDef, the modules talk over private
// audio buses, and the engine PAUSES any node whose output cannot currently
// be heard. A paused node costs nothing. The rule that makes this sound the
// same as the single def:
//
//   In.ar returns SILENCE for a bus nobody wrote this block. So a paused
//   node is indistinguishable, downstream, from a node outputting zero - and
//   every node here is only paused when its contribution already IS zero
//   (its wet / send / amount control is at 0).
//
// Modules that process in place (LOSS, DIFFUSE, REVERB) use ReplaceOut: when
// they are paused the bus passes through untouched, which is exactly their
// dry path.
//
// Each pausable node also has an `act` control (lagged, 50 ms) multiplying
// its contribution. The engine fades `act` to 0 before pausing, so a node can
// also be taken away while it IS audible - which is what the CPU governor
// will use - without a click.
//
// Parameter names are identical to the old single def's, so the engine can
// keep sending every command to the parent group with n_set: a node that
// does not have that control ignores it.
//
// This is a .scd, not a .sc, on purpose: norns compiles every .sc under dust
// into the class library, and this is not a class. Engine_Pappus loads it at
// alloc, and the test harness in test/ loads the SAME file, so the graph that
// is tested is the graph that ships.
//
// The file evaluates to an Event:
//   defs:  { |e| -> Array of SynthDefs }      e: see below
//   order: Array of [key, defName, pausable]   in execution order
//   conds: Event of key -> { |p| Boolean }     when a pausable node is needed
//   holds: Event of key -> { |p| seconds }     how long to wait before pausing
//
// e (built by the engine, or stubbed by the tests) holds:
//   lite, bufdur, nloops

(
var lagt = 0.02, envref = 0.5;
var frnd = [0, 0.41, -0.77, 0.19, 0.93, -0.35, 0.61, -0.12];

// A swarm's sends into a destination: GRAINSWARM 1 by a, GRAINSWARM 2 by b.
// Every consumer computes its own - it is two multiplies a channel - which is
// what lets each module be its own node.
var gfeed = { arg a, b;
	var g1 = In.ar(\bg1.kr(0), 2), g2 = In.ar(\bg2.kr(0), 2);
	(g1 * Lag.kr(a, 0.05)) + (g2 * Lag.kr(b, 0.05))
};

// The activity fade every pausable node carries.
var actk = { Lag.kr(\act.kr(1), 0.05) };

// One looping player per bank, pointed at the chosen file - what LITE always
// did. FULL used to run every file in audio/ continuously and Select between
// them; switching was a hard cut either way, so the only thing this changes
// is where in the new file a switch lands.
var loopplay = { arg nloops, sel, rate;
	var nums = \loopnums.kr(0 ! nloops);
	var bn = Select.kr(sel.clip(0, nloops - 1), nums);
	PlayBuf.ar(2, bn, rate * BufRateScale.kr(bn), loop: 1)
};

// GRAINSWARM, capture half: input select, tilt, the sound-on-sound record
// loop, and the dry passthrough share of SOS's crossfade. Always running -
// the buffer must keep recording whether or not grains are playing.
var capdef = { arg name, pfx;
	var k = { arg n, d; (pfx ++ n).asSymbol.kr(d) };
	SynthDef(name, {
		var in = In.ar(\bin.kr(0), 2);
		var bufl = \bufl.kr(0), bufr = \bufr.kr(0);
		var src = k.("src", 1), tilt = k.("tilt", 0), msos = k.("sos", 0),
			mlock = k.("lock", 0), mbuflen = k.("buflen", 8);
		var run = \run.kr(1);
		var ssel, sst, sml, smr, capl, capr, mtl, frames, sos, sosret, sosin,
			wlen, wphase, oldl, oldr, sxf;
		ssel = src.round(1);
		sst = Lag.kr((ssel > 1.5) * (ssel < 2.5), 0.02);
		sml = Lag.kr((ssel > 2.5) * (ssel < 3.5), 0.02);
		smr = Lag.kr((ssel > 3.5) * (ssel < 4.5), 0.02);
		capl = (in[0] * (sst + sml)) + (in[1] * smr);
		capr = (in[1] * (sst + smr)) + (in[0] * sml);
		mtl = Lag.kr(tilt, 0.05).clip(-1, 1);
		capl = BHiShelf.ar(BLowShelf.ar(capl, 700, 1, 0 - (mtl * 12)),
			700, 1, mtl * 12);
		capr = BHiShelf.ar(BLowShelf.ar(capr, 700, 1, 0 - (mtl * 12)),
			700, 1, mtl * 12);
		frames = BufFrames.kr(bufl);
		sos = Lag.kr(msos, lagt).max(mlock).clip(0, 1);
		sosret = (sos * 1.05).clip(0, 1);
		sosin = ((1 - sos) * 4).clip(0, 1) * Lag.kr(run, 0.02)
			* Lag.kr(ssel > 1.5, 0.02);
		wlen = (Lag.kr(mbuflen, 0.1) * SampleRate.ir).clip(4800, frames);
		wphase = Phasor.ar(0, BufRateScale.kr(bufl), 0, wlen);
		oldl = BufRd.ar(1, bufl, wphase, 1, 1);
		oldr = BufRd.ar(1, bufr, wphase, 1, 1);
		BufWr.ar(((capl * sosin) + (oldl * sosret)).clip2(1), bufl, wphase, 1);
		BufWr.ar(((capr * sosin) + (oldr * sosret)).clip2(1), bufr, wphase, 1);
		sxf = (Lag.kr(msos, lagt).max(mlock).clip(0, 1) / 0.6).clip(0, 1);
		Out.ar(\bph.kr(0), wphase);
		Out.ar(\bout.kr(0), [capl, capr] * (sxf * 0.5pi).cos);
	})
};

// GRAINSWARM, voice half: the eight-voice cloud. This is the expensive one,
// and it is silent unless SOS (or LOCK) is up and a voice is gated - the
// grains are crossfaded in by the same SOS curve that fades the dry out.
var voicedef = { arg name, pfx, sfx, bufdur;
	var k = { arg n, d; (pfx ++ n).asSymbol.kr(d) };
	SynthDef(name, {
		var bufl = \bufl.kr(0), bufr = \bufr.kr(0), gpat = \pat.kr(0);
		var mrate = k.("rate", 8), msize = k.("size", 0.12),
			mcontour = k.("contour", 8), mbuflen = k.("buflen", 8),
			mwinstart = k.("winstart", 0), mwinend = k.("winend", 1),
			mstrum = k.("strum", 0), mscan = k.("scan", 0.666),
			mscanmode = k.("scanmode", 1), mdelay = k.("delay", 0.25),
			mspray = k.("spray", 0), mspraymode = k.("spraymode", 1),
			melen = k.("elen", 1), mephase = k.("ephase", 0),
			mswarm = k.("swarm", 0), mswarmmode = k.("swarmmode", 1),
			mlock = k.("lock", 0), msos = k.("sos", 0);
		var pitches = ("pitches" ++ sfx).asSymbol.kr(0 ! 8);
		var gates = ("gates" ++ sfx).asSymbol.kr([1, 0, 0, 0, 0, 0, 0, 0]);
		var probs = ("probs" ++ sfx).asSymbol.kr(1 ! 8);
		var envnums = \envnums.kr(0 ! 17);
		var run = \run.kr(1), t_sync = \t_sync.tr(0);
		// The governor's two handles on a cloud (see Engine_Pappus
		// prGovern). dupcut fades SWARM's duplicate grains out; gcap caps
		// how many grains a voice may have sounding at once, by shortening
		// them. At their defaults neither does anything.
		var dupcut = Lag.kr(\dupcut.kr(1), 0.15);
		var gcap = Lag.kr(\gcap.kr(1000), 0.3);
		// ...and the last resort, voice stealing: at most vmax of the gated
		// voices play, the lowest rows first, so the top rows give way.
		var vmax = \vmax.kr(8);
		var vcount = 0, vallow;
		var wphase = In.ar(\bph.kr(0), 1);
		var frames, wlen, loopfrac, wpos, winlo, winhi, winspan, winspansec,
			dur, gph, trig, ktrig, estep, scanstretch, delaypos, scanpos,
			envsel, nvoices, swarm, dupgain, swiva, swivb, unison, strumsp,
			prnd, warpa, warpb, gsum, gover, sxf;

		frames = BufFrames.kr(bufl);
		wlen = (Lag.kr(mbuflen, 0.1) * SampleRate.ir).clip(4800, frames);
		loopfrac = wlen / frames;
		wpos = wphase / wlen;
		winlo = Lag.kr(mwinstart, lagt).clip(0, 0.99);
		winhi = Lag.kr(mwinend, lagt).clip(0.01, 1).max(winlo + 0.01);
		winspan = winhi - winlo;
		winspansec = (winspan * wlen / SampleRate.ir).max(0.002);
		dur = Lag.kr(msize, lagt).clip(0.002, 8).min(winspansec)
			.min(gcap / Lag.kr(mrate, lagt).max(0.05));

		gph = Phasor.ar(K2A.ar(t_sync),
			Lag.kr(mrate, lagt).max(0.05) * SampleDur.ir, 0, 1);
		trig = ((gph - Delay1.ar(gph)) < 0) + Impulse.ar(0);
		trig = trig * (run > 0.5);
		estep = Stepper.ar(trig, 0, 0, (melen - 1).max(0), 1);

		// TWO RATES. When a grain starts and which voices it starts on (the
		// euclid step, the gates, strum) stay at audio rate, and so does
		// WHERE it starts - the scan position. Dense grains at 1x read the
		// same timeline and add up coherently; a position even a few
		// samples stale per grain smears that into comb filtering (measured:
		// -2.4 dB and a hollow sound at 200 Hz in delay-follow).
		//
		// What only supplies a random VALUE for a grain - spray offset, pan,
		// jitter, whether the probability coin lands - is drawn at control
		// rate. GrainBuf samples its inputs once, at the trigger, so the
		// only difference is that a grain's draws are made at the trigger
		// before it rather than its own: still one fresh, independent draw
		// per grain. Those draws, and the Selects and arithmetic on them,
		// were most of what a voice cost: eight voices, two swarms.
		//
		// ktrig: a control-rate trigger in the block after each grain
		// trigger. Trig1 holds for exactly one block, so it is always still
		// high at the start of the next one, which is what A2K samples.
		ktrig = A2K.kr(Trig1.ar(trig, ControlDur.ir));

		scanstretch = Phasor.ar(0,
			((Lag.kr(mscan, lagt) * 3) - 1) / (wlen.max(1)), 0, 1);
		delaypos = (wpos - (Lag.kr(mdelay, lagt)
			/ Lag.kr(mbuflen, 0.1).max(0.1))).wrap(0, 1);
		scanpos = Select.ar(mscanmode - 1, [
			scanstretch, K2A.ar(Lag.kr(mscan, lagt)), delaypos, delaypos
		]);

		envsel = Select.kr(mcontour.clip(0, 16), envnums);
		vallow = gates.collect({ arg gt;
			vcount = vcount + (gt > 0.001);
			vcount <= vmax
		});
		// gates are levels, not just on/off - the sum is what normalises
		nvoices = (gates * vallow).sum.max(1);

		swarm = Lag.kr(mswarm, lagt).clip(0, 1);
		dupgain = (swarm * 3).clip(0, 1) * dupcut;
		swiva = Select.kr(mswarmmode - 1, [0, -7, -12,  7]);
		swivb = Select.kr(mswarmmode - 1, [0,  7,  12, 12]);
		unison = (swivb - swiva).abs < 0.5;

		strumsp = (Lag.kr(mstrum, lagt).clip(0, 0.125)
			* (1 / Lag.kr(mrate, lagt).max(0.05))).clip(0, 2);

		prnd = TRand.kr(0, 1, ktrig);
		warpa = LFNoise2.kr(0.3 + (mspray * 3));
		warpb = LFNoise2.kr(0.2 + (mspray * 2));

		gsum = DC.ar([0, 0]);
		8.do { arg i;
			var vtrig, dtrig, rnd, rnd2, off, pan, pos, base, g, coin, egate;
			var main, mainl, mainr, da, db, dsp, dupa, v, gjit;
			coin = ((prnd + frnd[i]).wrap(0, 1)) < probs[i];
			egate = Index.ar(gpat, (estep + (mephase * i)).floor % melen.max(1));
			vtrig = TDelay.ar(trig * (gates[i] > 0.001) * vallow[i] * coin * egate,
				strumsp * i);
			dtrig = vtrig * (dupgain > 0.001);
			rnd = TRand.kr(-1, 1, ktrig);
			rnd2 = TRand.kr(-1, 1, ktrig);
			off = Select.kr(mspraymode - 1, [rnd, rnd, warpa, warpa])
				* Lag.kr(mspray, lagt) * 0.25;
			pan = Select.kr(mspraymode - 1, [rnd2, 0, warpb, 0])
				* Lag.kr(mspray, lagt);
			gjit = TRand.kr(-1, 1, ktrig) * ((dur * 0.04).min(0.008) / bufdur);
			pos = ((winlo + ((scanpos + off + gjit).wrap(0, 1) * winspan))
				* loopfrac);
			base = pitches[i];
			g = Lag.kr(gates[i] * vallow[i], 0.02);

			mainl = GrainBuf.ar(1, vtrig, dur, bufl,
				2 ** (base / 12), pos, 2, 0, envsel, 16);
			mainr = GrainBuf.ar(1, vtrig, dur, bufr,
				2 ** (base / 12), pos, 2, 0, envsel, 16);
			main = Balance2.ar(mainl, mainr, pan);

			dsp = (swarm * 0.4) + ((swarm ** 4) * 6);
			da = swiva - dsp + (LFNoise2.kr(0.07 + (i * 0.011))
				* ((swarm * 0.25) + ((swarm ** 4) * 1.5)));
			db = swivb + dsp + (LFNoise2.kr(0.09 + (i * 0.013))
				* ((swarm * 0.25) + ((swarm ** 4) * 1.5)));

			dupa = GrainBuf.ar(2, dtrig, dur, bufl,
				2 ** ((base + da) / 12), pos, 2,
				(pan - (swarm * 0.7)).clip2(1), envsel, 8)
				+ GrainBuf.ar(2, dtrig, dur, bufr,
					2 ** ((base + db) / 12), pos, 2,
					(pan + (swarm * 0.7)).clip2(1), envsel, 8);

			v = (main + (dupa * dupgain)) / (1 + (2 * dupgain)).sqrt
				* (1 + (unison * swarm * 0.8)) * g;
			gsum = gsum + v;
		};
		gsum = gsum / nvoices.sqrt;
		// a capped overlap is normalised as the overlap it now is, so the
		// cap thins the cloud without making it quieter
		gover = (Lag.kr(msize, lagt) * Lag.kr(mrate, lagt)).min(gcap).clip(2, 40);
		gsum = gsum * (2 / gover).sqrt;
		gsum = gsum * 1.66;

		sxf = (Lag.kr(msos, lagt).max(mlock).clip(0, 1) / 0.6).clip(0, 1);
		Out.ar(\bout.kr(0), gsum * (sxf * 0.5pi).sin * actk.value);
	})
};

(
defs: { arg e;
	var lite = e[\lite], nloops = e[\nloops], bufdur = e[\bufdur];
	var defs = List.new;

	defs.add(SynthDef(\pappus_in, {
		var in = LeakDC.ar([In.ar(\inbusl.kr(0), 1), In.ar(\inbusr.kr(1), 1)])
			* Lag.kr(\ingain.kr(1), 0.05);
		Out.ar(\bout.kr(0), in);
	}));

	defs.add(capdef.(\pappus_cap1, "m"));
	defs.add(voicedef.(\pappus_voice1, "m", "", bufdur));
	if(lite.not) {
		defs.add(capdef.(\pappus_cap2, "n"));
		defs.add(voicedef.(\pappus_voice2, "n", "2", bufdur));
	};

	// RESONATOR's GRAIN layer - noise or a loop, following the input's
	// envelope. Only needed when GRAIN and the resonator's WET are both up.
	defs.add(SynthDef(\pappus_rnoise, {
		var pgrain = \pgrain.kr(0), pgraintype = \pgraintype.kr(2);
		var pin = gfeed.(\pin1.kr(0.7), \pin2.kr(0.7));
		var pmono = (pin[0] + pin[1]) * 0.5;
		var pgwash, pgloop, pglp;
		pgwash = Select.ar((pgraintype - 1).clip(0, 2),
			[WhiteNoise.ar(1), PinkNoise.ar(1.6), Dust2.ar(1800)]);
		pgwash = BPF.ar(pgwash, 2200, 0.5);
		if(nloops > 0) {
			var s = loopplay.(nloops, pgraintype - 4, 1);
			pgloop = (s[0] + s[1]) * 0.5;
			pglp = Lag.kr(pgraintype > 3.5, 0.05);
		} {
			pgloop = DC.ar(0);
			pglp = DC.kr(0);
		};
		Out.ar(\bout.kr(0), ((pgwash * (1 - pglp)) + (pgloop * pglp))
			* Amplitude.ar(pmono, 0.003, 0.05)
			* Lag.kr(pgrain, lagt).clip(0, 1) * 4 * actk.value);
	}));

	// RESONATOR excitation: the sends in, plus GRAIN, high-passed and
	// BRIGHTNESS-filtered. Also the two GRAINSWARM meters, because this node
	// always runs and reads both swarm buses anyway.
	defs.add(SynthDef(\pappus_rpre, {
		var pin = gfeed.(\pin1.kr(0.7), \pin2.kr(0.7));
		var pmono = (pin[0] + pin[1]) * 0.5;
		var pana = pmono + In.ar(\bnz.kr(0), 1);
		var g1 = In.ar(\bg1.kr(0), 2), g2 = In.ar(\bg2.kr(0), 2);
		pana = HPF.ar(pana, 35);
		pana = LPF.ar(pana, 300 * (53 ** Lag.kr(\pbright.kr(0.5), lagt).clip(0, 1)));
		Out.ar(\bout.kr(0), pana);
		Out.kr(\mbus.kr(0), [
			Amplitude.kr((g1[0] + g1[1]) * 0.5, 0.01, 0.2),
			if(lite) { DC.kr(0) } { Amplitude.kr((g2[0] + g2[1]) * 0.5, 0.01, 0.2) }
		]);
	}));

	// MODAL, in two tiers of twenty-four resonators. Tier A is exactly what
	// LITE always ran - partials one to three of each voice, alternating
	// sides voice by voice. Tier B is the other half, partials four to six.
	// For even voices the two together are the old FULL layout exactly; for
	// odd voices they are its mirror image, left for right. Run alone, tier A
	// is the LITE sound; that is the cut the governor makes first.
	[\a, \b].do { arg tier;
		defs.add(SynthDef(("pappus_modal_" ++ tier).asSymbol, {
			var pfrq = \pfrq.kr(0 ! 48), pamp = \pamp.kr(0 ! 48);
			var pana = In.ar(\bin.kr(0), 1);
			var prng = 0.003 * (900 ** Lag.kr(\pdamp.kr(0.35), lagt).clip(0, 1));
			var mfade = Lag.kr(\pmodel.kr(1) - 1, lagt).clip(0, 1) * 0.5pi;
			var li = [], ri = [], pfl, pfr, pal, par, pones, fmodal;
			8.do { arg v;
				var o = (v * 6) + (if(tier == \a) { 0 } { 3 });
				var a = o, b = o + 1, c = o + 2;
				// tier A: even voices a,c left; tier B flips it, so that
				// for even voices the union is "even band index = left".
				var flip = v.odd.xor(tier == \b);
				if(flip.not) {
					li = li ++ [a, c]; ri = ri ++ [b];
				} {
					ri = ri ++ [a, c]; li = li ++ [b];
				};
			};
			pones = Array.fill(li.size, 1);
			pfl = li.collect({ arg ix; Lag.kr(pfrq[ix], 0.012) });
			pfr = ri.collect({ arg ix; Lag.kr(pfrq[ix], 0.012) });
			pal = li.collect({ arg ix; Lag.kr(pamp[ix], 0.03) });
			par = ri.collect({ arg ix; Lag.kr(pamp[ix], 0.03) });
			fmodal = [
				DynKlank.ar(`[pfl, pal, pones], pana, 1, 0, prng),
				DynKlank.ar(`[pfr, par, pones], pana, 1, 0, prng)
			];
			fmodal = fmodal * (0.076 / ((1 + (prng * 12)) ** 0.12));
			Out.ar(\bout.kr(0), fmodal * mfade.cos * actk.value);
		}));
	};

	// STRING, in two tiers: A is the main comb of each voice (what LITE ran),
	// B is the detuned second comb that gives STRUCTURE its inharmonicity.
	[\a, \b].do { arg tier;
		defs.add(SynthDef(("pappus_string_" ++ tier).asSymbol, {
			var pvfrq = \pvfrq.kr(0 ! 8), pvamp = \pvamp.kr(0 ! 8);
			var pana = In.ar(\bin.kr(0), 1);
			var mfade = Lag.kr(\pmodel.kr(1) - 1, lagt).clip(0, 1) * 0.5pi;
			var sdec = 0.05 * (200 ** Lag.kr(\pdamp.kr(0.35), lagt).clip(0, 1));
			var sdet = 1 + (Lag.kr(\pstruct.kr(0), lagt).clip(-1, 1) * 0.06);
			var spos = Lag.kr(\ppos.kr(0.18), lagt).clip(0, 1);
			var fstring = DC.ar([0, 0]);
			8.do { arg i;
				var svfrq = Lag.kr(pvfrq[i].clip(20, 8000), 0.012);
				var svamp = Lag.kr(pvamp[i], 0.03);
				var period = 1 / svfrq;
				var exc = pana - DelayC.ar(pana, 0.06, spos * period);
				var voice = if(tier == \a) {
					CombL.ar(exc, 0.06, period, sdec)
				} {
					CombL.ar(exc, 0.06, period * sdet, sdec) * 0.35
				};
				fstring = fstring + Pan2.ar(voice * svamp, 0);
			};
			fstring = fstring * (0.6 / ((1 + (sdec * 3)) ** 0.3));
			Out.ar(\bout.kr(0), fstring * mfade.sin * actk.value);
		}));
	};

	// RESONATOR out: limiter, WET blend against the dry sends, brickwall.
	defs.add(SynthDef(\pappus_rpost, {
		var pin = gfeed.(\pin1.kr(0.7), \pin2.kr(0.7));
		var fsum = Limiter.ar(In.ar(\bres.kr(0), 2), 0.9, 0.02);
		var fwt = Lag.kr(\pwet.kr(0), lagt).clip(0, 1);
		var presig = (pin * (fwt * 0.5pi).cos) + (fsum * (fwt * 0.5pi).sin);
		presig = Limiter.ar(presig, (-1).dbamp, 0.05);
		Out.kr(\mbus.kr(0), Amplitude.kr((presig[0] + presig[1]) * 0.5, 0.01, 0.2));
		Out.ar(\bout.kr(0), presig);
	}));

	// DELAY, write side: always running, so the line holds the recent past
	// whenever the taps come back. Writes its own phase for the taps to read.
	defs.add(SynthDef(\pappus_dwrite, {
		var dbuf = \dbuf.kr(0);
		var sdry = In.ar(\bin.kr(0), 2) + gfeed.(\sin1.kr(0), \sin2.kr(0));
		var shold = \shold.kr(0);
		var dframes = BufFrames.kr(dbuf);
		var dph = Phasor.ar(0, BufRateScale.kr(dbuf), 0, dframes);
		var stl = Lag.kr(\stilt.kr(0), lagt);
		var sxo = Lag.kr(\stiltxover.kr(900), lagt).clip(80, 8000);
		var fbtime = Lag.kr(\scycle.kr(1.0), 0.05).clip(0.005, 10);
		var fbrd = BufRd.ar(1, dbuf,
			(dph - (fbtime * SampleRate.ir)).wrap(0, dframes), 1, 2);
		var fblo = LPF.ar(fbrd, sxo);
		var fbcol = (fblo * (stl.neg * 9).dbamp) + ((fbrd - fblo) * (stl * 9).dbamp);
		var fbamt = Lag.kr(\sfb.kr(0), lagt).clip(0, 1).max(shold);
		var dwrite = ((sdry[0] + sdry[1]) * 0.5 * (1 - shold))
			+ (fbcol * fbamt * 0.98);
		var smix = Lag.kr(\swet.kr(0), lagt).clip(0, 1);
		BufWr.ar(dwrite.clip2(1.5), dbuf, dph, 1);
		Out.ar(\bph.kr(0), dph);
		Out.ar(\bout.kr(0), sdry * (smix * 0.5pi).cos);
	}));

	// DELAY, read side: eight taps. Only needed while WET is up.
	//
	// Each tap reads at (write head - tap time + drift), where drift runs at
	// (ratio - 1): at PITCH SPREAD 0 a tap sits a constant tap-time behind
	// the head, away from it the head runs fast or slow like one motor of a
	// multi-head tape machine. That is the same motion the old free-running
	// per-tap Phasor made, with one difference: the old Phasor took its
	// starting point from the tap time once, at synth start, and never
	// again - so tap times sent after load (which is all of them) did not
	// move the taps. Here they do, gliding over the 80 ms lag.
	defs.add(SynthDef(\pappus_dtaps, {
		var dbuf = \dbuf.kr(0);
		var taptimes = \taptimes.kr([0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]);
		var taplevels = \taplevels.kr([1, 0, 0, 0, 0, 0, 0, 0]);
		var tappans = \tappans.kr(0 ! 8), tappitch = \tappitch.kr(0 ! 8);
		var dph = In.ar(\bph.kr(0), 1);
		var dframes = BufFrames.kr(dbuf);
		var stl = Lag.kr(\stilt.kr(0), lagt);
		var sxo = Lag.kr(\stiltxover.kr(900), lagt).clip(80, 8000);
		var wetsig = DC.ar([0, 0]), taps;
		8.do { arg i;
			var t = Lag.kr(taptimes[i], 0.08).clip(0.001, 10);
			var ratio = 2 ** (Lag.kr(tappitch[i], lagt) / 12);
			var drift = Phasor.ar(0, BufRateScale.kr(dbuf) * (ratio - 1),
				0, dframes);
			var ph = (dph - (t.max(0.0005) * SampleRate.ir) + drift)
				.wrap(0, dframes);
			wetsig = wetsig + Pan2.ar(BufRd.ar(1, dbuf, ph, 1, 2),
				Lag.kr(tappans[i], lagt), Lag.kr(taplevels[i], lagt));
		};
		taps = LPF.ar(wetsig, sxo);
		wetsig = (taps * (stl.neg * 9).dbamp) + ((wetsig - taps) * (stl * 9).dbamp);
		Out.ar(\bout.kr(0), wetsig * actk.value);
	}));

	// DELAY diffusion, in place on the wet bus. Paused = no diffusion.
	defs.add(SynthDef(\pappus_ddiff, {
		var w = In.ar(\bout.kr(0), 2);
		var amt = Lag.kr(\sdiffuse.kr(0), lagt) * actk.value;
		ReplaceOut.ar(\bout.kr(0), 2.collect({ arg c;
			var x = w[c], sp = 1 + (c * 0.13);
			[0.0133, 0.0191, 0.0277, 0.0413].do({ arg dt;
				x = AllpassC.ar(x, 0.05, dt * sp, dt * sp * 13.5);
			});
			XFade2.ar(w[c], x, (amt * 2) - 1);
		}));
	}));

	// DELAY out: wet into the line's dry share, and its meter.
	defs.add(SynthDef(\pappus_dmix, {
		var smix = Lag.kr(\swet.kr(0), lagt).clip(0, 1);
		var ssig = In.ar(\bout.kr(0), 2)
			+ (In.ar(\bwet.kr(0), 2) * (smix * 0.5pi).sin);
		Out.kr(\mbus.kr(0), Amplitude.kr((ssig[0] + ssig[1]) * 0.5, 0.01, 0.2));
		ReplaceOut.ar(\bout.kr(0), ssig);
	}));

	// COLOUR: DRIVE and CRUSH. Writes the result twice - as-is (mono, for
	// LOSS to transform) and delayed by the transform's latency, so that the
	// dry path is aligned with LOSS whether or not LOSS is running.
	defs.add(SynthDef(\pappus_colour, {
		var dry = In.ar(\bin.kr(0), 2) + gfeed.(\kin1.kr(0), \kin2.kr(0));
		var dr = Lag.kr(\drive.kr(0), lagt), cr = Lag.kr(\crush.kr(0), lagt);
		var crushmode = \crushmode.kr(1);
		var sig = dry, fb = LocalIn.ar(2);
		var dgain, dbias, dx, dy, mk, bits, q, pre, bc, crushfb, jit, sr, rd, br,
			crushed, lmono;
		dgain = 1 + (dr.squared * 40);
		dbias = 1 + (dr * 6.0 * (sig < 0));
		dx = sig * dgain;
		dy = dx / (1 + (dx.abs * dbias));
		dy = LeakDC.ar(dy);
		dy = LPF.ar(dy, (16000 - (dr * 10000)).clip(500, 18000));
		mk = 1.00658 + (dr * -2.84907) + (dr.squared * 3.75126)
			+ ((dr ** 3) * -1.62992);
		dy = dy * mk;
		sig = XFade2.ar(sig, dy, ((dr * 4).clip(0, 1) * 2) - 1);

		bits = 16 - (cr * 13.5);
		q = 2 ** (bits - 1);
		pre = (sig + (fb * cr * 0.5)).tanh;
		bc = (pre * q).round / q;
		crushfb = (pre - bc).clip2(1) * 0.85;
		jit = K2A.ar(LFNoise2.kr([220, 190]).range(0.7, 1.0));
		sr = (SampleRate.ir * ((1 - cr) ** 3).linlin(0, 1, 0.004, 1.0) * jit)
			.clip(150, SampleRate.ir);
		rd = Latch.ar(sig, Impulse.ar(sr));
		br = Latch.ar(bc, Impulse.ar(sr));
		crushed = [
			Select.ar(crushmode - 1, [bc[0], rd[0], br[0]]),
			Select.ar(crushmode - 1, [bc[1], rd[1], br[1]])
		];
		sig = XFade2.ar(sig, crushed, ((cr * 25).clip(0, 1) * 2) - 1);
		LocalOut.ar(crushfb);

		lmono = (sig[0] + sig[1]) * 0.5;
		Out.ar(\bmono.kr(0), lmono);
		Out.ar(\bout.kr(0),
			DelayN.ar(sig, 0.05, (512 - BlockSize.ir + 0.5) / SampleRate.ir));
	}));

	// LOSS, in place. FULL is the real transform; LITE (and the governor's
	// cut) is the two cascaded lowpasses LITE always had.
	[\pappus_loss, \pappus_losslite].do { arg name;
		defs.add(SynthDef(name, {
			var ldry = In.ar(\bout.kr(0), 2);
			var ls = Lag.kr(\loss.kr(0), lagt).clip(0, 1);
			var lmix = (ls * 1.6).clip(0, 1) * actk.value;
			var lossmono;
			if(name == \pappus_losslite) {
				var lcut = 18000 * (0.045 ** (ls ** 2.2));
				var m = (ldry[0] + ldry[1]) * 0.5;
				lossmono = LPF.ar(LPF.ar(m, lcut), lcut);
			} {
				var lmono = In.ar(\bmono.kr(0), 1);
				var lchain = FFT(LocalBuf(512).clear, lmono, 0.5, 0);
				var lthr = Amplitude.kr(lmono, 0.02, 0.15) * 163 * ls.squared * 0.45;
				lchain = PV_MagAbove(lchain, lthr);
				lchain = PV_BrickWall(lchain, 0 - ((ls ** 2.2) * 0.86));
				lossmono = IFFT(lchain);
			};
			ReplaceOut.ar(\bout.kr(0),
				(ldry * (1 - lmix)) + ([lossmono, lossmono] * lmix));
		}));
	};

	// COLOUR's NOISE, following the dry signal's envelope.
	defs.add(SynthDef(\pappus_noise, {
		var dry = In.ar(\bin.kr(0), 2) + gfeed.(\kin1.kr(0), \kin2.kr(0));
		var noisetype = \noisetype.kr(2), noisetone = \noisetone.kr(1200);
		var ns = Lag.kr(\noise.kr(0), lagt);
		var env, nwhite, npink, ndust, nwash, nloop, nlp;
		env = Amplitude.ar((dry[0] + dry[1]) * 0.5, 0.002,
			Lag.kr(\noisedecay.kr(0.25), lagt)).clip(0, 1);
		env = (envref * ((env / envref).max(0)
			** Lag.kr(\noisedyn.kr(2), lagt).clip(0.1, 8))).clip(0, 1);
		nwhite = { WhiteNoise.ar(1) } ! 2;
		npink = { PinkNoise.ar(1.6) } ! 2;
		ndust = { Dust2.ar(1800) } ! 2;
		nwash = [
			Select.ar((noisetype - 1).clip(0, 2), [nwhite[0], npink[0], ndust[0]]),
			Select.ar((noisetype - 1).clip(0, 2), [nwhite[1], npink[1], ndust[1]])
		];
		nwash = BPF.ar(nwash, Lag.kr(noisetone, lagt).clip(60, 12000), 0.8) * 2.5;
		if(nloops > 0) {
			var s = loopplay.(nloops, noisetype - 4, Lag.kr(noisetone, 0.05) / 1200);
			nloop = [s[0], s[1]] * 0.35;
			nlp = Lag.kr(noisetype > 3.5, 0.05);
		} {
			nloop = [Silent.ar, Silent.ar];
			nlp = DC.kr(0);
		};
		Out.ar(\bout.kr(0),
			((nwash * (1 - nlp)) + (nloop * nlp)) * env * ns * 4 * actk.value);
	}));

	// WOW, BYPASS, the direct sends, and the master compressor.
	defs.add(SynthDef(\pappus_out, {
		var sig = In.ar(\bcol.kr(0), 2), in = In.ar(\bin.kr(0), 2);
		var kw = Lag.kr(\kwow.kr(0), lagt).clip(0, 1);
		var bypass = \bypass.kr(0);
		var kwd, kwf, kwm, outsig, kout, omix, mcp;
		kwd = ((kw * 0.0012) + ((kw ** 3) * 0.026));
		kwf = (kw ** 4) * 0.006;
		kwm = (LFNoise2.kr([0.08 + (kw * 0.7), 0.067 + (kw * 0.55)]) * kwd)
			+ (LFNoise2.kr([4.7, 6.1]) * kwf);
		outsig = DelayC.ar(sig, 0.08, Lag.kr(0.0005 + kwd, 0.3) + kwm);
		kout = [
			Select.ar(bypass, [outsig[0], in[0]]),
			Select.ar(bypass, [outsig[1], in[1]])
		];
		Out.kr(\mbus.kr(0), Amplitude.kr((kout[0] + kout[1]) * 0.5, 0.01, 0.2));
		omix = kout + gfeed.(\oin1.kr(0), \oin2.kr(0));
		mcp = Lag.kr(\mcomp.kr(0.2), lagt).clip(0, 1);
		outsig = Compander.ar(omix, omix,
			thresh: (1 - (mcp * 0.92)).max(0.02),
			slopeBelow: 1,
			slopeAbove: (1 - (mcp * 0.9)).max(0.1),
			clampTime: 0.006,
			relaxTime: 0.15
		);
		outsig = outsig / ((1 - (mcp * 0.92)).max(0.02)
			+ ((1 - (1 - (mcp * 0.92)).max(0.02)) * (1 - (mcp * 0.9)).max(0.1)));
		Out.ar(\bout.kr(0), outsig);
	}));

	// REVERB, in place, with SHINE's pitch-shifted regeneration on its own
	// LocalIn/LocalOut - the loop lives entirely inside this node.
	defs.add(SynthDef(\pappus_reverb, {
		var outsig = In.ar(\bout.kr(0), 2);
		var ramt = Lag.kr(\rverb.kr(0), lagt).clip(0, 1) * actk.value;
		var rtl = Lag.kr(\rtime.kr(0), lagt).clip(0, 1);
		var rsize = 1 + (rtl * 2);
		var rdecay = 0.3 + (rtl.pow(1.6) * 11.7) + (rtl.pow(20) * 9988);
		var rdamp = 11000 - (rtl * 8000);
		var rratio = 2 ** (Lag.kr(\rshimmersemi.kr(12), lagt) / 12);
		var shimmerfb = LocalIn.ar(1);
		var rin = ((outsig[0] + outsig[1]) * 0.5) + shimmerfb;
		var rtankc, rwet, rshifted, shimmerout;
		rtankc = [[0.0297, 0.0411, 0.0532], [0.0371, 0.0437, 0.0613]]
			.collect({ arg lens;
				var s = DC.ar(0);
				lens.do({ arg len;
					s = s + CombL.ar(rin, 0.2, (len * rsize).min(0.19), rdecay);
				});
				LPF.ar(s * 0.5, rdamp);
			});
		rwet = rtankc.collect({ arg x, c;
			var sp = 1 + (c * 0.19);
			var stages = if(lite) { [0.0149, 0.0307] }
				{ [0.0149, 0.0223, 0.0307, 0.0389] };
			stages.do({ arg dt;
				x = AllpassC.ar(x, 0.16, (dt * sp * rsize).min(0.155),
					dt * sp * rsize * 11);
			});
			x;
		});
		rshifted = PitchShift.ar((rwet[0] + rwet[1]) * 0.5, 0.15, rratio, 0, 0.008);
		shimmerout = (rshifted * Lag.kr(\rshimmer.kr(0), lagt).clip(0, 1) * 0.9)
			.clip2(1.3);
		LocalOut.ar(shimmerout);
		ReplaceOut.ar(\bout.kr(0),
			(outsig * (ramt * 0.5pi).cos) + (rwet * (ramt * 0.5pi).sin));
	}));

	// Master level, fade, limiter, the last two meters, and out.
	defs.add(SynthDef(\pappus_final, {
		var outsig = In.ar(\bin.kr(0), 2), mt6, mt7;
		mt6 = Amplitude.kr((outsig[0] + outsig[1]) * 0.5, 0.01, 0.2);
		outsig = LeakDC.ar(outsig) * Lag.kr(\amp.kr(1), 0.05)
			* Lag.kr(\fade.kr(1), 0.06);
		outsig = Limiter.ar(outsig, Lag.kr(\limceil.kr(0.98855), 0.1).clip(0.03, 1.0), 0.01);
		mt7 = Amplitude.kr((outsig[0] + outsig[1]) * 0.5, 0.01, 0.2);
		Out.kr(\mbus.kr(0), [mt6, mt7]);
		Out.ar(\outbus.kr(0), outsig);
	}));

	defs.asArray
},

// Execution order: key, def, pausable, then bus wiring as
// [control, busKey] pairs the engine resolves to bus indices. mbus offsets
// are the meter slots (meter1..meter7).
order: [
	[\in,      \pappus_in,       false, [\bout, \in]],
	[\cap1,    \pappus_cap1,     false, [\bin, \in, \bph, \ph1, \bout, \g1, \bufl, \buf, \bufr, \bufr]],
	[\voice1,  \pappus_voice1,   true,  [\bph, \ph1, \bout, \g1, \bufl, \buf, \bufr, \bufr, \pat, \patbuf]],
	[\cap2,    \pappus_cap2,     false, [\bin, \in, \bph, \ph2, \bout, \g2, \bufl, \buf2, \bufr, \buf2r]],
	[\voice2,  \pappus_voice2,   true,  [\bph, \ph2, \bout, \g2, \bufl, \buf2, \bufr, \buf2r, \pat, \patbuf2]],
	[\rnoise,  \pappus_rnoise,   true,  [\bout, \nz]],
	[\rpre,    \pappus_rpre,     false, [\bout, \pana, \bnz, \nz, \mbus, [\mbus, 0]]],
	[\modal_a, \pappus_modal_a,  true,  [\bin, \pana, \bout, \res]],
	[\modal_b, \pappus_modal_b,  true,  [\bin, \pana, \bout, \res]],
	[\string_a, \pappus_string_a, true, [\bin, \pana, \bout, \res]],
	[\string_b, \pappus_string_b, true, [\bin, \pana, \bout, \res]],
	[\rpost,   \pappus_rpost,    false, [\bres, \res, \bout, \pre, \mbus, [\mbus, 2]]],
	[\dwrite,  \pappus_dwrite,   false, [\bin, \pre, \bph, \dph, \bout, \sd, \dbuf, \dbuf]],
	[\dtaps,   \pappus_dtaps,    true,  [\bph, \dph, \bout, \wet, \dbuf, \dbuf]],
	[\ddiff,   \pappus_ddiff,    true,  [\bout, \wet]],
	[\dmix,    \pappus_dmix,     false, [\bout, \sd, \bwet, \wet, \mbus, [\mbus, 3]]],
	[\colour,  \pappus_colour,   false, [\bin, \sd, \bmono, \mono, \bout, \col]],
	[\loss,    \pappus_loss,     true,  [\bmono, \mono, \bout, \col]],
	[\losslite, \pappus_losslite, true, [\bmono, \mono, \bout, \col]],
	[\noise,   \pappus_noise,    true,  [\bin, \sd, \bout, \col]],
	[\out,     \pappus_out,      false, [\bcol, \col, \bin, \in, \bout, \out, \mbus, [\mbus, 4]]],
	[\reverb,  \pappus_reverb,   true,  [\bout, \out]],
	[\final,   \pappus_final,    false, [\bin, \out, \outbus, \outbus, \mbus, [\mbus, 5]]]
],

// Audio buses the graph needs: key -> channels.
buses: (in: 2, g1: 2, g2: 2, ph1: 1, ph2: 1, nz: 1, pana: 1, res: 2, pre: 2,
	dph: 1, sd: 2, wet: 2, mono: 1, col: 2, out: 2),

// When each pausable node is needed, from the current parameter values.
// A node is only ever paused when its contribution is already zero, so
// these are exact, not heuristics.
conds: (
	voice1: { arg p; (p[\msos].max(p[\mlock]) > 0) and: { p[\gates].sum > 0.001 } },
	voice2: { arg p; (p[\nsos].max(p[\nlock]) > 0) and: { p[\gates2].sum > 0.001 } },
	rnoise: { arg p; (p[\pwet] > 0) and: { p[\pgrain] > 0 } },
	modal_a: { arg p; (p[\pwet] > 0) and: { p[\pmodel] < 1.5 } },
	modal_b: { arg p; (p[\pwet] > 0) and: { p[\pmodel] < 1.5 } },
	string_a: { arg p; (p[\pwet] > 0) and: { p[\pmodel] > 1.5 } },
	string_b: { arg p; (p[\pwet] > 0) and: { p[\pmodel] > 1.5 } },
	dtaps: { arg p; p[\swet] > 0 },
	ddiff: { arg p; (p[\swet] > 0) and: { p[\sdiffuse] > 0 } },
	loss: { arg p; p[\loss] > 0 },
	losslite: { arg p; p[\loss] > 0 },
	noise: { arg p; p[\noise] > 0 },
	reverb: { arg p; p[\rverb] > 0 }
),

// How long a node must stay unneeded before it is paused: long enough for
// its own 20 ms parameter lags (and the 50 ms act fade) to have reached
// zero. The voices wait out the longest grain that could still be sounding.
holds: (
	voice1: { arg p; p[\msize].clip(0, 8) + 0.25 },
	voice2: { arg p; p[\nsize].clip(0, 8) + 0.25 }
),

// THE GOVERNOR'S LADDER, least audible first. Each step is applied on top
// of the ones before it. [\cut, keys] takes nodes away (fading them out);
// [\voice, control, value] sets a control on both grain-voice nodes. No
// module the player turned up is ever removed; the last two steps steal
// grain voices (the top rows of each swarm) - only reached when everything
// before them was not enough. Measured cost of each, all-on, FULL, desktop NRT:
// diffusion ~3%, tier B ~6% each, the FFT ~2%, SWARM duplicates up to
// ~40% of a cloud, the overlap cap in proportion to the density.
ladder: [
	[\cut, [\ddiff]],
	[\cut, [\modal_b, \string_b]],
	[\cut, [\loss]],
	[\voice, \dupcut, 0],
	[\voice, \gcap, 6],
	[\voice, \gcap, 3],
	[\voice, \vmax, 6],
	[\voice, \vmax, 4]
],

// Substitutes: when the governor cuts the key, the value runs in its place
// (if the key's own cond says it is needed). A substitute is otherwise off.
subs: (loss: \losslite),

// Parameter defaults, for the engine's copy of the current values. Only
// the ones the conds and holds read are needed.
defaults: (
	msos: 0, mlock: 0, gates: [1, 0, 0, 0, 0, 0, 0, 0], msize: 0.12,
	nsos: 0, nlock: 0, gates2: [1, 0, 0, 0, 0, 0, 0, 0], nsize: 0.12,
	pwet: 0, pgrain: 0, pmodel: 1, swet: 0, sdiffuse: 0,
	loss: 0, noise: 0, rverb: 0
)
)
)
