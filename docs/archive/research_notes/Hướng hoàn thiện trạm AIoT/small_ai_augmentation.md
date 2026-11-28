# Small, robust AI component fine-tuned on few real sessions plus synthetic data (Pi 4 air-quality station)

Scope: choosing ONE main AI task for a Raspberry Pi 4 station (PMS7003, DHT11, BH1750, relay+fan, USB camera; MQ135 being removed) with only ~1–3 days of collection, then generating synthetic data from those sessions and evaluating honestly.

## Which task is most feasible with ~1–3 days of data and most demoable?

### Takeaway
Recommended main task: a **session-level "room model + event recognizer"**, which combines (b) and (a). Fit a physics mass-balance model (decay rate k with fan off and on) to each recorded episode. That model then gives a 10–30 min nowcast and time-to-clean, and it also serves as the simulator that generates synthetic training data for a small tree-based classifier of event type (normal / incense-smoke / cooking / dust-resuspension). Sensor-fault detection, is a cheap rule-based add-on, not the headline. Treat the event classifier as the "AI" part and the physics model as the honest, explainable core. If time runs short, the physics nowcast plus fan-effect estimate alone is still a defensible deliverable.

### Cited findings
- Indoor events measured with low-cost sensors in a real home show pseudo-first-order decay. ASHRAE 1756-RP reported median and mean decay rates of about 1.3 and 2.0 h⁻¹ across 836 emission events, so one cooking or incense episode decays over roughly 0.5–2 h. That makes 10–30 min nowcasts and time-to-clean observable within a single afternoon. — [ASHRAE 1756-RP, Response of eight low-cost particle sensors… (ResearchGate record)](https://www.researchgate.net/publication/336249268_Response_of_eight_low-cost_particle_sensors_and_consumer_devices_to_typical_indoor_emission_events_in_a_real_home_ASHRAE_1756-RP) (figures from search snippet; full text not accessible, 403)
- Emission rates are normally estimated with a mass-balance model that includes the air exchange rate and a first-order indoor loss rate. The LBL compilation assumed k_d = 0.27 h⁻¹ and a room height of 2.5 m. Cooking emission rates across 14 experiments ranged from 6.8×10² to 5.8×10⁴ µg/h. — [Singer, LBL compilation of PM2.5 emission rates for cooking, candles and incense](https://indoor.lbl.gov/sites/default/files/brett_singer_-_compilation_of_published_pm_2.5_emission_rates_for_cooking_candles_and_incense_for_the_use_in_modeling_of_exposeres_in_residences.pdf)
- Incense measured in a California residence emitted 7–202 mg/h of PM2.5, with emission factors of 5–56 mg/g burned. — [ACS EST, incense burning in a California residence](https://pubs.acs.org/doi/10.1021/acs.est.3c08904). A London study found a median incense emission of 454 µg/min, which it describes as comparable to kitchen activities. This conflicts with LBL's statement that incense is on average lower than cooking. — [Environment International 2026, fragrance products in London homes](https://www.sciencedirect.com/science/article/pii/S0160412026004290)
- There is a Hanoi-specific study on incense burning and indoor PM2.5 in residential houses (title confirmed; full text blocked). It is useful as local motivation for an "incense" class. — [ResearchGate: impact of incense burning on indoor PM2.5 in Hanoi houses](https://researchgate.net/publication/353791189_The_impact_of_incense_burning_on_indoor_PM25_concentrations_in_residential_houses_in_Hanoi_Vietnam)
- Even with dense multi-week monitoring of 3 homes using OPC-N3 sensors and NMF, "source identification could not be confidently specified beyond indoors or outdoors." Passive field data alone does not label sources well; deliberately staged and labeled sessions are needed. — [Rathbone et al. 2025, Scientific Reports (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11729851/)

### Inferences
- (a) Event classification alone is risky. With only 1–3 days, the team gets perhaps 5–10 episodes per class, and on a PMS7003 cooking and incense may look alike (both fine-mode). It becomes demoable and defensible when paired with (b): "the station recognises *what* happened, predicts *when* the room will be clean, and shows *how much faster* the fan cleans it."
- (b) The physics nowcast is the most feasible part. Each staged episode (emit → stop → decay with fan OFF; repeat with fan ON) directly yields k_off and k_on, and the difference k_on − k_off is the fan's effective clean-air delivery per room volume. This is a measurable, honest claim ("fan increases removal rate by X h⁻¹, time-to-clean drops from A to B min"), and it ties the relay, the sensor and the AI together. That answers the lecturer's "disconnected sensors" critique.
- (c) Fault detection, such as a frozen value, PM out of range, PM2.5 > PM10, a sudden zero, or RH > 85 %, is best done as deterministic rules, not as the headline AI.

### Gaps
- No published study was found that does exactly this on a PMS7003 with fan on/off and few sessions. The combined design is an inference, not a validated benchmark.

## Literature on classifying indoor PM sources with low-cost sensors

### Takeaway
Published evidence that low-cost optical sensors can *classify* indoor source type is thin and mostly fairly recent. Reported high accuracies (95–98 %) come from setups with extra signals (VOC, camera) or may not use event-grouped splits. Low-cost sensors miss sub-0.3 µm emissions, so some sources are invisible. Features that physically discriminate sources are the size ratios (PM1/PM2.5, PM2.5/PM10), rise rate, peak, duration and decay constant.

### Cited findings
- A 2026 study (Machine Learning with Applications / ScienceDirect) used PurpleAir particle and VOC data with a neural network to recognize smoking, cooking and cleaning. It reported 98 % accuracy versus 66 % for a logistic model, with cooking the best per-class result (99.1 %). The full text was blocked (403), so the dataset size and the split method (random vs. by event) could not be verified. Treat the headline number with caution. — [Identifying indoor aerosol sources using PurpleAir data (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S2666827026000678)
- A 2024 study combined computer vision (video of cooking) with low-cost PM sensors and reached 95 % accuracy classifying cooking methods by emission level. A 6-minute high-emission cooking event raised indoor PM by 21–25 %. This is relevant because the station already has a USB camera. — [PubMed 39163940](https://pubmed.ncbi.nlm.nih.gov/39163940/); [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0048969724057772)
- In a 120 m³ lab with 7 consumer monitors (incl. PurpleAir, which uses Plantower), outputs were time-correlated and within a factor of 2 of reference mass for most sources. All monitors "substantially under-reported or missed" events dominated by particles < 0.3 µm. — [Singer & Delp 2018, Indoor Air (LBL page)](https://indoor.lbl.gov/publications/response-consumer-and-research-grade)
- In a 6-monitor study over 21 residential sources, the low-cost monitors had low or no response to sources whose mass was entirely < 0.25 µm, such as steady candle flames and cooking without frying or grilling. — [Building and Environment 2020, Performance of low-cost indoor monitors for PM2.5 and PM10 from residential sources](https://www.sciencedirect.com/science/article/abs/pii/S0360132320300123)
- In home networks, indoor source contribution increases with particle size (PM1 → PM10). Average I/O ratios were 0.30 for PM1, 1.02 for PM2.5 and 3.66 for PM10, and larger-particle factors decayed faster than fine ones. This supports PM2.5/PM10 and decay-rate features for separating resuspension (dust, cleaning) from combustion (incense, smoke). — [Rathbone et al. 2025 (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC11729851/)
- An optical-sensor patent notes that indoor size distributions depend largely on event type, and that the sensor-to-reference calibration factor shifts with size. — [USPTO 10365197](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10365197)
- A 2018 US patent application describes classifying pollution events (cooking, smoking, pesticide spraying) from sensor features with SVMs. This is prior art for a feature-based event classifier. — referenced in search results via [uspto.report 20180073759](https://uspto.report/patent/app/20180073759)
- A 2025 Air (MDPI) study built an ML model detecting smoking households from low-cost sensor data and suggested, without testing, that it could extend to cooking and incense. — [Modelling the presence of smokers in households, Air 3(4):27](https://doi.org/10.3390/air3040027)

### Inferences
- Realistic expectation for a PMS7003-only classifier with a handful of staged sessions: **"combustion-like fine aerosol (incense/paper smoke/frying)" vs "coarse/resuspension (dust, sweeping)" vs "normal"** should be separable via PM2.5/PM10 and PM1/PM2.5. Separating incense from cooking may fail, and the team should be ready to merge them into a single "combustion/fine" class and report it.
- Candidate features per event or 1–5-min window: PM1, PM2.5, PM10 levels above a baseline; the ratios PM1/PM2.5 and PM2.5/PM10; rise slope (µg/m³/min) and time-to-peak; peak height; duration above baseline+X; fitted decay constant k after the peak; temperature/RH deltas (cooking raises T and RH, though DHT11 is coarse); lux change (lights on); fan state. Camera-based features, such as "person present" (YOLO was removed from repo), are optional.
- Dataset sizes in the cited works were not retrievable. No comparable public dataset was found for transfer.

### Gaps
- Exact dataset sizes, split protocols and per-class confusion for the 2026 PurpleAir neural-network paper (paywall/403).
- No published PMS7003-specific source-classification benchmark was found.

## Time-series augmentation and synthetic data for small datasets

### Takeaway
For tiny data, use **(1) a physics box-model simulator with parameters fitted to the real sessions** as the main generator, plus **(2) mild transformation augmentations** (jitter, magnitude scaling within physically sensible bounds, window slicing/cropping, window warping). Avoid permutation, flipping and aggressive time-warping, which destroy the rise/decay shape. GANs and diffusion models (TimeGAN, RCGAN) need far more data than a few sessions and add little that the physics model does not. Validate synthetic data with TSTR (train on synthetic, test on real).

### Cited findings
- Um et al. 2017 (Parkinson's wearable CNN): augmentation lifted accuracy from 77.54 % to 86.88 %. Rotation, permutation and time-warping helped and their combinations gave the best results. The authors caution that scaling can change labels when classes differ by intensity. — [arXiv 1706.00527](https://arxiv.org/pdf/1706.00527)
- Iwana & Uchida 2021 (12 methods, 128 UCR datasets, 6 network types): **window warping** had the best average rank and is the most recommended general method, and **window slicing** was very effective and fast. **Permutation** was severely detrimental and **rotation/flipping** hurt all models. Plain **time warping** performed poorly through over-transformation, and jittering had minor effects. Gains were larger for smaller datasets. CNNs benefited most. Transformation methods run in a fraction of a second, whereas DGW/wDBA take thousands of seconds. — [Iwana & Uchida, PLOS ONE 16(7): e0254841](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0254841)
- The same survey's taxonomy has four families: transformation, pattern mixing, generative models and decomposition. — [PLOS ONE / DOAJ record](https://doaj.org/article/f69abd8bbc574c12a7b980e739932adf)
- The tsaug library provides TimeWarp, Crop, Quantize, Drift, Reverse, AddNoise and Dropout. Augmenters compose with `+` (sequential), `*` (n parallel copies) and `@p` (apply with probability). — [tsaug documentation](https://tsaug.readthedocs.io/en/stable/)
- TSTR/TRTS were introduced with RCGAN: train a model on synthetic labelled data, evaluate on real data, and the reverse. RCGAN data gave "only minor degradation" on real tests, but on large data (serialized MNIST; eICU with 17,000 patients). — [Esteban, Hyland, Rätsch 2017, arXiv 1706.02633](https://arxiv.org/pdf/1706.02633)
- Physics generator basis: the well-mixed mass balance V·dC/dt = P·a·V·C_out + S − (a + k_dep + CADR/V)·V·C. CADR counts only particles captured by the cleaner, not deposition or infiltration losses. — [Air cleaner report, mass-balance and CADR/V](https://www.scribd.com/document/590988443/air-cleaner-report); [LBL compilation](https://indoor.lbl.gov/sites/default/files/brett_singer_-_compilation_of_published_pm_2.5_emission_rates_for_cooking_candles_and_incense_for_the_use_in_modeling_of_exposeres_in_residences.pdf)

### Inferences
- Concrete synthetic-generation recipe:
  1. From each real episode, fit the background C_bg, the emission duration τ, the source strength S/V (from the rise) and the decay rate k (log-linear fit of C − C_bg after the source stops), separately for fan OFF and ON. Also fit the per-class ratio distributions PM1/PM2.5 and PM2.5/PM10.
  2. Sample new episodes by drawing parameters from the fitted ranges, slightly widened (for example ±30 % on S, k in [k_min·0.8, k_max·1.2], τ, start time, background level, fan switch time), then integrate the ODE at a 10 s step.
  3. Add a realistic sensor layer: the PMS7003 noise level estimated from flat background periods (residual SD), integer quantization of µg/m³, occasional dropouts, DHT11 1 °C / 1 %RH quantization, and a humidity bias above about 75 % RH.
  4. Apply mild tsaug-style transforms on top: AddNoise, Crop/slicing, small magnitude scaling within the fitted range, and window warping. Do not use Reverse, permutation or flips, because decay must stay after rise.
- Be honest that the simulator only produces shapes the team already measured, with parameter interpolation. It cannot create a source type never recorded, and a classifier trained on it inherits the box-model assumptions (well-mixed room, single exponential decay). Report a **real-only vs real+synthetic vs synthetic-only (TSTR)** table.
- Generative deep models are overkill: TimeGAN/RCGAN results were shown on thousands to tens of thousands of sequences, and with 5–10 episodes per class they would memorise or collapse. This is an inference; no source tests GANs at n≈10.

### Gaps
- No source quantifies how much physics-simulated indoor PM data improves real-world classification ("sim-to-real" for indoor aerosols). The team's own real-vs-synthetic ablation will be the evidence.

## Proper evaluation with few sessions

### Takeaway
Split by **session/episode**, never by window. Use Leave-One-Session-Out (or StratifiedGroupKFold), and generate synthetic and augmented data **only from the training sessions inside each fold**. Report per-class confusion matrices, macro-F1 and balanced accuracy against a hand-written threshold heuristic, and run the augmentation ablation. For the nowcast, use a time-ordered holdout and compare against persistence and a "physics with default k" baseline.

### Cited findings
- scikit-learn: when samples come in dependent groups (subjects, experiments, devices), "ensure that all the samples in the validation fold come from groups that are not represented at all in the paired training fold". GroupKFold, StratifiedGroupKFold and LeaveOneGroupOut implement this, and LeaveOneGroupOut is recommended "in the cases of multiple experiments". — [scikit-learn cross-validation guide](https://scikit-learn.org/stable/modules/cross_validation.html)
- For time series, KFold/ShuffleSplit "would result in unreasonable correlation between training and testing instances", and shuffling time-ordered data leads to "an inflated validation score". TimeSeriesSplit always trains on the past and tests on the future. — [scikit-learn cross-validation guide](https://scikit-learn.org/stable/modules/cross_validation.html)
- The TSTR/TRTS protocol for judging synthetic data utility. — [Esteban et al. 2017](https://arxiv.org/pdf/1706.02633)

### Inferences
- Leakage traps specific to this project:
  - Overlapping 1-min windows from the same episode in both train and test. This is the biggest risk and inflates accuracy massively.
  - Fitting simulator parameters on *all* episodes (including test) and then testing on a real held-out episode, which leaks test statistics into training data. The fix is to refit the simulator inside each fold.
  - Fitting scalers or baselines on all data.
  - Labeling windows by staged-session metadata where the "normal" class is always recorded at a different time of day, so the model learns time or lux instead of the event. Record "normal" periods interleaved with events.
- Minimal honest report table: for each method (heuristic rules / RF real-only / RF real+aug / RF synthetic-only (TSTR) / RF real+synthetic), give macro-F1 ± SD across LOSO folds plus the summed confusion matrix. With about 20–30 episodes, state that confidence intervals are wide. Also report results on **one fully unseen day** recorded after model freeze as the final test.
- For the nowcast and time-to-clean: hold out the last episodes in time and report MAE at +10/+20/+30 min against persistence, and the error in predicted time-to-clean (minutes). Optional: conformal intervals from residuals of the training episodes (split-conformal with few calibration points gives wide but valid intervals).

### Gaps
- No source gives a minimum number of episodes per class for reliable LOSO estimates. Rule of thumb (inference): at least 5 episodes per class, at least 2 recording days, and at least 1 untouched test day.

## Transfer learning and fine-tuning options

### Takeaway
The transfer path for the short-horizon model is **"pretrain on physics-simulated episodes → fine-tune or calibrate on real episodes"**. Another option is to **fit 2–4 physical parameters per room** (k_off, k_on, background), which is "fine-tuning" in the most data-efficient sense. Chronos-Bolt Tiny (9M parameters) could serve as a zero-shot nowcast baseline.

### Cited findings
- Chronos-Bolt comes in Tiny (9M), Mini (21M), Small (48M) and Base (205M) sizes. It runs on CPU and is described as much faster than the original Chronos (Base "over 600 times faster" than Chronos Large, hardware unspecified). No published Pi 4 latency was found. — [AWS blog: Chronos-Bolt and AutoGluon](https://aws.amazon.com/jp/blogs/news/machine-learning-fast-and-accurate-zero-shot-forecasting-with-chronos-bolt-and-autogluon); [Hugging Face chronos-bolt-tiny](https://huggingface.co/autogluon/chronos-bolt-tiny)

### Inferences
- Short-horizon model: a parametric decay model with fitted k is already the strongest prior. If an ML model is wanted, train a small GBM/RF regressor on simulated episodes (inputs: last 10–30 min of PM, fan state, slope; target: PM at +10/+20/+30 min), then compare it zero-shot on real episodes (TSTR) against the same model retrained with real episodes added. This doubles as the sim-to-real experiment.
- Chronos-Bolt on Pi requires PyTorch, which is heavy on a Pi 4. Use it, if at all, as an offline laptop baseline. Inference only; not benchmarked here.

### Gaps
- No measured Chronos-Bolt latency on Raspberry Pi.

## Tiny model choices on Pi 4

### Takeaway
With under 100 real windows, **RandomForest/ExtraTrees (or LightGBM with strong regularisation) on 10–20 hand-crafted event features** is more robust than a 1D-CNN/GRU and runs in well under a millisecond to a few ms on a Pi. A tiny 1D-CNN is feasible latency-wise (~20 ms measured for a CNN-1D on a Raspberry Pi), but it needs the synthetic data to train at all and is harder to explain.

### Cited findings
- On a Raspberry Pi, a CNN-1D (ESC-10 audio) took about 20.4 ± 0.6 ms per inference unquantized and about 21.4 ms with 8-bit PTQ. A small feed-forward ANN took about 0.1–0.6 ms. — [Sensors 23:6227 tables (PMC10347208)](https://pmc.ncbi.nlm.nih.gov/articles/PMC10347208/table/sensors-23-06227-t008)
- Calibration studies found random forest can outperform linear regression for low-cost PM monitors. — [ResearchGate: performance of low-cost indoor monitors](https://www.researchgate.net/publication/338704547_Performance_of_low-cost_indoor_air_quality_monitors_for_PM25_and_PM10_from_residential_sources)
- Iwana & Uchida found MLP and some recurrent models often hurt by augmentation, while CNNs benefit. A deep model's behaviour on tiny data depends on the augmentation choice. — [PLOS ONE 2021](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0254841)

### Inferences
- Choose RF/ExtraTrees (100–300 trees, max_depth around 6–10, class_weight balanced), serialised with joblib. On a Pi 4 a single prediction from a forest of that size should be in the low-millisecond range and the file a few hundred KB to a few MB. This is not measured here, so benchmark on the Pi with `timeit`.
- Features make the model explainable in the demo ("classified as smoke because PM1/PM2.5 = 0.9 and the rise was 40 µg/m³/min"), which matters given the lecturer's "not attractive / not applied" feedback. Feature importances are a slide.
- A 1D-CNN is only worth it as a comparison row (trained on synthetic + real, evaluated LOSO).

### Gaps
- No published Pi 4 latency for scikit-learn RF or LightGBM predict calls was found.

## Known pitfalls: humidity, DHT11, warm-up, leakage

### Takeaway
PMS-family sensors over-read above about 75 % RH, and Hanoi humidity often exceeds that. Use the EPA/Barkjohn equation only as a reference form, since it was fitted on PurpleAir 24 h data, and flag high-RH windows. DHT11's coarse RH (±5 %RH) limits any fine humidity correction. Discard the first ~30 s after sensor or fan power-up (an inference, not sourced). Overlapping windows are the main leakage risk.

### Cited findings
- PMS1003 (same Plantower family) number and mass readings rose significantly above about 75 % RH. During fog, total particle count rose 28 % and counts > 2.5 µm rose > 50 %. PM10 was 46 % above a dried reference, and in the chamber PM2.5 rose from about 9 to 16 µg/m³ between 78 % and 89 % RH. The cause is hygroscopic growth and droplets counted as particles, with no inlet dryer. — [Jayaratne et al. 2018, AMT 11:4883](https://amt.copernicus.org/articles/11/4883/2018/); [author response](https://amt.copernicus.org/preprints/amt-2018-100/amt-2018-100-AR1.pdf)
- The US-wide PurpleAir correction is PM2.5 = 0.524 × PA_cf1 − 0.0862 × RH + 5.75 (RH in %, cf_1 channel average). It reduced RMSE from about 8 to 3 µg/m³ on about 12,000 paired 24-h averages across 16 states. Raw PurpleAir overestimated by about 40 %. More complex multiplicative models did not improve results on independent data. — [Barkjohn, Gantt, Clements 2021, AMT 14:4617](https://amt.copernicus.org/articles/14/4617/2021/)

### Inferences
- PMS7003 outputs both "CF=1" and "atmospheric" fields. Barkjohn's coefficients are for PurpleAir cf_1 averaged over two sensors at 24 h resolution, so applying them to a single PMS7003 at 10 s is only an approximation. Present the formula as "EPA-style correction, not locally validated". Proper local calibration needs collocation with a reference station or an outdoor public station for days.
- For event classification, high RH is a confounder: a humid shower or cooking steam can raise the PM reading without smoke. Include RH and ΔRH as features and/or label "steam" as its own pseudo-class if observed.
- DHT11: ±2 °C / ±5 %RH (per project context) with 1-unit resolution, so RH-based corrections carry ±5 %RH uncertainty. At RH near 75–85 %, that straddles the onset of hygroscopic growth.

### Gaps
- No PMS7003-specific warm-up time was verified from a primary source here. Datasheet values were not fetched.

## Physics: decay rates, fan/filter effect, fitting k

### Takeaway
Use a single-zone exponential model. Total loss rate k = a (air exchange) + k_dep (deposition) + CADR/V (cleaner). Typical measured total decay after indoor events is about 1–2 h⁻¹, and deposition-only assumptions are around 0.27 h⁻¹. Air exchange in homes ranges from about 0.6 h⁻¹ (closed) to about 3 h⁻¹ (normal ventilation, Brisbane). A mini 5 V fan with no real filter will add little to k unless it pushes air through a filter, so measure it rather than assume.

### Cited findings
- 836 indoor emission events in a real home gave median and mean pseudo-first-order decay of about 1.3 and 2.0 h⁻¹. — [ASHRAE 1756-RP record](https://www.researchgate.net/publication/336249268_Response_of_eight_low-cost_particle_sensors_and_consumer_devices_to_typical_indoor_emission_events_in_a_real_home_ASHRAE_1756-RP) (via search snippet)
- The LBL compilation assumed a PM2.5 decay (deposition) k_d = 0.27 h⁻¹ and room height 2.5 m for emission-rate calculations. — [LBL compilation](https://indoor.lbl.gov/sites/default/files/brett_singer_-_compilation_of_published_pm_2.5_emission_rates_for_cooking_candles_and_incense_for_the_use_in_modeling_of_exposeres_in_residences.pdf)
- Brisbane homes: air exchange about 0.6 h⁻¹ under minimum ventilation and about 3.0 h⁻¹ under normal ventilation. Deposition rates varied by house and were lowest for 0.2–0.3 µm particles. — [He, Morawska & Gilbert 2005, QUT eprints](https://eprints.qut.edu.au/3663)
- CADR divided by room volume gives equivalent air changes per hour from the cleaner. Effectiveness is the cleaner removal rate relative to baseline removal (ventilation + deposition). — [Air cleaner report](https://www.scribd.com/document/590988443/air-cleaner-report)
- AHAM room-size labels assume 4.8 equivalent ACH from the cleaner. — [AHAM white paper on PACs and ACH](https://ahamverifide.org/wp-content/uploads/2021/11/White-Paper-Portable-Air-Cleaners-and-AIr-Changes-per-Hour-FINAL-00106301.pdf)
- Measured in-situ PAC effectiveness: median about 36–94 % in homes, 0–67 % in classrooms, 11–33 % in offices, depending on room size, CADR, sources and background loss. — [U Toronto in-situ PAC study](https://hab.civmin.utoronto.ca/wp-content/uploads/2025/06/insitu_PAC.pdf)
- A Hanoi study measured house airtightness as ACH50 = 4.8 h⁻¹ (2007 house) and 2.1 h⁻¹ (2018 house). This is a 50 Pa pressurization value, not natural air exchange. CADR equals airflow × filter efficiency (× circulation). — [PMC9610078, purifier efficacy in Hanoi-area houses](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9610078/)
- The decay-derived loss rate represents the combined air exchange plus all removal processes, not air changes alone. — [Environment International 2026, London](https://www.sciencedirect.com/science/article/pii/S0160412026004290)

### Inferences
- Fitting procedure for each episode: subtract the background (median of 10 min before the event). After the source stops and the peak passes, fit ln(C − C_bg) vs t by linear regression over the segment where C − C_bg > ~3× the noise SD, or fit nonlinearly to C_bg + A·e^(−kt). Then k_fan = k_on − k_off, effective CADR = k_fan × V, and time-to-clean from C0 to target C* is t = ln((C0 − C_bg)/(C* − C_bg))/k.
- Realistic simulator ranges: k_off ≈ 0.3–2 h⁻¹ (depends on windows and doors; measure). For k_on, a PC/USB fan without a filter mostly mixes air and adds little removal. Strapping the fan to a cheap HEPA/MERV filter (a "Corsi–Rosenthal mini") is what makes k_on − k_off clearly measurable. This is an inference: the fan's CADR is unknown and must be measured. Emission: incense 7–202 mg/h, cooking 0.7–58 mg/h (LBL); scale with V to get µg/m³/min rises.
- Use a small closed volume (e.g., a 50–200 L box or a closet) for staged demos so that decay happens in minutes, not hours. That makes 10+ episodes per day feasible. Note this in the report as a proof-of-concept chamber, consistent with the project's "hộp khói" test.

### Gaps
- No measured CADR for a 5 V mini fan with or without a filter was found.
- Deposition rate (k_dep) specifically for Hanoi homes was not found.
- Recommended data plan (inference, not sourced): 2–3 recording days. Per day: 3–4 episodes each of incense, paper-smoke or frying (choose 2–3 safe classes), 2–3 dust/sweeping episodes, half fan-ON and half fan-OFF, with 15–30 min "normal" blocks interleaved. Target ≥ 5–8 real episodes per class in total and keep one full day untouched as the final test. Generate ~50–200 simulated episodes per class *per training fold*.
