# IMAP_AUTOSORT_NILSIMSA(5) — File Formats Manual

## NAME
**imap_autosort_nilsimsa.conf** — scoring and threshold options for  
**imap_autosort_nilsimsa**

## SYNOPSIS
```
[nilsimsa]
threshold=INT
min_score=INT
min_average=FLOAT
min_over=FLOAT
weight_headers=HDR[,HDR...]
headers_skip=HDR[,HDR...]
weight_headers_by=INT
xinclude=HDR[,HDR...]

[openai]
api_key=STRING
sender_skip_llm=GLOBS
```

## DESCRIPTION
**imap_autosort_nilsimsa** sorts IMAP mail by computing Nilsimsa similarity
over normalized headers, then scoring candidate folders using distances
strictly greater than a configured **threshold**.  
Distances are in the Nilsimsa range (−127..128). Only values strictly greater
than the threshold participate in scoring.

## OPTIONS (SCORING & THRESHOLDS)

**threshold** (INT; default 50)  
:   Nilsimsa distance cut-off. Only distances *x* where `x > threshold` are scored.  
    Historically, values around 65 worked well.

**min_score** (INT; default 100)  
:   Minimum total score the leading folder must reach to be chosen when resolving ties.

**min_average** (FLOAT; default 0)  
:   Minimum per-message average for the leading folder that must be satisfied.

**min_over** (FLOAT; default 1)  
:   Minimum number of over-threshold samples required before an average is computed and  
    the log-boost is applied.

**weight_headers** (LIST)  
:   Comma-separated list of header names to up-weight in the normalization text.

**headers_skip** (LIST)  
:   Comma-separated list of header names to ignore during normalization.

**weight_headers_by** (INT; default 1)  
:   Multiplier used with weight_headers; repeats the header string this many times.

**xinclude** (LIST)  
:   Specific X- headers to include; all others are stripped.

### OPENAI / CLASSIFICATION

**api_key** (STRING)  
:   If present, enables calls to an LLM for lightweight labeling. Labels are added  
    to the normalized header before Nilsimsa, influencing similarity.

**sender_skip_llm** (LIST of globs)  
:   Senders that should not be sent to the LLM.

> **Note:** If classification text contains “Spam”/“Phishing Suspected” above 0.10 probability,  
> the sorter sets a flag regardless of similarity scoring.

## SCORING ALGORITHM
For each candidate folder:

1. Keep only distances strictly over `threshold`.
2. Convert each distance `x` to a per-message score:
   ```
   score = 100 * (x − threshold) / (128 − threshold)
   ```
3. Sum scores to get `total_score`.
4. If the number of over-threshold hits ≥ `min_over`:
   - `average = total_score / count`
   - `total_score *= log10(count)` (when count > 1)

The resolver prefers the folder with the **highest average**.  
Total score is the secondary key.

## LADDERING / TIE-BREAK
If the top two folders are not clearly separated:

- Start with `T = threshold`. Compute stats for all folders.  
- Let `r1 = lead_avg / sum_avg` and `r2 = runner_avg / sum_avg`.  
- If `(r1 − r2) ≥ 0.10`, decide now; else raise `T` by +5 and retry.  
- Stop if `T ≥ 125`.  
- A winner must also satisfy `min_score` and `min_average`.  
- Otherwise, the message remains in the "new" folder.

## HEADER NORMALIZATION
- Removes volatile noise (Date, Message-Id, most X- headers).  
- Limits DKIM to the `d=` domain.  
- Keeps only headers not listed in `headers_skip`.  
- Headers listed in `weight_headers` are repeated `weight_headers_by` times.  
- Ensures stable digests for consistent traffic.

## EXAMPLES

### Example 1: Conservative filing
```
[nilsimsa]
threshold=85
min_score=200
min_average=30
min_over=3
```
- Only very strong similarities (`>85`) count.  
- At least 3 over-threshold matches are required.  
- Prevents misfilings but leaves more mail in INBOX.  

### Example 2: Aggressive filing
```
[nilsimsa]
threshold=60
min_score=50
min_average=0
min_over=1
```
- Even weak similarities (`>60`) are considered.  
- Only one over-threshold hit is enough.  
- High recall, but risk of false positives.  

### Example 3: Header weighting
```
[nilsimsa]
threshold=75
weight_headers=from,subject
weight_headers_by=2
```
- “From” and “Subject” headers count double.  
- Useful for newsletters or senders with consistent formatting.  

### Example 4: Spam guard with LLM
```
[openai]
api_key=sk-XXXX
sender_skip_llm=*@trusted.com,alerts@bank.com
```
- Uses LLM labels to flag suspicious messages.  
- Skips trusted senders to save tokens and latency.

## FILES
`imap_autosort.conf.sample` — example configuration provided with the repo.

## HISTORY
- Original README notes that “like” messages scored around 80.  
- Default threshold is lower (50) but tunable.

## AUTHOR
Marc Lucke <marc@marcsnet.com>
