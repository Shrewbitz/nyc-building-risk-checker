# NYC building complaints/issues checker

This is a small, standalone handoff for checking whether an NYC apartment building has public complaint or maintenance risk signals.

It is intentionally separate from the full housing-finder app. You can put this folder in its own GitHub repo, or email the two files:

- `README.md`
- `check_nyc_building.py`

## What it checks

The script uses public NYC data sources:

- NYC Planning Labs GeoSearch: turns an address into BBL/BIN building IDs.
- HPD Violations (`wvxf-dwi5`): open violations, serious class-C violations, recent heat/hot-water patterns, recent pest patterns.
- DOB Complaints (`eabe-havv`): DOB complaint count in the last 24 months, when a BIN is available.
- Bedbug Annual Reports (`wz6d-d3jb`): recent reported infested dwelling units, when a BBL is available.

Note: HPD's old complaints dataset (`uwyv-629c`) is currently login-restricted, so this script does not use it. Heat, hot-water, pest, and maintenance signals are derived from verified HPD violation descriptions instead.

## Requirements

- Python 3.9 or newer.
- Internet access.
- No Python packages to install.

Optional:

- `SOCRATA_APP_TOKEN`: an NYC Open Data app token. The script works without it, but a token can reduce rate-limit issues.

## Run it

The examples use placeholders instead of real residential addresses.

From this folder, replace `ADDRESS HERE` with the apartment building address:

```bash
python3 check_nyc_building.py "ADDRESS HERE"
```

You can also pass building identifiers directly:

```bash
python3 check_nyc_building.py --bbl 4013210016
python3 check_nyc_building.py --bin 4625717
python3 check_nyc_building.py --bbl 4013210016 --bin 4625717
```

Pretty human summary:

```bash
python3 check_nyc_building.py "ADDRESS HERE" --human
```

Machine-readable JSON, useful for Codex/LLMs:

```bash
python3 check_nyc_building.py "ADDRESS HERE" --json
```

`--json` is the default.

If the risk result mentions DOB complaints, run the drill-down view:

```bash
python3 check_nyc_building.py "ADDRESS HERE" --dob-details --human
```

For JSON with recent DOB complaint rows:

```bash
python3 check_nyc_building.py "ADDRESS HERE" --dob-details --json
```

## Suggested prompt for Codex

```text
Use the local script check_nyc_building.py to check this NYC apartment building:

ADDRESS HERE

Run:
python3 check_nyc_building.py "ADDRESS HERE" --dob-details --json

Then summarize:
- the normalized address, BBL, and BIN, and whether the resolved address looks like the input
- risk level
- open HPD violations
- open class-C serious violations
- recent heat/hot-water pattern
- recent pest pattern
- DOB complaints in the last 24 months
- recent bedbug flag
- any warnings
- if DOB complaint rows are included, the dates, addresses, categories, category descriptions, status, and disposition codes

Do not claim this is a complete tenant-history report. Explain that it is a public-data screen. If the resolved address or DOB complaint row addresses look mismatched, say so clearly.
```

## How to interpret results

Risk levels are simple screening labels:

- `low`: no major public-data issues found by these sources.
- `medium`: some concerning signal, such as several open violations or a few DOB complaints.
- `high`: stronger warning signal, such as many open HPD violations, class-C serious violations, heat/hot-water pattern, pest pattern, high DOB complaint count, or recent bedbug report.
- `unknown`: the script could not successfully query any applicable data source.

This is not legal advice and not a complete inspection. Use it to decide what to ask a broker, landlord, current tenant, or building manager.

## How to send it

Best option: put this folder in a tiny GitHub repo. That gives your friend one stable link, makes updates easy, and lets Codex clone or inspect the files directly.

Emailing a zip also works because there are only two files. Use a zip if you want the lowest-friction one-time handoff, but GitHub is better if you expect to improve the script or documentation.

