# The Employee Retention Predictor

A predictive HR analytics proof of concept: ranking employees by propensity to leave, and testing whether acting on that ranking is worth the money.

**MSBA Capstone** | Quantic School of Business and Technology | Philip Wouter Roos

---

## What this is

A fictional mid-sized technology firm, Helix Systems Group, loses 16.1% of its 1,470 employees over the dataset's observation window. At an industry-standard replacement cost of 1.5× annualised salary, that is roughly $20.4 million a year — spent entirely *after* each resignation, when the decision is already irreversible.

This repository contains the full analytical pipeline behind the capstone: a supervised classifier that scores every employee for relative propensity to leave, validated under nested cross-validation, and translated into a gated business case with explicit sensitivity to its own weakest assumptions.

## Headline results

| Measure | Value |
|---|---|
| ROC-AUC (out-of-fold, all 1,470 records) | **0.813** |
| Recall — leavers identified | **0.650** (95% CI 0.587–0.708) |
| Precision — flags that are genuine | **0.429** (95% CI 0.379–0.481) |
| Break-even intervention effectiveness | **16.9%** |
| Net annual benefit (1-year window) | **≈ $1.7m**, ≈77% ROI, ≈6.8-month payback |

Protected attributes (`Gender`, `Age`, `MaritalStatus`) and one proxy-risk feature (`DistanceFromHome`) are excluded from the model. Gender impact ratio is 0.908, passing the four-fifths rule.

## Two limitations that govern every number above

**The data is synthetic.** This is the IBM HR Analytics Employee Attrition dataset, which is fabricated. An AUC of 0.813 is a property of IBM's data generator, not of human attrition behaviour. **This repository demonstrates that the analytical architecture is sound. It does not establish that comparable signal exists in any real workforce.** Coefficients must be refitted and performance re-established from scratch on live HRIS records before any operational use.

**The observation window is unknown.** The target records *that* an employee left, not *when*. The dataset does not state over what period its 237 departures accumulated. The financial case assumes one year; under a two- or three-year window the programme returns −$0.3m or −$0.9m instead of +$1.7m. Benefits scale with the window, scoring costs do not. `Business_Case_Scenarios.csv` reports all three.

Both limitations are treated at length in the written report, not buried in it.

## Repository structure

```
helix-retention-predictor/
├── employee_retention_predictor.py          End-to-end pipeline
├── WA_Fn-UseC_-HR-Employee-Attrition.csv    Raw dataset (unmodified)
├── requirements.txt
├── .gitignore
├── docs/                                    Written deliverables
└── output/                                  Everything the script generates
    ├── Business_Case_Scenarios.csv          Financials under each observation window
    ├── Feature_Coefficients.csv             Standardised log-odds effect sizes
    ├── Helix_Risk_Register.csv              Per-employee scores, ranked by expected loss
    ├── Sensitivity_Analysis.csv             Joint sensitivity: recall CI × effectiveness
    ├── Threshold_Sweep_ROI.csv              Operating points with financial translation
    └── figures/                             The six figures reproduced in the report
```

## Reproducing the analysis

```bash
pip install -r requirements.txt
python employee_retention_predictor.py
```

Defaults read `WA_Fn-UseC_-HR-Employee-Attrition.csv` from the repository root and write to `./output`. To override:

```bash
python employee_retention_predictor.py --data path/to/data.csv --outdir ./output
```

Every partitioning and estimator call uses `random_state=42`. All figures and tables in the written report regenerate exactly from a single run.

## What the pipeline does

1. **Ingestion and validation** — structural checks, and an explicit warning that the 16.12% figure is a prevalence rather than an annual rate.
2. **Field elimination** — removes three zero-variance columns, the row identifier, three protected attributes, and one proxy-risk feature. 26 predictors remain.
3. **Leakage-safe preprocessing** — `StandardScaler` and `OneHotEncoder` composed inside a `Pipeline`, so transformations are only ever fitted on training folds.
4. **Nested cross-validation** — the decision threshold is a tuned parameter, so it is selected in an inner loop and evaluated in an outer loop. This yields out-of-fold predictions for all 1,470 records, raising the evaluation base from 47 positive cases to 237 and roughly halving every confidence interval.
5. **Fairness and proxy audit** — measures the AUC cost of the ethical exclusions and applies the four-fifths rule to realised predictions.
6. **Business case** — translates out-of-fold performance into annual financials across one-, two- and three-year observation windows.

The threshold is selected on **expected net business value**, not F1 or accuracy. At base assumptions that means flagging one more employee pays whenever their probability of being a retained true positive exceeds 0.10 / 0.45 = 22.2%.

## A note on the risk register

`Helix_Risk_Register.csv` carries an `ActualOutcome_ValidationOnly` column. It exists only because these are out-of-fold predictions on a labelled dataset. **It would not exist in production scoring** and must not be read as a model output.

The register is also subject to the governance provisions in the report: human-in-the-loop review before any action, and a standing prohibition on using propensity scores in promotion, performance, compensation or redundancy decisions.

## Data source

IBM HR Analytics Employee Attrition & Performance, published by IBM as sample data and distributed via [Kaggle](https://www.kaggle.com/datasets/pavansubhasht/ibm-hr-analytics-attrition-dataset). Included here unmodified for reproducibility. All analysis, figures and derived outputs are the author's own work.

## Author

Philip Wouter Roos — BEng (Chemical Engineering), University of Pretoria; MSBA candidate, Quantic School of Business and Technology.

Analysis conducted in Python (pandas, scikit-learn, seaborn). LLM assistance cited per Quantic conventions in the written report.
