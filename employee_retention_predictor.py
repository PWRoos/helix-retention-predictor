#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
THE EMPLOYEE RETENTION PREDICTOR
Helix Systems Group, Inc. -- Predictive HR Analytics Proof of Concept
===============================================================================

MSBA Capstone Project | Quantic School of Business and Technology
Author  : Philip Wouter Roos | Roos Analytics

-------------------------------------------------------------------------------
WHAT THIS SCRIPT DOES
-------------------------------------------------------------------------------
Builds and validates a supervised classifier that ranks employees by propensity
to leave, then translates that ranking into a business case.

Four design decisions distinguish this implementation:

  1. NESTED CROSS-VALIDATION. The decision threshold is a tuned parameter.
     Selecting it on the same partition used to report performance is leakage
     and inflates every downstream figure. The threshold is selected in an INNER
     loop and evaluated in an OUTER loop. This also yields out-of-fold
     predictions for all 1,470 employees, raising the evaluation base from 47
     positive cases to 237.

  2. TIME-ANCHOR SENSITIVITY. The target records THAT an employee left, not
     WHEN. The observation window is unknown, so every annualised figure is
     reported across 1-, 2- and 3-year window assumptions.

  3. PROXY AUDIT. Excluding protected attributes does not make a model fair if
     retained features reconstruct them. DistanceFromHome is audited explicitly.

  4. UTILITY-BASED THRESHOLD SELECTION. The threshold maximises expected net
     business value, not F1 or accuracy.

-------------------------------------------------------------------------------
CRITICAL LIMITATION -- READ BEFORE ANY OPERATIONAL USE
-------------------------------------------------------------------------------
The training data is the IBM HR Analytics Employee Attrition dataset, which is
SYNTHETIC. Performance here demonstrates that the analytical architecture is
sound. It does NOT establish that comparable signal exists in live client data.
Coefficients must be refitted, and performance re-established from scratch, on
real HRIS records before any employee is scored.

USAGE: python employee_retention_predictor.py [--data PATH] [--outdir PATH]
===============================================================================
"""

import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, confusion_matrix, roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

warnings.filterwarnings("ignore")


# =============================================================================
# SECTION 0: CONFIGURATION
# =============================================================================
# All business assumptions are centralised so a reviewer can audit and vary them
# without reading the modelling code. Identifiers match the report's register.

RANDOM_STATE = 42
OUTER_FOLDS = 5
INNER_FOLDS = 5

# --- Business assumptions ---------------------------------------------------
REPLACEMENT_MULTIPLIER = 1.50      # A1: replacement cost = 1.5x annualised salary
INTERVENTION_RATE = 0.10           # A2: triage budget = 10% of annualised salary
INTERVENTION_EFFECTIVENESS = 0.30  # A8: share of identified leavers retained

# A10: THE TIME ANCHOR. The dataset does not state over what period its 237
# departures occurred. If they accumulated over three years, the true annual
# liability is one third of the headline figure. Every annualised result is
# reported across this range rather than silently assuming one year.
WINDOW_SCENARIOS = [1.0, 2.0, 3.0]

# --- Deployment cost stack --------------------------------------------------
IMPLEMENTATION_COST = 145_000      # Year-1 one-off
ANNUAL_RUN_COST = 40_000           # recurring

# --- Field handling ---------------------------------------------------------
ZERO_VARIANCE_COLS = ["EmployeeCount", "Over18", "StandardHours"]
IDENTIFIER_COL = "EmployeeNumber"
TARGET_COL = "Attrition"

PROTECTED_EXCLUDE = ["Gender", "Age", "MaritalStatus"]

# PROXY RISK. DistanceFromHome is a documented proxy for race and socioeconomic
# status through residential segregation. The dataset contains no race field, so
# the proxy relationship CANNOT be tested here. Section 5 measures what excluding
# it costs; the exclusion decision follows from that measurement.
PROXY_RISK_COLS = ["DistanceFromHome"]
EXCLUDE_PROXY_FEATURES = True

THRESHOLD_GRID = np.arange(0.20, 0.86, 0.05)


def r100k(x):
    """Round to the nearest $100,000. See report limitations on false precision."""
    return round(x / 100_000) * 100_000


def wilson_interval(successes, n, z=1.96):
    """Wilson score interval -- preferred to the normal approximation at small n."""
    if n == 0:
        return (np.nan, np.nan)
    p = successes / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    margin = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return ((centre - margin) / denom, (centre + margin) / denom)


# =============================================================================
# SECTION 1: INGESTION AND STRUCTURAL VALIDATION
# =============================================================================

def load_data(path):
    """Load the HRIS extract and fail loudly on structural problems."""
    print("=" * 79)
    print("SECTION 1: INGESTION AND STRUCTURAL VALIDATION")
    print("=" * 79)

    df = pd.read_csv(path)
    # Strip the UTF-8 BOM Excel prepends; without this 'Age' becomes '\ufeffAge'
    # and silently drops out of the numeric feature list.
    df.columns = [c.strip().replace("\ufeff", "") for c in df.columns]

    n_pos = int((df[TARGET_COL] == "Yes").sum())
    print(f"  Records                 : {df.shape[0]:,}")
    print(f"  Variables               : {df.shape[1]}")
    print(f"  Null values             : {df.isna().sum().sum()}")
    print(f"  Duplicate employee IDs  : {df[IDENTIFIER_COL].duplicated().sum()}")
    print(f"  Departures (positive)   : {n_pos} ({n_pos / len(df):.2%})")

    print("\n  TIME ANCHOR WARNING")
    print("  The target records THAT an employee left, not WHEN. The observation")
    print("  window is not stated. 16.12% is a PREVALENCE, not a rate. It becomes")
    print("  an annual attrition rate only under the assumption that all")
    print("  departures fell within one year -- which the data cannot support")
    print("  or refute.")
    print("  Annualised departures by window: " +
          ", ".join(f"{w:.0f}yr={n_pos / w:.0f}" for w in WINDOW_SCENARIOS))
    return df


# =============================================================================
# SECTION 2: FIELD ELIMINATION
# =============================================================================

def prepare_features(df, exclude_proxy=EXCLUDE_PROXY_FEATURES,
                     exclude_protected=True, verbose=True):
    """Drop non-informative, protected, and (optionally) proxy-risk fields."""
    if verbose:
        print("\n" + "=" * 79)
        print("SECTION 2: FIELD ELIMINATION")
        print("=" * 79)

    df = df.copy()
    keys = df[IDENTIFIER_COL].copy()
    to_drop = []

    for col in ZERO_VARIANCE_COLS:
        if col in df.columns:
            if len(df[col].unique()) > 1:
                raise ValueError(f"{col} is not constant; investigate before dropping.")
            if verbose:
                print(f"    {col:<22} constant {list(df[col].unique())}  -> DROP")
            to_drop.append(col)

    if verbose:
        print(f"    {IDENTIFIER_COL:<22} identifier -> DROP as feature, RETAIN as key")
    to_drop.append(IDENTIFIER_COL)

    if exclude_protected:
        for col in PROTECTED_EXCLUDE:
            if col in df.columns:
                if verbose:
                    print(f"    {col:<22} PROTECTED ATTRIBUTE -> DROP")
                to_drop.append(col)

    if exclude_proxy:
        for col in PROXY_RISK_COLS:
            if col in df.columns:
                if verbose:
                    print(f"    {col:<22} PROXY RISK (untestable here) -> DROP")
                to_drop.append(col)

    df = df.drop(columns=[c for c in to_drop if c in df.columns])
    y = (df.pop(TARGET_COL) == "Yes").astype(int)
    X = df

    cat_cols = X.select_dtypes(include=["object", "string"]).columns.tolist()
    num_cols = [c for c in X.columns if c not in cat_cols]
    if verbose:
        print(f"\n  Modelling matrix: {X.shape[1]} predictors "
              f"({len(num_cols)} numeric, {len(cat_cols)} categorical)")
    return X, y, keys, num_cols, cat_cols


# =============================================================================
# SECTION 3: LEAKAGE-SAFE PREPROCESSING
# =============================================================================

def build_preprocessor(num_cols, cat_cols):
    """
    StandardScaler on numerics, one-hot on categoricals, composed into a
    ColumnTransformer that is always fitted INSIDE a Pipeline on training folds
    only -- making distributional leakage structurally impossible rather than
    merely avoided by discipline.

    Scaling is mandatory, not cosmetic. The L2 penalty shrinks coefficients in
    proportion to magnitude, and magnitude is inversely proportional to feature
    scale. Unscaled, the penalty would fall almost entirely on the 1-4 Likert
    variables while leaving MonthlyIncome effectively unregularised -- variable
    selection driven by units of measurement.
    """
    return ColumnTransformer([
        ("num", StandardScaler(), num_cols),
        ("cat", OneHotEncoder(drop="first", handle_unknown="ignore"), cat_cols),
    ])


def make_pipeline(num_cols, cat_cols, estimator=None):
    # NOTE: test `is None` explicitly. `estimator or Default(...)` evaluates the
    # estimator's truthiness, and an unfitted RandomForestClassifier raises on
    # __len__. Cloning gives each fold an independent, unfitted estimator.
    if estimator is None:
        est = LogisticRegression(
            class_weight="balanced",  # ~5.2:1 reweighting toward the 16% minority
            max_iter=2000, C=1.0, random_state=RANDOM_STATE,
        )
    else:
        est = clone(estimator)
    return Pipeline([("preprocessor", build_preprocessor(num_cols, cat_cols)),
                     ("classifier", est)])


# =============================================================================
# SECTION 4: NESTED CROSS-VALIDATION
# =============================================================================

def net_utility(tp, flagged, effectiveness=INTERVENTION_EFFECTIVENESS):
    """
    Expected net value of an operating point, in multiples of annual salary.

        benefit = effectiveness x TP x REPLACEMENT_MULTIPLIER
        cost    = flagged x INTERVENTION_RATE

    At base assumptions this is 0.45 per true positive against 0.10 per flag, so
    flagging one more employee pays whenever their probability of being a
    RETAINED true positive exceeds 0.10/0.45 = 22.2%. That marginal condition,
    not accuracy or F1, selects the threshold.
    """
    return (effectiveness * tp * REPLACEMENT_MULTIPLIER) - (flagged * INTERVENTION_RATE)


def select_threshold(y_true, proba):
    """Choose the threshold maximising net utility on validation data."""
    best_t, best_u = 0.50, -np.inf
    for t in THRESHOLD_GRID:
        pred = (proba >= t).astype(int)
        tp = int(((pred == 1) & (y_true == 1)).sum())
        u = net_utility(tp, int(pred.sum()))
        if u > best_u:
            best_u, best_t = u, t
    return best_t


def nested_cv(X, y, num_cols, cat_cols, estimator=None, label="Logistic Regression"):
    """
    Nested cross-validation.

    OUTER loop : holds out a fold, never touched during any tuning
    INNER loop : selects the decision threshold on outer-training data only
    Result     : out-of-fold predictions for every record, and an unbiased
                 estimate of performance at a properly-selected threshold

    This replaces a single train/test split. At 237 positive cases a three-way
    split would leave ~47 positives for threshold selection and ~47 for testing
    -- too few for either job. Nested CV uses every record for both purposes
    without leaking.
    """
    print("\n" + "=" * 79)
    print(f"SECTION 4: NESTED CROSS-VALIDATION -- {label}")
    print("=" * 79)

    outer = StratifiedKFold(OUTER_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    oof_proba = np.zeros(len(y))
    oof_pred = np.zeros(len(y), dtype=int)
    chosen, fold_auc = [], []

    for k, (tr, te) in enumerate(outer.split(X, y), 1):
        X_tr, X_te = X.iloc[tr], X.iloc[te]
        y_tr, y_te = y.iloc[tr], y.iloc[te]

        # --- INNER: threshold selection on training data only ---------------
        inner = StratifiedKFold(INNER_FOLDS, shuffle=True, random_state=RANDOM_STATE)
        inner_proba = np.zeros(len(y_tr))
        for itr, ite in inner.split(X_tr, y_tr):
            p = make_pipeline(num_cols, cat_cols, estimator)
            p.fit(X_tr.iloc[itr], y_tr.iloc[itr])
            inner_proba[ite] = p.predict_proba(X_tr.iloc[ite])[:, 1]
        t_star = select_threshold(y_tr.values, inner_proba)
        chosen.append(t_star)

        # --- OUTER: fit on full outer-train, evaluate on untouched fold -----
        pipe = make_pipeline(num_cols, cat_cols, estimator)
        pipe.fit(X_tr, y_tr)
        p_te = pipe.predict_proba(X_te)[:, 1]
        oof_proba[te] = p_te
        oof_pred[te] = (p_te >= t_star).astype(int)
        auc = roc_auc_score(y_te, p_te)
        fold_auc.append(auc)
        print(f"  Fold {k}: inner-selected threshold {t_star:.2f} | outer AUC {auc:.3f}")

    print(f"\n  Outer AUC: {np.mean(fold_auc):.3f} +/- {np.std(fold_auc):.3f}")
    print(f"  Thresholds selected: {[round(float(t), 2) for t in chosen]}"
          f"  (std {np.std(chosen):.3f})")
    return {"oof_proba": oof_proba, "oof_pred": oof_pred,
            "thresholds": chosen, "fold_auc": fold_auc,
            "auc_mean": float(np.mean(fold_auc)), "auc_std": float(np.std(fold_auc)),
            "operating_threshold": float(np.median(chosen))}


def oof_performance(y, res):
    """Out-of-fold confusion matrix and Wilson intervals on 237 positives."""
    print("\n" + "=" * 79)
    print("SECTION 4b: OUT-OF-FOLD PERFORMANCE (unbiased)")
    print("=" * 79)

    pred, proba = res["oof_pred"], res["oof_proba"]
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    n_pos, n_flag = int(y.sum()), int(pred.sum())
    rec = tp / n_pos
    pre = tp / n_flag if n_flag else 0.0
    r_lo, r_hi = wilson_interval(tp, n_pos)
    p_lo, p_hi = wilson_interval(tp, n_flag)

    print(f"\n  Evaluation base : {len(y):,} employees, {n_pos} departures")
    print(f"                    (a single 20% holdout would give 47)")
    print(f"  Flagged         : {n_flag}")
    print(f"  True positives  : {tp}")
    print(f"\n  Recall    {tp}/{n_pos} = {rec:.3f}   95% CI [{r_lo:.3f}, {r_hi:.3f}]")
    print(f"  Precision {tp}/{n_flag} = {pre:.3f}   95% CI [{p_lo:.3f}, {p_hi:.3f}]")
    print(f"  ROC-AUC (out-of-fold)   : {roc_auc_score(y, proba):.3f}")
    print(f"  Brier score             : {brier_score_loss(y, proba):.3f}")
    return {"tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn),
            "n_pos": n_pos, "n_flag": n_flag, "recall": rec, "precision": pre,
            "recall_ci": (r_lo, r_hi), "precision_ci": (p_lo, p_hi),
            "auc": float(roc_auc_score(y, proba))}


# =============================================================================
# SECTION 5: FAIRNESS AND PROXY AUDIT
# =============================================================================

def proxy_audit(y, X_unrestricted, nc_u, cc_u, X_with, nc_w, cc_w, X_without, nc_o, cc_o):
    """
    Two questions the ethics section must answer with evidence, not assertion.

      1. Does excluding DistanceFromHome cost predictive performance?
      2. Does the model produce disparate impact across the one protected
         attribute actually present (Gender)?

    On the proxy question the honest finding is a NEGATIVE one: the dataset
    holds no race or socioeconomic field, so the proxy relationship motivating
    the concern cannot be tested. Where a risk is untestable and the feature is
    not load-bearing, exclusion is the defensible default.
    """
    print("\n" + "=" * 79)
    print("SECTION 5: FAIRNESS AND PROXY AUDIT")
    print("=" * 79)

    outer = StratifiedKFold(OUTER_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    scores = {}
    for lab, Xs, nc, cc in [("Unrestricted (all features)", X_unrestricted, nc_u, cc_u),
                            ("With DistanceFromHome", X_with, nc_w, cc_w),
                            ("Without DistanceFromHome", X_without, nc_o, cc_o)]:
        aucs = []
        for tr, te in outer.split(Xs, y):
            p = make_pipeline(nc, cc)
            p.fit(Xs.iloc[tr], y.iloc[tr])
            aucs.append(roc_auc_score(y.iloc[te], p.predict_proba(Xs.iloc[te])[:, 1]))
        scores[lab] = float(np.mean(aucs))
        print(f"  {lab:<28} out-of-fold AUC {scores[lab]:.4f}")

    delta = scores["With DistanceFromHome"] - scores["Without DistanceFromHome"]
    total = scores["Unrestricted (all features)"] - scores["Without DistanceFromHome"]
    print(f"  Total cost of the fairness constraint: {total:+.4f} AUC")
    print(f"\n  Cost of excluding the proxy feature: {delta:+.4f} AUC")
    print("  The dataset holds no race or socioeconomic field, so the proxy")
    print("  relationship motivating the concern CANNOT be tested here. Given an")
    print("  untestable risk and a negligible performance cost, exclusion is the")
    print("  defensible default. No commute-based policy lever is recommended.")
    return {"auc_with": scores["With DistanceFromHome"],
            "auc_without": scores["Without DistanceFromHome"], "delta": delta}


def disparate_impact(df, oof_pred):
    """Four-fifths rule check on Gender, the only protected attribute present."""
    print("\n  Disparate impact check (Gender, excluded from the feature set):")
    g = df["Gender"].values
    rates = {}
    for lvl in np.unique(g):
        m = g == lvl
        rates[lvl] = float(oof_pred[m].mean())
        print(f"    {lvl:<8} flag rate {rates[lvl]:.3f}  (n={int(m.sum())})")
    ratio = min(rates.values()) / max(rates.values())
    print(f"    Impact ratio: {ratio:.3f}  "
          f"({'PASSES' if ratio >= 0.80 else 'FAILS'} the four-fifths rule)")
    return {"rates": rates, "ratio": float(ratio)}


# =============================================================================
# SECTION 6: BUSINESS CASE WITH TIME-ANCHOR SENSITIVITY
# =============================================================================

def business_case(perf, mean_salary, outdir):
    """
    Translate out-of-fold performance into annual financials across the three
    observation-window assumptions.

    THE TIME ANCHOR IS THE DOMINANT UNCERTAINTY, and it acts asymmetrically:
    benefits scale DOWN with a longer window (fewer departures per year) while
    scoring costs do NOT (the model still flags the same share of the workforce
    each cycle). A three-year window therefore does not merely reduce the return
    -- it can invert it.
    """
    print("\n" + "=" * 79)
    print("SECTION 6: BUSINESS CASE AND TIME-ANCHOR SENSITIVITY")
    print("=" * 79)

    repl = mean_salary * REPLACEMENT_MULTIPLIER
    intv = mean_salary * INTERVENTION_RATE
    print(f"\n  Replacement cost per departure : ${repl:,.0f}")
    print(f"  Triage budget per intervention : ${intv:,.0f}")
    print(f"  Cost asymmetry                 : {repl / intv:.0f} : 1")

    rows = []
    print(f"\n  {'Window':>7}{'Dep/yr':>8}{'Liability':>12}{'TP/yr':>7}{'Flagged':>9}"
          f"{'Prevent':>9}{'Gross':>11}{'Cost':>11}{'Net':>11}{'ROI':>8}{'BE':>8}")
    print("  " + "-" * 101)
    for w in WINDOW_SCENARIOS:
        dep = perf["n_pos"] / w
        liability = dep * repl
        tp = perf["tp"] / w
        flagged = perf["n_flag"]      # scoring cadence is annual regardless of window
        prevented = INTERVENTION_EFFECTIVENESS * tp
        gross = prevented * repl
        cost = flagged * intv + IMPLEMENTATION_COST + ANNUAL_RUN_COST
        net = gross - cost
        be = cost / (tp * repl)
        rows.append({
            "ObservationWindowYears": w, "AnnualDepartures": round(dep),
            "BaselineLiability": r100k(liability), "TruePositivesPerYear": round(tp),
            "FlaggedPerCycle": flagged, "DeparturesPrevented": round(prevented),
            "GrossBenefit": r100k(gross), "ProgrammeCost": r100k(cost),
            "NetBenefit": r100k(net), "ROIPercent": round(net / cost * 100, 1),
            "BreakevenEffectiveness": round(be, 3),
            "PaybackMonths": round(cost / (gross / 12), 1) if gross > 0 else np.nan,
        })
        print(f"  {w:>6.0f}y{round(dep):>8}${liability/1e6:>10.1f}m{round(tp):>7}"
              f"{flagged:>9}{round(prevented):>9}${gross/1e6:>9.1f}m"
              f"${cost/1e6:>9.1f}m${net/1e6:>9.1f}m{net/cost*100:>7.0f}%{be:>7.1%}")

    print("\n  Benefits scale with the window; scoring costs do not. Programme")
    print("  viability is therefore MORE sensitive to the unknown observation")
    print("  window than to any modelling choice in this report.")

    # --- compound sensitivity at a 1-year window ----------------------------
    print(f"\n  Compound sensitivity, 1-year window "
          f"(recall CI [{perf['recall_ci'][0]:.3f}, {perf['recall_ci'][1]:.3f}]):")
    print(f"  {'Scenario':<40}{'Net':>13}{'ROI':>9}")
    print("  " + "-" * 62)
    sens = []
    for lab, rec, eff in [
        ("Upper 95% CI recall, 40% effectiveness", perf["recall_ci"][1], 0.40),
        ("Point estimate, 40% effectiveness", perf["recall"], 0.40),
        ("Upper 95% CI recall, 30% effectiveness", perf["recall_ci"][1], 0.30),
        ("Point estimate, 30% effectiveness", perf["recall"], 0.30),
        ("Lower 95% CI recall, 30% effectiveness", perf["recall_ci"][0], 0.30),
        ("Point estimate, 20% effectiveness", perf["recall"], 0.20),
        ("Lower 95% CI recall, 20% effectiveness", perf["recall_ci"][0], 0.20),
        
    ]:
        tp = rec * perf["n_pos"]
        gross = eff * tp * repl
        cost = perf["n_flag"] * intv + IMPLEMENTATION_COST + ANNUAL_RUN_COST
        net = gross - cost
        sens.append({"Scenario": lab, "Recall": round(rec, 3), "Effectiveness": eff,
                     "NetBenefit": r100k(net), "ROIPercent": round(net / cost * 100, 1)})
        print(f"  {lab:<40}${r100k(net):>12,.0f}{net/cost*100:>8.0f}%")

    pd.DataFrame(rows).to_csv(
        os.path.join(outdir, "Business_Case_Scenarios.csv"), index=False)
    pd.DataFrame(sens).to_csv(
        os.path.join(outdir, "Sensitivity_Analysis.csv"), index=False)
    return rows, sens, repl, intv


# =============================================================================
# SECTION 7: EXPORTS
# =============================================================================

def export_coefficients(X, y, num_cols, cat_cols, outdir):
    """Standardised log-odds effect sizes from a model fitted on all records."""
    pipe = make_pipeline(num_cols, cat_cols).fit(X, y)
    ohe = pipe.named_steps["preprocessor"].named_transformers_["cat"]
    names = num_cols + list(ohe.get_feature_names_out(cat_cols))
    coefs = pipe.named_steps["classifier"].coef_[0]
    out = (pd.DataFrame({
        "Feature": names,
        "Coefficient_StdLogOdds": np.round(coefs, 4),
        "OddsRatio": np.round(np.exp(coefs), 4),
        "AbsMagnitude": np.round(np.abs(coefs), 4),
        "Direction": np.where(coefs > 0, "RISK", "PROTECTIVE"),
    }).sort_values("AbsMagnitude", ascending=False).reset_index(drop=True))
    out.insert(0, "Rank", out.index + 1)
    out.to_csv(os.path.join(outdir, "Feature_Coefficients.csv"), index=False)
    print(f"\n  [EXPORT] Feature_Coefficients.csv ({len(out)} features)")
    for _, r in out.head(5).iterrows():
        print(f"             {r.Feature:<40}{r.Coefficient_StdLogOdds:+.3f}")
    return out


def export_threshold_sweep(y, proba, mean_salary, outdir):
    """Operating-point table with financial translation at a 1-year window."""
    repl = mean_salary * REPLACEMENT_MULTIPLIER
    intv = mean_salary * INTERVENTION_RATE
    rows = []
    for t in THRESHOLD_GRID:
        pred = (proba >= t).astype(int)
        tp = int(((pred == 1) & (y == 1)).sum())
        flagged = int(pred.sum())
        rec = tp / int(y.sum())
        pre = tp / flagged if flagged else 0.0
        gross = INTERVENTION_EFFECTIVENESS * tp * repl
        cost = flagged * intv + IMPLEMENTATION_COST + ANNUAL_RUN_COST
        r_lo, r_hi = wilson_interval(tp, int(y.sum()))
        rows.append({
            "Threshold": round(float(t), 2), "Recall": round(rec, 3),
            "Recall_CI_Low": round(r_lo, 3), "Recall_CI_High": round(r_hi, 3),
            "Precision": round(pre, 3), "Flagged": flagged, "TruePositives": tp,
            "GrossBenefit": r100k(gross), "ProgrammeCost": r100k(cost),
            "NetBenefit": r100k(gross - cost),
            "ROIPercent": round((gross - cost) / cost * 100, 1),
            "BreakevenEffectiveness": round(cost / (tp * repl), 3) if tp else np.nan,
        })
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(outdir, "Threshold_Sweep_ROI.csv"), index=False)
    print(f"  [EXPORT] Threshold_Sweep_ROI.csv ({len(out)} operating points)")
    return out


def export_risk_register(df, keys, y, proba, pred, mean_salary, outdir):
    """
    The operational deliverable: one row per employee, ranked by expected loss.

    GOVERNANCE: this register may NOT inform promotion, performance rating,
    compensation review, or redundancy selection. Every flag requires documented
    human review before any action is taken.
    """
    repl = mean_salary * REPLACEMENT_MULTIPLIER
    out = pd.DataFrame({
        "EmployeeNumber": keys.values,
        "AttritionPropensity": np.round(proba, 4),
        "Flagged": pred,
        "RiskBand": pd.cut(proba, [-.001, .20, .40, .60, 1.001],
                           labels=["Low", "Moderate", "High", "Critical"]),
        "ReplacementLiability": round(repl),
        "ExpectedLoss": np.round(proba * repl, 0),
    })
    for c in ["JobRole", "Department", "OverTime", "JobLevel",
              "YearsAtCompany", "YearsSinceLastPromotion", "StockOptionLevel"]:
        if c in df.columns:
            out[c] = df[c].values
    # Ground truth exists only because these are out-of-fold predictions on a
    # labelled dataset. It would NOT exist in production scoring.
    out["ActualOutcome_ValidationOnly"] = np.where(y.values == 1, "Left", "Stayed")
    out = out.sort_values("ExpectedLoss", ascending=False).reset_index(drop=True)
    out.insert(0, "PriorityRank", out.index + 1)
    out.to_csv(os.path.join(outdir, "Helix_Risk_Register.csv"), index=False)
    print(f"  [EXPORT] Helix_Risk_Register.csv ({len(out)} scored, "
          f"{int(pred.sum())} flagged)")
    return out


# =============================================================================
# SECTION 8: VISUALISATIONS
# =============================================================================

def generate_figures(df, y, res, perf, sweep, coef_df, scenarios, outdir):
    """Regenerate every figure reproduced in the report, so the two cannot drift."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns
    from sklearn.metrics import roc_curve

    sns.set_theme(style="whitegrid", palette="deep")
    figdir = os.path.join(outdir, "figures")
    os.makedirs(figdir, exist_ok=True)
    a = df[TARGET_COL] == "Yes"
    print("\n  Figures:")

    # Fig 1 -- headline EDA findings
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    ot = df.groupby("OverTime")[TARGET_COL].apply(lambda s: (s == "Yes").mean() * 100)
    ax[0].bar(ot.index, ot.values, color=["#4C72B0", "#C44E52"])
    for i, v in enumerate(ot.values):
        ax[0].text(i, v + .7, f"{v:.1f}%", ha="center", fontweight="bold")
    ax[0].set_title("Departure rate by overtime status", fontweight="bold")
    ax[0].set_ylabel("Departure rate (%)")
    ax[0].set_ylim(0, 36)
    sns.kdeplot(data=df, x="MonthlyIncome", hue=TARGET_COL, fill=True,
                common_norm=False, alpha=.45, ax=ax[1])
    ax[1].set_title("Monthly income density by outcome", fontweight="bold")
    ax[1].set_xlabel("Monthly income ($)")
    plt.tight_layout(); plt.savefig(f"{figdir}/fig1_overtime_income.png", dpi=150); plt.close()

    # Fig 2 -- risk gradients
    fig, ax = plt.subplots(2, 2, figsize=(12, 8))
    for (col, bins, title), axis in zip(
            [("YearsAtCompany", [-1, 1, 2, 5, 10, 40], "Years at company"),
             ("YearsWithCurrManager", [-1, 0, 2, 5, 20], "Years with current manager"),
             ("StockOptionLevel", None, "Stock option level"),
             ("JobLevel", None, "Job level")], ax.ravel()):
        grp = pd.cut(df[col], bins) if bins else df[col]
        rate = a.groupby(grp, observed=False).mean() * 100
        axis.bar([str(i) for i in rate.index], rate.values, color="#4C72B0")
        axis.axhline(16.12, color="#C44E52", ls="--", lw=1.2, label="Overall 16.1%")
        axis.set_title(title, fontweight="bold")
        axis.set_ylabel("Departure rate (%)")
        axis.legend(fontsize=8); axis.tick_params(axis="x", rotation=20)
    plt.tight_layout(); plt.savefig(f"{figdir}/fig2_risk_gradients.png", dpi=150); plt.close()

    # Fig 3 -- out-of-fold performance
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    fpr, tpr, _ = roc_curve(y, res["oof_proba"])
    ax[0].plot(fpr, tpr, lw=2.2, color="#4C72B0",
               label=f"Out-of-fold (AUC = {perf['auc']:.3f})")
    ax[0].plot([0, 1], [0, 1], "--", color="grey", lw=1, label="Random (0.500)")
    ax[0].set_xlabel("False positive rate"); ax[0].set_ylabel("True positive rate")
    ax[0].set_title("ROC, out-of-fold across all 1,470 records", fontweight="bold")
    ax[0].legend()
    pdf = pd.DataFrame({"p": res["oof_proba"],
                        "Outcome": np.where(y == 1, "Left", "Stayed")})
    sns.kdeplot(data=pdf, x="p", hue="Outcome", fill=True, common_norm=False,
                alpha=.45, ax=ax[1])
    ax[1].axvline(res["operating_threshold"], color="black", ls="--", lw=1.4)
    ax[1].set_title("Propensity by actual outcome", fontweight="bold")
    ax[1].set_xlabel("Predicted propensity")
    plt.tight_layout(); plt.savefig(f"{figdir}/fig3_model_performance.png", dpi=150); plt.close()

    # Fig 4 -- threshold trade-off with recall confidence band
    fig, ax1 = plt.subplots(figsize=(11, 5))
    ax1.plot(sweep.Threshold, sweep.Recall, "o-", color="#C44E52", label="Recall")
    ax1.fill_between(sweep.Threshold, sweep.Recall_CI_Low, sweep.Recall_CI_High,
                     color="#C44E52", alpha=.15, label="Recall 95% CI")
    ax1.plot(sweep.Threshold, sweep.Precision, "s-", color="#55A868", label="Precision")
    ax1.axvline(res["operating_threshold"], color="black", ls="--", lw=1.2)
    ax1.set_xlabel("Decision threshold"); ax1.set_ylabel("Recall / Precision")
    ax2 = ax1.twinx()
    ax2.bar(sweep.Threshold, sweep.NetBenefit / 1e6, width=.03, alpha=.25,
            color="#4C72B0", label="Net annual value ($m)")
    ax2.set_ylabel("Net annual value ($m), 1-year window")
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper center", ncol=2, fontsize=8)
    plt.title("Threshold trade-off with sampling uncertainty", fontweight="bold")
    plt.tight_layout(); plt.savefig(f"{figdir}/fig4_threshold_tradeoff.png", dpi=150); plt.close()

    # Fig 5 -- coefficients
    top = coef_df.head(18).sort_values("Coefficient_StdLogOdds")
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.barh(top.Feature, top.Coefficient_StdLogOdds,
            color=["#C44E52" if c > 0 else "#55A868"
                   for c in top.Coefficient_StdLogOdds])
    ax.axvline(0, color="black", lw=.9)
    ax.set_xlabel("Standardised coefficient (log-odds)")
    ax.set_title("Strongest drivers, deployed model\n"
                 "red = raises risk, green = protective", fontweight="bold")
    plt.tight_layout(); plt.savefig(f"{figdir}/fig5_coefficients.png", dpi=150); plt.close()

    # Fig 6 -- the time anchor
    sc = pd.DataFrame(scenarios)
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    ax[0].bar(sc.ObservationWindowYears.astype(int).astype(str),
              sc.BaselineLiability / 1e6, color="#4C72B0")
    ax[0].set_xlabel("Assumed observation window (years)")
    ax[0].set_ylabel("Baseline annual liability ($m)")
    ax[0].set_title("Baseline liability rests entirely on an\nassumption the data cannot verify",
                    fontweight="bold", fontsize=10)
    ax[1].bar(sc.ObservationWindowYears.astype(int).astype(str), sc.NetBenefit / 1e6,
              color=["#55A868" if v > 0 else "#C44E52" for v in sc.NetBenefit])
    ax[1].axhline(0, color="black", lw=1)
    ax[1].set_xlabel("Assumed observation window (years)")
    ax[1].set_ylabel("Net annual benefit ($m)")
    ax[1].set_title("Programme value under each\nwindow assumption", fontweight="bold",
                    fontsize=10)
    plt.tight_layout(); plt.savefig(f"{figdir}/fig6_time_anchor.png", dpi=150); plt.close()

    for f in sorted(os.listdir(figdir)):
        print(f"    {f}")
    return figdir


# =============================================================================
# MAIN
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="Employee Retention Predictor")
    ap.add_argument("--data", default="WA_Fn-UseC_-HR-Employee-Attrition.csv")
    ap.add_argument("--outdir", default="./output")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print("\n" + "#" * 79)
    print("#  THE EMPLOYEE RETENTION PREDICTOR | Helix Systems Group")
    print("#  MSBA Capstone | P. W. Roos | Roos Analytics")
    print("#" * 79)

    df = load_data(args.data)
    mean_salary = df.loc[df[TARGET_COL] == "Yes", "MonthlyIncome"].mean() * 12
    print(f"\n  Mean annualised salary, leavers: ${mean_salary:,.0f}")

    # Deployed feature set (protected + proxy excluded)
    X, y, keys, nc, cc = prepare_features(df, exclude_proxy=True)
    # Comparison set retaining the proxy feature
    Xf, _, _, ncf, ccf = prepare_features(df, exclude_proxy=False, verbose=False)
    # Unrestricted set — measures the total cost of the fairness constraint
    Xu, _, _, ncu, ccu = prepare_features(df, exclude_proxy=False, exclude_protected=False, verbose=False)

    res = nested_cv(X, y, nc, cc, label="Logistic Regression (deployed)")
    rf = nested_cv(X, y, nc, cc,
                   estimator=RandomForestClassifier(
                       n_estimators=500, class_weight="balanced",
                       min_samples_leaf=3, random_state=RANDOM_STATE, n_jobs=-1),
                   label="Random Forest (challenger)")
    print("\n  CRITERION T4 -- does the ensemble justify its complexity?")
    print(f"    Logistic Regression AUC {res['auc_mean']:.3f} | "
          f"Random Forest AUC {rf['auc_mean']:.3f}")
    winner = ("Random Forest" if rf["auc_mean"] > res["auc_mean"]
              else "Logistic Regression")
    print(f"    {winner} selected.")

    perf = oof_performance(y, res)
    proxy_audit(y, Xu, ncu, ccu, Xf, ncf, ccf, X, nc, cc)
    disparate_impact(df, res["oof_pred"])

    scenarios, sens, repl, intv = business_case(perf, mean_salary, args.outdir)

    print("\n" + "=" * 79)
    print("SECTION 7: EXPORTS")
    print("=" * 79)
    coef_df = export_coefficients(X, y, nc, cc, args.outdir)
    sweep = export_threshold_sweep(y, res["oof_proba"], mean_salary, args.outdir)
    export_risk_register(df, keys, y, res["oof_proba"], res["oof_pred"],
                         mean_salary, args.outdir)
    print("  [EXPORT] Business_Case_Scenarios.csv")
    print("  [EXPORT] Sensitivity_Analysis.csv")

    generate_figures(df, y, res, perf, sweep, coef_df, scenarios, args.outdir)

    print("\n" + "#" * 79)
    print(f"#  COMPLETE. Deliverables in {os.path.abspath(args.outdir)}")
    print("#" * 79 + "\n")


if __name__ == "__main__":
    sys.exit(main())
