# ChemClaw Benchmark: Simulated Handwritten MOF Laboratory Records and Ground Truth

This benchmark evaluates **ChemClaw's longitudinal timeline tracking and summarization** and **horizontal comparison of synthesis recipes and characterization results**. The records simulate a researcher's handwritten laboratory notebook. They include **information scattered across dates, crossed-out corrections (`~~strikethrough~~`), informal shorthand, implicit references such as "same as the previous batch," mixed units (mg/g, mmol/g versus cm³/g STP, and bar versus kPa), and annotations about experimental problems**.

---

## I. Experimental Background and Sample Identifiers

For evaluator reference; this section may be withheld from the system under test.

- **Research system:** acid-modulator defect engineering of the Zr-based metal-organic framework **UiO-66**, $\mathrm{Zr}_6\mathrm{O}_4(\mathrm{OH})_4(\mathrm{BDC})_6$, for flue-gas $\mathrm{CO}_2/\mathrm{N}_2$ capture.
- **Key differences among the three recipes:**
  - **Sample 1 (`Zr-BDC-01`; batch `EXP-1012-A`):** glacial acetic acid (AA, 30 eq), 120 °C for 24 h; the conventional microporous control.
  - **Sample 2 (`Zr-BDC-02`; batch `EXP-1012-B`):** trifluoroacetic acid (TFA, 30 eq), 120 °C for 24 h; a moderately defective sample with exposed $\mathrm{Zr}^{4+}$ sites and defect-derived pore cages.
  - **Sample 3 (`Zr-BDC-03`; batch `EXP-1013-C`):** TFA at 60 eq, increased added water, and 160 °C for 36 h; excessive modulation causes local framework etching and collapse during high-temperature activation.

---

## II. Simulated Laboratory Records

The following dated records are the raw textual inputs.

### [Record Page P01] Date: 2026-10-12 (Monday) | Sunny | Room temperature 23 °C | RH 45%

**Topic: Exploring Zr-MOF defect engineering - first synthesis batches (`Zr-BDC-01` and `Zr-BDC-02`)**

- **1. Batch `EXP-1012-A` (sample label: `Zr-BDC-01`)**
  - **Charge:** weighed $\mathrm{ZrCl}_4$ ~~200 mg~~ **233.2 mg** (approximately 1.0 mmol; reweighed because the balance had not been zeroed) and terephthalic acid ($\mathrm{H}_2\mathrm{BDC}$) **166.1 mg** (1.0 mmol) into a 50 mL PTFE liner.
  - **Solvent and modulator:** added **30 mL DMF** with a graduated cylinder; sonicated for 10 min until mostly dissolved. Pipetted in **1.72 mL glacial acetic acid (AA)**, calculated as 30 eq, then added **100 µL DI water** to aid dissolution and cluster nucleation.
  - **Reaction:** stirred magnetically for 15 min, sealed the autoclave, and placed it in **Oven #1 at 120 °C**. Started timing at **14:00**; planned reaction time **24 h**.

- **2. Batch `EXP-1012-B` (sample label: `Zr-BDC-02`)**
  - **Charge:** metal salt, linker, DMF and added water **exactly the same as the previous batch (`Zr-BDC-01`)**. Actual weights: $\mathrm{ZrCl}_4$ 233.5 mg and $\mathrm{H}_2\mathrm{BDC}$ 166.0 mg.
  - **Change:** replaced glacial acetic acid with the stronger acid **trifluoroacetic acid (TFA)**, also at **30 eq**. Pipetted in ~~2.0 mL~~ **2.30 mL TFA** (recalculated the volume after checking the density; nearly underdosed it). Slight warming after adding TFA; the solution remained clear.
  - **Reaction:** placed together with `Zr-BDC-01` in **Oven #1 at 120 °C** at 14:00. Set an alarm to remove it tomorrow at 14:00.

---

### [Record Page P02] Date: 2026-10-13 (Tuesday) | Overcast | Room temperature 22 °C

**Topic: Recovery and washing of 01/02 + a third synthesis under extreme conditions (`Zr-BDC-03`)**

- **1. Recovery of `Zr-BDC-01` and `Zr-BDC-02` (removed at 14:15 and cooled to room temperature)**
  - `Zr-BDC-01`: a thick layer of white microcrystalline precipitate at the bottom; clear, colorless supernatant. Centrifugation at 8000 rpm was straightforward. Washed three times with fresh DMF (20 mL per wash, soaking for 2 h each time).
  - `Zr-BDC-02`: slightly less white precipitate than 01; the supernatant had a faint yellow tint. Finer particles; needed 9500 rpm to settle completely. Also washed three times with DMF.
  - **Solvent exchange:** at 20:00 tonight, transferred both batches into **25 mL anhydrous methanol (MeOH)** for exchange. Planned a two-day soak, replacing the methanol morning and evening, for three replacements in total.

- **2. New batch `EXP-1013-C` (sample label: `Zr-BDC-03`; trying concentrated TFA and higher temperature to make larger mesopores)**
  - **Charge:** $\mathrm{ZrCl}_4$ **0.233 g** and $\mathrm{H}_2\mathrm{BDC}$ **0.166 g** in **30 mL DMF**.
  - **Changes:**
    1. **Doubled TFA to 60 eq (4.60 mL).**
    2. Increased DI water to **0.5 mL (500 µL)**.
    3. Raised the temperature to **160 °C in Oven #2**, with the reaction extended to **36 h**. Started at 21:00 tonight; planned removal Thursday at 09:00.

---

### [Record Page P03] Date: 2026-10-15 (Thursday) | Sunny

**Topic: Vacuum activation of 01/02 + recovery and rapid exchange of 03**

- **1. Activation of `Zr-BDC-01` and `Zr-BDC-02`**
  - `Zr-BDC-01`: after 48 h in MeOH (three replacements), filtered and briefly dried at 60 °C in a forced-air oven, then transferred to a vacuum oven for **activation at 150 °C for 12 h**. Obtained **248 mg pure white powder**. Theoretical yield based on ideal, defect-free $\mathrm{Zr}_6\mathrm{O}_4(\mathrm{OH})_4(\mathrm{BDC})_6$ is approximately 278 mg; estimated yield ~89.2%.
  - `Zr-BDC-02`: first **activated under vacuum at 120 °C for 4 h**. A quick FT-IR check showed a small band at **1660 cm⁻¹** (residual DMF C=O) and a clear **C-F band near 1180 cm⁻¹**. This indicated that 120 °C had not fully removed the trifluoroacetate coordinated at vacant Zr-cluster sites or the residual solvent. **Returned it to the oven, raised the temperature to 150 °C, and continued under vacuum for 8 h (12 h total).** Repeat FT-IR: the 1660 cm⁻¹ band disappeared completely and the C-F band became much weaker. Final product: **215 mg off-white powder**.

- **2. Recovery and workup of `Zr-BDC-03` (160 °C / 36 h batch)**
  - Removed at 09:00. Pungent acid odor on opening the liner; distinctly pale brown-yellow mother liquor. The precipitate looked gray-white and rather sticky.
  - After three DMF washes, skipped methanol to save time and used **acetone for rapid Soxhlet/soak exchange, three times (24 h total)**.
  - *Note / plan: tomorrow morning, go straight to 180 °C activation to try to remove all the high-loading TFA occupying the sites.*

---

### [Record Page P04] Date: 2026-10-16 (Friday) | Partly cloudy

**Topic: High-temperature activation of 03 + PXRD, SEM and FT-IR comparison of all three samples**

- **1. Activation result for `Zr-BDC-03`**
  - After acetone exchange, directly **activated under vacuum at 180 °C for 12 h**. The color changed from gray-white to **pale beige / darker**. Only **162 mg** recovered; clearly low yield.

- **2. PXRD (powder X-ray diffraction; Cu Kα, 2θ = 3-50°)**
  - **`Zr-BDC-01`:** extremely sharp, strong peaks at **7.36° (111)** and **8.50° (200)**, symmetric profiles and **FWHM 0.14°**. Fully matches standard `fcu` UiO-66; no impurity peaks; very high crystallinity.
  - **`Zr-BDC-02`:** strong main peaks at **7.36° and 8.50°**, **FWHM 0.18°**; framework retained. **Important: a pronounced, broad superlattice diffuse-scattering hump appears at low angle, 2θ = 4.1-5.8°.** The notebook assigns this to `reo` defect nanodomains (ordered missing-cluster/missing-linker regions), interpreting it as successful pore formation without destruction of the main framework.
  - **`Zr-BDC-03`:** the main peak around **7.3°** lost nearly half its intensity; relative crystallinity estimated at **~55% of 01**. **FWHM 0.42°**, with a clearly elevated baseline suggesting partial amorphization. A weak hump at **30.2°** is tentatively assigned to a tetragonal $\mathrm{ZrO}_2$ impurity formed by local hydrolysis under hot, acidic conditions.

- **3. SEM (scanning electron microscopy)**
  - **`Zr-BDC-01`:** highly regular octahedral single-crystal particles with sharp edges; **280-320 nm**, mean approximately **300 nm**.
  - **`Zr-BDC-02`:** truncated octahedra with slightly rounded edges, smaller than 01; **200-240 nm**, mean approximately **220 nm**. Mild surface etching and roughness.
  - **`Zr-BDC-03`:** severely degraded octahedral morphology; mostly irregular spherical / fragmented aggregates, **450-600 nm**, with obvious cracks and perforated corrosion pits.

- **4. Additional FT-IR comparison**
  - No DMF band at **1660 cm⁻¹** in any of the three samples.
  - Bridging **µ₃-OH** stretching intensity at **3674 cm⁻¹**: **`Zr-BDC-01` > `Zr-BDC-02` >> `Zr-BDC-03`**. The notebook attributes the near-loss of hydroxyls in 03 to dehydroxylation into Zr-O-Zr during 180 °C activation.

---

### [Record Page P05] Date: 2026-10-17 (Saturday) | Sunny

**Topic: 77 K N₂ adsorption/desorption, BET surface area and pore-size distribution + TGA defect quantification**

- **1. N₂ physisorption at 77 K** (ASAP 2460; all samples degassed again in situ at **150 °C for 4 h** before measurement)
  - **`Zr-BDC-01`:** typical **Type I isotherm**; steep uptake at **P/P₀ < 0.05**, then a plateau, with no hysteresis.
    - **BET surface area = 1180 m²/g**; Langmuir surface area **1310 m²/g**.
    - **Total pore volume = 0.48 cm³/g**; micropore volume **0.44 cm³/g**.
    - NLDFT distribution: a single micropore peak at **0.85 nm**, with a small contribution from **0.60 nm** tetrahedral-cage windows.
  - **`Zr-BDC-02`:** a **combined Type I + Type IV isotherm**; markedly higher low-pressure uptake than 01, with a small **H4 hysteresis loop at P/P₀ = 0.45-0.85**.
    - **BET surface area = 1620 m²/g**, approximately **37% higher** than 01.
    - **Total pore volume = 0.74 cm³/g**; micropore volume **0.51 cm³/g**; mesopore volume **0.23 cm³/g**.
    - NLDFT: a **bimodal hierarchical pore structure**, with intrinsic **0.85 nm** micropores and a strong **1.85 nm** defect-cage peak, described in the source notebook as mesoporous.
  - **`Zr-BDC-03`:** low-pressure uptake dropped sharply; slow uptake at high pressure with clear hysteresis.
    - **BET surface area = 740 m²/g**, below the conventional sample.
    - **Total pore volume = 0.39 cm³/g**; micropore volume shrank to **0.21 cm³/g**.
    - NLDFT: severely diminished **0.85 nm** micropore peak and a broad, disordered **2.6-3.2 nm** pore peak. The notebook attributes this to excessive etching by 60 eq TFA at 160 °C, followed by local microporous framework collapse during 180 °C activation.

- **2. TGA** (air, **10 °C/min**, room temperature to **800 °C**)
  - Treating the final residue as pure **ZrO₂**, normalized to **100%**, used the **450-550 °C** linker-combustion mass-loss step to estimate **missing linkers per Zr₆ unit**. An ideal unit contains six BDC linkers.
  - **`Zr-BDC-01`:** main decomposition temperature **T_d = 515 °C**; measured linker/cluster ratio **5.70**; **0.30 missing linkers per Zr₆ cluster**, close to an ideal lattice.
  - **`Zr-BDC-02`:** **T_d = 490 °C**; slightly reduced but still good thermal stability. Linker/cluster ratio **4.62**; **1.38 missing linkers per cluster**, associated in the notebook with many unsaturated Zr⁴⁺ Lewis acid sites.
  - **`Zr-BDC-03`:** early, gradual mass loss from **410 °C**, with **T_d = 435 °C**. Apparent missing-linker count **2.35 per cluster**; the notebook interprets this as connectivity below the stability threshold and severely impaired thermal stability.

---

### [Record Page P06] Date: 2026-10-19 (Monday) | Sunny

**Topic: CO₂/N₂ adsorption, isosteric heat (Q_st), IAST selectivity, and cycling / humid breakthrough tests**

- **1. Single-component static adsorption, 0-1 bar**
  - **Unit warning:** Zhang recorded the **298 K CO₂ uptake for `Zr-BDC-02` in cm³/g STP**, while the other uptake values use mmol/g.
  - **`Zr-BDC-01`:**
    - CO₂ at **273 K, 1 bar: 3.10 mmol/g**.
    - CO₂ at **298 K, 1 bar: 1.85 mmol/g**; at **0.15 bar** (flue-gas partial pressure): **0.52 mmol/g**.
    - N₂ at **298 K, 1 bar: 0.14 mmol/g**.
  - **`Zr-BDC-02`:**
    - CO₂ at **273 K, 1 bar: 5.18 mmol/g**.
    - CO₂ at **298 K, 1 bar: 76.61 cm³/g (STP)** on the original printout. *Annotation: divide by the stated standard molar volume, **22.4 cm³/mmol**, to obtain **3.42 mmol/g**.*
    - CO₂ at **15 kPa (0.15 bar): 1.28 mmol/g**, **2.46 times** the uptake of 01.
    - N₂ at **298 K, 1 bar: 0.16 mmol/g**.
  - **`Zr-BDC-03`:**
    - CO₂ at **273 K, 1 bar: 2.45 mmol/g**.
    - CO₂ at **298 K, 1 bar: 1.52 mmol/g**; at **0.15 bar: 0.41 mmol/g**.
    - N₂ at **298 K, 1 bar: 0.13 mmol/g**.

- **2. Zero-coverage isosteric heat, Q_st⁰, and IAST selectivity**
  - Q_st⁰ calculated by a Virial fit to the **273 K and 298 K** isotherms. IAST evaluated at **298 K, 1 bar, CO₂:N₂ = 15:85**, representing coal-fired power-plant flue gas.
  - **`Zr-BDC-01`: Q_st⁰ = 26.5 kJ/mol; IAST CO₂/N₂ selectivity = 28.4.**
  - **`Zr-BDC-02`: Q_st⁰ = 34.8 kJ/mol; IAST selectivity = 54.2.** The notebook attributes the enhanced low-pressure affinity to electrostatic interaction of the CO₂ quadrupole with open Zr⁴⁺ sites, and faster mass transfer to the hierarchical pores.
  - **`Zr-BDC-03`: Q_st⁰ = 29.1 kJ/mol; IAST selectivity = 21.0.** Residual defect sites give a slightly higher initial heat than 01, but severe micropore collapse reduces total capacity and selectivity.

- **3. Fixed-bed breakthrough and five adsorption/desorption cycles**
  - **298 K; CO₂/N₂ = 15/85; total flow 20 mL/min; packed mass 0.50 g.**
  - **Dry mixture:**
    - N₂ breaks through all three columns within **1.2-1.5 min/g**.
    - CO₂ breakthrough times: **`Zr-BDC-02` 41.5 min/g > `Zr-BDC-01` 23.0 min/g > `Zr-BDC-03` 16.8 min/g**.
    - Regenerated with **He purge at 80 °C**. After **five dry cycles**, capacity retention: **01 = 99.1%; 02 = 98.4%; 03 = 94.0%**.
  - **Humid flue-gas challenge (RH = 50%; BET and CO₂ uptake checked after five cycles):**
    - **`Zr-BDC-01`:** highly water-resistant; **97.8%** CO₂ working-capacity retention; unchanged PXRD.
    - **`Zr-BDC-02`:** robust; **94.6%** retention. Some water competes for exposed Zr sites, but **regeneration at 150 °C** fully restores capacity; no framework collapse.
    - **`Zr-BDC-03`: severe deactivation.** CO₂ uptake falls by **38.5%**, leaving **61.5%** of the initial value. BET falls to **430 m²/g**. The notebook attributes this to irreversible Zr-O-C hydrolysis under water vapor in the heavily linker-deficient framework (**2.35 missing linkers per cluster**).

---

## III. Evaluation Questions and Ground Truth

Evaluator-only material. Do not include this section in the record inputs supplied to the system under test.

### A. Longitudinal Summarization and Tracking

#### L1. Track the complete lifecycle of a single sample

**Recommended prompt:**

> Reconstruct the complete experimental timeline of sample `Zr-BDC-02`, from reagent charging on October 12 to performance testing on October 19. Identify any experimental problems encountered and explain how they were resolved.

**What this tests:** aggregation across six dated pages; inheritance of implicit recipe parameters from "same as the previous batch" on P01; rejection of the crossed-out **2.0 mL** in favor of **2.30 mL**; extraction of the activation problem and corrective treatment on P03.

**Expected answer:**

1. **October 12 - synthesis:** inherit the base recipe from 01, while retaining the actual weights for 02: ZrCl₄ **233.5 mg**, H₂BDC **166.0 mg**, DMF **30 mL**, and water **100 µL**. Replace AA with **TFA 2.30 mL (30 eq)**, correcting the erroneous 2.0 mL. React at **120 °C for 24 h**.
2. **October 13 - recovery and exchange:** fine particles require **9500 rpm** centrifugation. Wash with DMF three times, then exchange in anhydrous MeOH for **48 h with three replacements**.
3. **October 15 - activation problem and correction:** **120 °C under vacuum for 4 h** leaves residual DMF at **1660 cm⁻¹** and a C-F band at **1180 cm⁻¹**. Return to the oven for **150 °C under vacuum for another 8 h**, giving **12 h total**. DMF band disappears and C-F weakens. Final product **215 mg**.
4. **October 16-19 - characterization and performance:** `reo` defect-domain assignment; **BET 1620 m²/g**; **1.38 missing linkers per cluster**; **CO₂ uptake 3.42 mmol/g at 298 K and 1 bar**; **94.6% retention after humid cycling**.

#### L2. Explain failure and degradation through an evidence chain

**Recommended prompt:**

> `Zr-BDC-03` was intended to have larger mesopores. Why did it end up with the poorest CO₂ capture and humidity resistance? Build a complete causal explanation using the synthesis, workup and characterization evidence.

**What this tests:** linking synthesis, activation, PXRD, TGA, BET and FT-IR evidence to the reported humid-cycle degradation.

**Expected answer:**

1. **Excessive etching during synthesis:** **60 eq TFA + 0.5 mL water + 160 °C for 36 h** gives excessive competitive coordination. TGA indicates **2.35 missing linkers per cluster**. PXRD crystallinity falls to **~55%**, with a **30.2°** hump tentatively attributed to ZrO₂; SEM shows fragmented aggregates.
2. **Workup and high-temperature activation damage:** rapid acetone exchange is followed directly by **180 °C activation**. FT-IR shows a severe loss of the **3674 cm⁻¹ µ₃-OH** band. Local micropore collapse is supported by **BET 740 m²/g** and **micropore volume 0.21 cm³/g**.
3. **Poor separation and humidity resistance:** loss of micropores reduces **298 K / 1 bar CO₂ uptake to 1.52 mmol/g** and **IAST selectivity to 21.0**. The highly defective coordination environment is associated with irreversible hydrolysis at **50% RH** and a **38.5%** uptake loss.

### B. Horizontal Comparison and Unit Normalization

#### H1. Build a normalized comparison table

**Recommended prompt:**

> Summarize the synthesis recipes, structural parameters (BET area, pore volumes, pore sizes and defect counts), and adsorption performance at 298 K for `Zr-BDC-01`, `Zr-BDC-02` and `Zr-BDC-03` in one comparison table. Normalize all masses to mg, CO₂ uptake at 298 K to mmol/g, and partial pressures to bar.

**What this tests:**

- Convert **0.233 g to 233 mg** and **0.166 g to 166 mg** for sample 03.
- Convert **76.61 cm³/g STP ÷ 22.4 cm³/mmol to 3.42 mmol/g** for sample 02.
- Convert **15 kPa to 0.15 bar**.
- Ignore crossed-out values when extracting the final recipe.

**Expected answer:** use Table 1 below.

#### H2. Explain the structure-performance relationship

**Recommended prompt:**

> Compare the three samples. How do acid-modulator strength and loading, together with synthesis temperature, affect UiO-66 pore structure, defect concentration and CO₂/N₂ separation? Do the results follow a volcano-type relationship?

**What this tests:** the reported benefit of moderate defects followed by degradation at excessive defect levels, and the tradeoff between adsorption affinity (Q_st) and framework stability.

**Expected answer:** a pronounced **volcano-type structure-performance relationship** in this three-sample comparison.

1. **01 to 02 (AA 30 eq to TFA 30 eq):** a stronger modulator, with lower pK_a, increases missing linkers from **0.30 to 1.38 per cluster**. The source assigns the added **1.85 nm** pore cages to `reo` defects. BET increases **1180 to 1620 m²/g**; exposed Zr⁴⁺ sites are associated with a Q_st⁰ increase **26.5 to 34.8 kJ/mol**. CO₂ uptake at **0.15 bar** rises **0.52 to 1.28 mmol/g**, and IAST selectivity rises **28.4 to 54.2**.
2. **02 to 03 (TFA 60 eq + synthesis at 160 °C + activation at 180 °C):** the notebook interprets **2.35 missing linkers per cluster** as exceeding the framework's tolerance. Micropores collapse and broad **2.6-3.2 nm** disordered pores appear. Thermal stability falls (**T_d = 435 °C**) and humid-cycle performance deteriorates severely.

---

## IV. Table 1: Standardized Cross-Sample Reference

For automated evaluation and manual checking.

| Category | Metric (normalized units) | `Zr-BDC-01` - conventional control | `Zr-BDC-02` - optimum moderate defects | `Zr-BDC-03` - excessive modulation / collapse |
| :--- | :--- | :--- | :--- | :--- |
| Synthesis | ZrCl₄ / H₂BDC charge (mg) | 233.2 / 166.1 | 233.5 / 166.0 | 233.0 / 166.0 (converted from g) |
| Synthesis | Modulator and equivalents | Glacial AA, 30 eq, 1.72 mL | TFA, 30 eq, 2.30 mL | TFA, 60 eq, 4.60 mL |
| Synthesis | Added DI water (µL) | 100 | 100 | 500 (from 0.5 mL) |
| Synthesis | Crystallization temperature / duration | 120 °C / 24 h | 120 °C / 24 h | 160 °C / 36 h |
| Workup and yield | Solvent exchange / final activation | MeOH 48 h; 150 °C for 12 h | MeOH 48 h; 120 °C for 4 h + 150 °C for 8 h | Acetone 24 h; 180 °C for 12 h |
| Workup and yield | Activated mass / appearance | 248 mg; pure white | 215 mg; off-white | 162 mg; pale beige / darker |
| Phase and morphology | PXRD features / crystallinity | Sharp fcu phase; FWHM 0.14° | fcu main phase + reo defect hump at 4.1-5.8° | ~55% relative crystallinity; possible ZrO₂ hump at 30.2° |
| Phase and morphology | SEM morphology / particle size | Regular octahedra; mean ~300 nm | Truncated octahedra; mean ~220 nm | Etched fragmented aggregates; 450-600 nm |
| Pores and defects | BET area (m²/g) | 1180 | **1620** | 740 |
| Pores and defects | Total / micropore volume (cm³/g) | 0.48 / 0.44 | **0.74 / 0.51**; additional mesopore volume 0.23 | 0.39 / 0.21 |
| Pores and defects | Dominant NLDFT pore sizes (nm) | 0.85; single micropore peak | **0.85 + 1.85**; hierarchical bimodal pores | 0.85 (weak) + 2.6-3.2 (broad, disordered) |
| Pores and defects | TGA T_d (°C) / missing linkers per cluster | 515 / 0.30 | 490 / **1.38** | 435 / 2.35 |
| Adsorption and separation | CO₂ uptake, 273 K, 1 bar (mmol/g) | 3.10 | **5.18** | 2.45 |
| Adsorption and separation | CO₂ uptake, 298 K, 1 bar (mmol/g) | 1.85 | **3.42** (from 76.61 cm³/g STP) | 1.52 |
| Adsorption and separation | CO₂ uptake, 298 K, 0.15 bar (mmol/g) | 0.52 | **1.28** (15 kPa = 0.15 bar) | 0.41 |
| Adsorption and separation | N₂ uptake, 298 K, 1 bar (mmol/g) | 0.14 | **0.16** | 0.13 |
| Adsorption and separation | Q_st⁰ (kJ/mol) | 26.5 | **34.8** | 29.1 |
| Adsorption and separation | IAST CO₂/N₂ selectivity, 15:85 | 28.4 | **54.2** | 21.0 |
| Dynamics and stability | Dry CO₂ breakthrough time (min/g) | 23.0 | **41.5** | 16.8 |
| Dynamics and stability | Capacity retention, five cycles: dry / 50% RH | 99.1% / **97.8%** | 98.4% / **94.6%** | 94.0% / **61.5%** (hydrolytic collapse) |

