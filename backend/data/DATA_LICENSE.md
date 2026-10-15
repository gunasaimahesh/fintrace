# Data Attribution and License

The IBM AML files in this directory (`ibm_aml_hi_small_slice.csv.gz`, `ibm_aml_hi_small_accounts.csv.gz`) and the label files in `backend/eval/` are derived from:

- **IBM Transactions for Anti Money Laundering (AML)**, HI-Small sample, by Erik Altman, Jovan Blanuša, Luc von Niederhäusern, Béni Egressy, Andreea Anghel and Kubilay Atasu (IBM Research).
  - Kaggle: https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml
  - Mirror used to build this slice: https://huggingface.co/datasets/OsamaMIT/IBM-AML-HI-Small
  - Paper: "Realistic Synthetic Financial Transactions for Anti-Money Laundering Models", NeurIPS 2023 Datasets and Benchmarks Track (arXiv:2306.16424).

The source data is synthetic (produced by the agent-based transaction generator described in the paper) and is licensed under the **Community Data License Agreement – Sharing, Version 1.0** (CDLA-Sharing-1.0). The full license text is in [`LICENSE-CDLA-Sharing-1.0.txt`](LICENSE-CDLA-Sharing-1.0.txt). These derived files are shared under the same license.

## Changes made to the source data

Produced by [`scripts/prepare_ibm_slice.py`](../../scripts/prepare_ibm_slice.py); parameters and counts are recorded in [`slice_manifest.json`](slice_manifest.json).

- Kept all 370 labeled laundering instances from `HI-Small_Patterns.txt` with every account involved, their direct counterparties, and a capped random sample of second-ring counterparties (40,000 accounts in total). All transfers between two kept accounts are included.
- Dropped self-transfers (same source and destination account).
- Converted `Amount Paid` to integer US cents using exchange rates implied by the dataset's own cross-currency transactions.
- Shifted every timestamp forward by a constant whole number of weeks (see `time_shift_days`), so relative timing and weekdays are unchanged.
- Account IDs are `acct-<Account Number>` (with the bank ID appended for the 8 account numbers that appear at two banks). Transfer IDs are `ibm-<row>`, where `<row>` is the 0-based data row in `HI-Small_Trans.csv`.
- Laundering labels were moved out of the transfer data into `backend/eval/labels.csv` and `backend/eval/instances.csv`. They are used only for evaluation and are never loaded into the graph.

Everything else in the demo graph (devices, IPs, the NovaPay scenario, employees, policies, tickets) is synthetic and was written for FinTrace.
