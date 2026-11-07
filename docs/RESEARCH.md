# Training-plan research

What published running plans and coaching literature say, and how RunningTime uses it. Every number in
`coach/knowledge.py` traces back to a row here. Nothing here is a copy of anyone's plan: only facts about plans
(structure, ranges, rules of thumb) are recorded, in our own words.

**How to read the confidence column**
- **A** – the source itself was read in full (MIT-licensed reference files, an official plan page excerpt).
- **B** – two or more independent secondary sources agree (coaching sites, plan providers).
- **C** – one secondary source, or sources conflict. Used as a soft default and flagged.

**What could not be reached.** Several primary sites (halhigdon.com, nhs.uk, felixrieseberg.github.io) were outside this
environment's network allowlist, so their pages were not read directly; their numbers come via search summaries of
those pages and third-party copies (marked). Daniels' *Running Formula*, Pfitzinger's *Advanced Marathoning* and the
Hansons book were not available either; their rules come from secondary descriptions. Where it matters, that is
noted. **Open-source plan data is scarce**: a GitHub search found no permissively licensed plan datasets beyond
[claude-coach](https://github.com/felixrieseberg/claude-coach) (MIT, a methodology, not templates).
[OpenAthlete](https://github.com/openathleteorg/openathlete) is AGPL-3.0 and was deliberately not used.

---

## 1. Why the first version produced 12–16 km/week for a 5K

Version 1 ignored published volumes. It started at max(stated baseline, 8 km) and compounded +8%/week. The form
defaulted "current weekly distance" to 0, so a 5K plan started at 8 km and peaked at about 12–16 km. Published plans
for the same goal look very different (section 2). The model also never told the athlete *why* volume was what it
was. Fixes: (a) volume targets come from published ranges, (b) the starting point is the athlete's real running
(Strava) or a stated number, else a level-based assumption that is reported back, (c) time-limited and ramp-limited
plans say so explicitly.

## 2. Weekly volume by goal and level

Converted to km (1 mi = 1.609 km). "Peak" = highest week before the taper.

| Goal | Level | Published peak weekly volume | Sources | Conf. |
|---|---|---|---|---|
| 5K | beginner | run miles only: 4.5 → 8 mi (7 → 13 km) for Higdon Novice, 3 runs + walks; other providers 10–18 mi (16–29 km), 10–20 mi (16–32 km) | [Higdon Novice 5K](https://www.halhigdon.com/training-programs/5k-training/novice-5k/) (via search summary + [third-party PDF](https://seashorestriders.com/sites/default/files/5knovice.pdf)); [RunningFront](https://www.runningfront.com/5k-training-plans/); [Vorlich](https://vorlich.com/blogs/news/weekly-mileage-guidelines-for-5k-10k-and-marathon-training) | B |
| 5K | intermediate | 18–26 mi (29–42 km); 20–30 mi (32–48 km) | RunningFront; Vorlich | B |
| 5K | advanced | 30–41 mi (48–66 km); 30–45+ mi; McMillan advanced tiers 50–70 mi (80–110 km) | RunningFront; Vorlich; [McMillan](https://www.mcmillanrunning.com/plans/5k-training-plans/) | B |
| 10K | beginner | "upper teens to low 20s" mi (29–35 km) | [Active.com](https://www.active.com/running/articles/10-steps-to-a-successful-10k) | C |
| 10K | intermediate | mid-20s mi (~40 km); 25–35 mi (40–56 km); 30–40 mi (48–64 km) | Active.com; [McMillan 10K](https://www.mcmillanrunning.com/tp_plans/trainingpeaks-10k/); [On](https://www.on.com/en-us/stories/10K-training-plan) | B |
| 10K | advanced | "into the 30s" mi (48+ km); 30+ mi | Active.com; On | C |
| Half | novice | Higdon Novice peaks 23–25 mi (37–40 km); McMillan novice 10–20 mi; "first-time finisher 25–40 mi" | [Half-marathon plan comparison](https://marathonhandbook.com/wp-content/uploads/Half-Marathon-Training-Plans.pdf); [McMillan](https://www.mcmillanrunning.com/plans/half-marathon-plan-2/) | B |
| Half | intermediate | Higdon Intermediate 33–34 mi (53–55 km); 35–50 mi (56–80 km); McMillan 24–40 mi | same | B |
| Half | advanced | 50–65+ mi (80–105 km); a sub-1:30 plan peaks 45–50 mi | same | C |
| Marathon | beginner | 30–35 mi (48–56 km) for a 4-day, 20-week plan; Hansons *beginner* plan peaks 55–57 mi (88–92 km) – an outlier | [Sunrise Run](https://sunriserunco.com/?p=13766); [Hansons overview](https://runningwithrock.com/?p=1401) | C |
| Marathon | intermediate | Pfitzinger 18/55 (55 mi, 88 km) is called "entry-level advanced" | [Pfitz plans explained](https://runningwithrock.com/pfitz-marathon-training-explained/) | B |
| Marathon | advanced | Pfitzinger 18/70 and 18/85 (113–137 km); [Claude Coach](https://github.com/felixrieseberg/claude-coach) experienced marathoners 60–90 km | same; claude-coach `periodization.md` | A/B |

**Takeaways used in code.** (1) Volume ranges overlap heavily between levels and sources; Higdon sits at the low end,
McMillan/Pfitz at the high end. (2) Volume must be *reachable from where the athlete is* (section 3), so the
plan targets the lower-middle of each range, not the top. (3) Level drives *runs per week*: 5K plans use about
3 (beginner), 4–5 (intermediate), 5–6 (advanced) ([RunningFront](https://www.runningfront.com/5k-training-plans/),
McMillan).

## 3. Progression and recovery weeks

| Finding | Source | Conf. |
|---|---|---|
| Typical weekly increases: 8% beginner, 10% intermediate, 12% advanced | [RunningFront](https://www.runningfront.com/5k-training-plans/) via search | C |
| claude-coach: build ≤10%/week; true beginners 5–10%; returning with strong base 10–15% | claude-coach `periodization.md` | A |
| Recovery week every 3–4 weeks; 3:1 typical, 2:1 for beginners/injury-prone; recovery-week cut 30–40% (another file says 40–50% of a normal week) | claude-coach `periodization.md`, `load-management.md` (they disagree; we use ~25–30% cut) | A (conflict) |
| **The 10% rule is a convention, not a tested threshold.** The one RCT (GRONORUN, 532 novices) compared a ~10%/week 13-week programme with a ~24%/week 8-week one: injury rates ≈20.8% vs 20.3% | [Buist et al. 2008 record](https://search.pedro.org.au/search-results/record-detail/20220); [protocol](https://link.springer.com/content/pdf/10.1186/1471-2474-8-24.pdf) | B |
| Weekly acute:chronic workload ratio did not predict injury in that cohort | same | B |
| Single long-run jumps may matter more: a run >10% longer than the longest run of the prior month was the strongest observational predictor (secondary summary of Frandsen et al.; unverified) | [10% rule reframed](https://trainright.com/10-running-rule-reframed/) | C |
| claude-coach: grow the long run by no more than 2–3 km per week | claude-coach `periodization.md` | A |

**Used in code.** Percentage ramp by level (8/10/12%) with a small absolute floor so low volumes can actually grow
(floors are a design choice; the RCT shows ~24% ramps were not worse for novices over 8 weeks). Recovery week every
4th week at 75%. Long-run growth capped at +10% of the 4-week longest run *or* +2.5 km, whichever is larger,
never above the long-run cap.

## 4. Long runs

| Finding | Source | Conf. |
|---|---|---|
| Daniels (as summarised): long run ≤ 25% of weekly mileage or 150 min, whichever is *less*, for runners above ~40 mi (64 km)/week; ≤30% below that | [Running Magazine](https://runningmagazine.ca/?p=99607); [Daniels study notes](https://test.knowt.com/note/c9a27c0b-2f67-49ce-8736-e000492dbdae/Daniels-Running-Formula--Comprehensive) | B (secondary) |
| Coaches broadly: 25–30% of weekly mileage; 2.5–3 h ceiling | [Strength Running](https://strengthrunning.com/2016/02/how-long-should-my-long-run-be-relative-to-my-weekly-mileage/); Hansons description | B |
| Peak long run by event: 5K 10–12 km; 10K 14–16 km; half 18–22 km; marathon 32–35 km, ≤3.5 h | claude-coach `workouts.md` | A |
| Higdon Novice 5K: longest run builds 1.5 → 3 mi (2.4 → 4.8 km) | Higdon via search summary / third-party PDF | B |
| Hansons: long run capped at 16 mi (26 km) | [Hansons overview](https://runningwithrock.com/?p=1401) | B |

**Used in code.** `long_run ≤ min(share × week, event cap, time cap, the day's available minutes)`. For 5K/10K the share is 40% with
3 runs a week (Higdon Novice ≈ 37%), otherwise 30% (25% at ≥64 km/week). For half/marathon it is 35%/40%, because those plans
routinely exceed 25–30% (claude-coach: 28–35 km long runs at 60–90 km/week) and the 150/300-minute cap and the event caps
still bound them (conf. C for the half/marathon shares). Event caps: claude-coach values, scaled down for beginners.

## 5. Intensity: how much hard running

| Finding | Source | Conf. |
|---|---|---|
| ~80% of training easy / ~20% hard is a *guideline*; Seiler "can live with 85/15 or 75/25" | [Runner's World AU summary](https://runnersworldonline.com.au/?p=49649) | B |
| Polarised beat threshold-heavy training for 10K in recreational runners (n≈30, 10 weeks) | [Muñoz et al. 2014](https://www.pubmed.gov/23752040/) | B |
| For 5K/10K specifically, threshold work may matter more; evidence is extrapolated | secondary | C |
| ~2 quality sessions/week for intermediate 5K/10K; a typical week: intervals Tue, tempo Thu, easy otherwise | [RunnersConnect 5K workouts](https://runnersconnect.net/workouts-for-the-5k/); [Active.com](https://active.com/running/articles/6-speed-workouts-to-run-a-faster-5k) | B |
| Hard/easy: ≥48 h between quality sessions; easy day after the long run; recovery weeks have no hard work | claude-coach `periodization.md`, `load-management.md` | A |
| Per-session hard-volume limits (Daniels, secondary): threshold ≤10% of weekly mileage, interval ≤8% (and ≤10 km), repetition ≤5% | Running Magazine; study notes (sources disagree on weekly vs per-session scope) | C |
| Beginners commonly run easy days too fast ("grey zone"); the talk test is the most reliable easy-pace check; easy ≈ RPE 3–4 | [Runner's Blueprint](https://www.runnersblueprint.com/how-fast-beginner-runners-should-run/); [REI](https://www.rei.com/learn/expert-advice/does-zone-2-running-really-matter.html) | B |

**Used in code.** Quality sessions per week by level and phase (beginner: 0 in base, ≤1 later; intermediate: ≤1 base,
≤2 build/peak when ≥4 run days; advanced ≤2); never on consecutive days or adjacent to the long run; none in
recovery weeks; combined tempo+interval *session* distance ≤ 45% of the week (about half of a session is warm-up and
cool-down, so true hard work stays near 20%, inside Seiler's 85/15–75/25); easy running ≥ 75% of weekly distance.

## 6. Taper

| Finding | Source | Conf. |
|---|---|---|
| 5K/10K: about 5–10 days; ≤75% of normal mileage in the final week | [RunStreet](https://www.runstreet.com/blog/taper-running); [COROS](https://coros.com/au/stories/coros-coaches/c/how-to-master-your-taper) | B |
| Half: about 7–14 days; −30% two weeks out, −50% the week before | same | B |
| Marathon: 2–3 weeks; ~20–30%/week reductions, race week ≈ −60% | same; Mayo Clinic table cited by a secondary source | B |
| Review of 14 studies (cited secondhand): progressive volume cut of 41–60% with intensity and frequency kept works best | secondary summary, unverified | C |
| claude-coach: taper 1–3 weeks, volume −40–60% from peak; marathon 14–21 days | claude-coach `periodization.md`, `race-day.md` | A |
| Keep some race-pace work; speed sessions no faster than goal pace for short races | RunStreet; COROS | B |

**Used in code.** Taper weeks: 1 for 5K/10K, 2 for half, 3 for marathon (race week included). Volume vs peak:
5K/10K race week 60%; half 75% → 55%; marathon 80% → 65% → 45%. Race week target always leaves room for the race itself.

## 7. Plan length

| Goal | Minimum (beginner / experienced) | Source | Conf. |
|---|---|---|---|
| 5K | 8–10 wk / 4 wk (finish only) ; 6–8 wk to sharpen if already running 20+ mi/wk | [trainingplan.dev](https://www.trainingplan.dev/tools/training-plan-length-calculator) | C |
| 10K | 10–12 wk / 8–10 wk | same; [Marathon Handbook](https://marathonhandbook.com/how-long-to-train-for-a-half-marathon/) | C |
| Half | 12–16+ wk beginner; 8–12 wk from a 5K base; 6–12 wk from a 10K base | Marathon Handbook; trainingplan.dev | B |
| Marathon | 16–20 wk | claude-coach `periodization.md` | A |

**Used in code.** If the time left is shorter than the minimum for the athlete's level, the plan is still built but
the athlete is told it is a compressed build.

## 8. Beginners: run/walk

| Finding | Source | Conf. |
|---|---|---|
| NHS Couch to 5K: 9 weeks, 3 runs/week with rest days between; 5-min brisk warm-up and cool-down walks; week 1 = 1 min run / 1.5 min walk repeats; continuous running begins in week 5 (20 min) and reaches 30 min by week 9; may repeat any week | [NHS plan page](https://www.nhs.uk/better-health/get-active/get-running-with-couch-to-5k/couch-to-5k-running-plan/) (search excerpt, weeks 5–9 via [HealthWell](https://healthwell.eani.org.uk/healthtopic/get-fit/couch-5k-week-week?type_1=1)) | B |
| Galloway run/walk ratios by fitness: beginners 30–60 s run / 30–60 s walk; intermediate 3–4 min / 30–60 s; advanced up to 8 min / 1 min | [Livestrong](https://www.livestrong.com/article/551333-running-a-murmur/); [RunDida](https://rundida.com/tools/run-walk-planner/) | C (walk lengths vary by source) |

**Used in code.** For a beginner whose current weekly volume is very low, the coach is told to start with run/walk
intervals (3 runs/week, rest between) and move to continuous running by about week 5–6, advancing only when the
previous week felt manageable.

## 9. Pace calculation

| Finding | Source | Conf. |
|---|---|---|
| Riegel: T₂ = T₁ × (D₂/D₁)^1.06 (1.04 endurance-oriented, 1.08 speed-oriented are used by some tools); ±5% for well-trained runners; weaker predicting a marathon from a 5K | [Wikipedia: Peter Riegel](https://en.wikipedia.org/wiki/Peter_Riegel); [Train as One](https://trainasone.com/ufaq/riegels-formula); [Coachway](https://coachway.io/tools/race-time-predictor/) | B |
| Daniels/Gilbert VDOT: oxygen cost VO₂ = −4.60 + 0.182258 v + 0.000104 v² (v in m/min); sustainable fraction %VO₂max = 0.8 + 0.1894393 e^(−0.012778 t) + 0.2989558 e^(−0.1932605 t) (t in min); VDOT = VO₂ / fraction. Several independent calculators agree | [FitMetricLab](https://fitmetriclab.com/en/tools/running/vdot-calculator/); [RunDida](https://rundida.com/tools/vdot-calculator/); [LetsRun thread](https://www.letsrun.com/forum/flat_read.php?thread=3704747) | B |
| Training intensity as % of VO₂max: easy ~59–74% (65% typical), marathon ~75–84% (80%), threshold ~83–88% (88%), interval ~95–100% (98%), repetition >100% (105%). Sources differ by a few points | claude-coach `zones.md`; [RunningAHead thread](https://www.runningahead.com/forums/post/21df4d69e0974b1e91bc4542f9aa851d); [RunCalcs](https://runcalcs.pages.dev/training/jack-daniels-vdot-calculator/) | B |
| If only a threshold pace is known: easy ≈ T + 50–70 s/km; marathon ≈ T + 15–25; interval ≈ T − 15–20; repetition ≈ T − 25–35 | claude-coach `zones.md` | A |

**Used in code.** `coach/knowledge.py` implements the VDOT equations and derives pace *ranges* (easy as 59–74%).
Paces are computed in code, not by the language model, which is unreliable at arithmetic. A goal time gives goal
paces; real Strava runs give a current easy-pace estimate. The prompt tells the coach to prescribe by effort (RPE /
talk test) as well as pace.

## 10. Workout library (what sessions look like)

Standard session templates, not anyone's copyrighted plan:

| Type | Typical form | Phase | Source |
|---|---|---|---|
| Easy / aerobic | 30–75 min conversational | all | claude-coach `workouts.md` |
| Recovery | 30–45 min very easy | after hard days | same |
| Strides | 4–8 × 15–30 s relaxed fast, full recovery, after an easy run | all | [Running Magazine](https://runningmagazine.ca/sections/training/5-workouts-to-crush-your-fastest-5k/); Active.com |
| Hill repeats | 6–10 × 30–45 s hard uphill, jog down | base→build | Active.com; RunnersConnect |
| Fartlek | unstructured or 6–8 × 1 min on/off | base | claude-coach |
| Tempo / threshold | 15–40 min continuous at ~25–30 s/mi slower than 5K pace, or cruise intervals 3–5 × 1 mile, 60–90 s jog | build | RunnersConnect; claude-coach |
| 5K-specific intervals | 5–6 × 800 m (2 min jog) ; 6–8 × 400 m ; 4–6 × 1000 m (3-min jog) at 5K–3K pace | build→peak | RunnersConnect; Active.com |
| Race simulation | 2 miles continuous at 5K pace (final 2 weeks) | peak | Active.com |
| Progression long run | last 25% faster | build/peak | claude-coach |
| Marathon-specific long run | most easy, final 8–10 km at marathon pace | peak | claude-coach |

## 11. Returning from a layoff or injury

| Weeks/days off | Restart at (share of previous weekly volume) | Source | Conf. |
|---|---|---|---|
| <5 days | full | Under Armour (via search) | C |
| up to 10 days | ~70% | [Runner's World AU](https://runnersworldonline.com.au/how-to-make-a-comeback-to-running/) | C |
| 15–30 days | ~60% (others 50% after 2 weeks, 30% after 4) | same; [Sunrise Run](https://sunriserunco.com/running-after-an-injury/) | C |
| 30 days–3 months | ~50% (others 25–50%) | same; [Running Magazine](https://runningmagazine.ca/?p=32527) | C |
| Injury-related | be pain-free walking 30 min first; stop if pain rises in 10 easy minutes; rebuild ≤10–15%/week; allow ~3 months to return to old volume | [RunnersConnect](https://runnersconnect.net/return-to-running-after-an-injury/); [Active.com](https://www.active.com/articles/play-it-safe-after-a-layoff-due-to-injury) | C |

**Used in code.** The weekly review re-derives the ramp from the *actual* last 4 weeks, so a layoff lowers the baseline
automatically. Injury or pain mentioned in a check-in switches the coach to a conservative mode (no intensity, reduced
volume) and tells the athlete to see a clinician.

---

## Known limits of this research
- Primary sources behind several numbers (Higdon pages, NHS pages, Daniels, Pfitzinger, Hansons) were read only through
  secondary summaries. The ranges are wide on purpose; they are defaults, not prescriptions.
- Published plans disagree with each other, sometimes by 2×. The app targets the lower-middle of each range and
  never exceeds what the athlete's schedule and safe ramp allow.
- This is general fitness guidance, not medical advice.


## Run frequency and per-run size (design choices, added after a 4-run marathon week came out at 78 km)

Weekly volume used to be limited only by the goal/level range and the athlete's stated time. That let four runs carry a huge week:
~19.5 km per run on average, tempo and interval days of 100 minutes, and three of the four runs hard. Now:

- **Per-run caps** (`knowledge.RUN_MAX_MIN`): weekday easy runs ≤ 50/70/85 min and tempo/interval sessions (with warm-up and cool-down)
  ≤ 50/75/90 min for beginner/intermediate/advanced; a marathon block gets +10 min on easy runs. Long runs keep their own caps. These are
  design choices, not figures from one source; they follow the usual pattern that anything much beyond 75-90 minutes is a long run.
- **Frequency ceiling** (`knowledge.frequency_cap_km`): weekly km can't exceed what the run days carry at those caps with the long run at
  its usual share (40% marathon, 35% half). A 4-run intermediate marathon peaks around 60-65 km; a fifth run day allows the full ~78 km.
  The plan page says when run frequency is what limits the build, and the readiness warning suggests adding a run day.
- **Hard sessions**: with 4 run days or fewer a non-advanced athlete gets one tempo/interval session plus the long run, not two.


## Plan shape: build up, peak late (rewritten after "32 km every week")

The first skeleton ramped volume at the maximum safe rate until it hit the goal's ceiling, then held it. On a 24-week marathon that meant a
32 km long run in eleven of the weeks and nine identical 78 km weeks. Three defects, all fixed in `coach/skeleton.py`:

1. **Ramp-then-plateau.** Volume and long run now climb evenly across *all* the build weeks (the gentler of "as fast as is safe" and "spread
   so the peak lands at the end"), peak volume is held for at most two build weeks, and the biggest long run arrives once, in the last build
   week before the taper (one or two near-peak runs before it, step-back weeks in between). A short plan is limited by the safe ramp, not rushed.
2. **Weekly reviews erased step-back weeks.** The recovery rule was "not the first week", and every Monday review makes the current week the
   first week, so an athlete following the app never got a recovery week. It is now keyed to the absolute week of the plan.
3. **Weekly reviews restarted the build.** The review baseline was the mean of the last four weeks (a step-back week dragged it down) and the
   current week was set to that baseline with no progression. The baseline now ignores one low week among steady ones, and a mid-plan rebuild
   steps up from what was achieved along the same ladder.

`coach/planaudit.py` is the written definition of a good plan (shape: gradual build, late peak, at most two peak long runs, a step-back every
fourth week, taper steps down; rules: availability, weekly totals, hard-session spacing, run caps). `tests/test_plan_shape.py` runs it over
5,000+ athlete configurations (the old skeleton fails 2,800 of them, the new one none) and simulates weekly reviews. `tools/plan_eval.py`
builds the real coach prompt for realistic athletes, takes a plan from a model (`live` calls the real API), and audits the result.
