#!/usr/bin/env python3
"""
Check an NYC building for public complaint/issue signals.

Standalone: uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
SOCRATA_BASE = "https://data.cityofnewyork.us/resource"

HPD_VIOLATIONS = "wvxf-dwi5"
DOB_COMPLAINTS = "eabe-havv"
BEDBUG = "wz6d-d3jb"

PATTERN_MIN = 3
HEAT_RE = re.compile(r"HEAT|HOT WATER", re.I)
PEST_RE = re.compile(r"RODENT|MICE|ROACH|VERMIN|BED ?BUG|INSECT|PEST", re.I)

MANY_OPEN_VIOLATIONS = 10
SEVERE_OPEN_VIOLATIONS = 25
SEVERE_SERIOUS_VIOLATIONS = 5
HIGH_DOB_COMPLAINTS_24MO = 8

DOB_CATEGORY_DESCRIPTIONS = {
    "05": "Permit none",
    "31": "Certificate of occupancy none/illegal/contrary to CO",
    "37": "Egress locked/blocked/improper/no secondary means",
    "45": "Illegal conversion",
    "59": "Electrical wiring defective/exposed, in progress",
    "66": "Plumbing work illegal/no permit",
    "73": "Failure to maintain",
    "83": "Construction contrary/beyond approved plans/permits",
    "94": "Plumbing defective/leaking/not maintained",
    "1Z": "Enforcement work order",
    "2F": "Building under structural monitoring",
    "3A": "Unlicensed/illegal/improper electrical work in progress",
    "4A": "Illegal hotel rooms in residential buildings",
    "4G": "Illegal conversion no-access follow-up",
    "4W": "Woodside Settlement Project",
    "5G": "Unlicensed/illegal/improper work in progress",
}


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    request = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def socrata_rows(dataset: str, params: dict[str, str]) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(params)
    url = f"{SOCRATA_BASE}/{dataset}.json?{query}"
    headers = {"Accept": "application/json"}
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    data = get_json(url, headers=headers)
    if not isinstance(data, list):
        raise RuntimeError(f"Socrata {dataset} returned non-list JSON")
    return data


def where_eq(field: str, value: str) -> str:
    return f"{field}='{value.replace(chr(39), chr(39) + chr(39))}'"


def parse_bbl(bbl: str) -> dict[str, str] | None:
    if not re.fullmatch(r"\d{10}", bbl):
        return None
    return {
        "boroid": bbl[0],
        "block": str(int(bbl[1:6])),
        "lot": str(int(bbl[6:10])),
    }


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    formats = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value[:23], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def within_months(value: str | None, months: int) -> bool:
    date = parse_date(value)
    if not date:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    return date >= cutoff


def resolve_address(address: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"text": address, "size": "1"})
    data = get_json(f"{GEOSEARCH_URL}?{query}")
    features = data.get("features") or []
    if not features:
        return None

    feature = features[0]
    props = feature.get("properties") or {}
    pad = ((props.get("addendum") or {}).get("pad") or {})
    coords = (feature.get("geometry") or {}).get("coordinates") or []

    return {
        "normalizedAddress": props.get("label") or address,
        "borough": props.get("borough"),
        "zip": props.get("postalcode"),
        "bbl": pad.get("bbl"),
        "bin": pad.get("bin"),
        "longitude": coords[0] if len(coords) > 0 else None,
        "latitude": coords[1] if len(coords) > 1 else None,
    }


def derive_risk_level(signals: dict[str, Any], any_source_succeeded: bool) -> str:
    if not any_source_succeeded:
        return "unknown"

    open_violations = signals.get("openHpdViolations") or 0
    serious_open = signals.get("seriousHpdViolations") or 0
    hpd_24 = signals.get("hpdViolationCount24mo") or 0
    dob_24 = signals.get("dobComplaintCount24mo") or 0

    if (
        signals.get("bedbugFlag")
        or serious_open >= SEVERE_SERIOUS_VIOLATIONS
        or open_violations >= SEVERE_OPEN_VIOLATIONS
        or open_violations >= MANY_OPEN_VIOLATIONS
        or signals.get("heatHotWaterViolationPattern")
        or signals.get("pestViolationPattern")
        or dob_24 >= HIGH_DOB_COMPLAINTS_24MO
    ):
        return "high"

    if open_violations >= 3 or hpd_24 >= 10 or dob_24 >= 3:
        return "medium"

    return "low"


def check_building(bbl: str | None, bin_: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    any_source_succeeded = False

    signals: dict[str, Any] = {
        "hpdViolationCount12mo": None,
        "hpdViolationCount24mo": None,
        "openHpdViolations": None,
        "seriousHpdViolations": None,
        "heatHotWaterViolationPattern": None,
        "pestViolationPattern": None,
        "dobComplaintCount24mo": None,
        "bedbugFlag": None,
    }

    parts = parse_bbl(bbl) if bbl else None
    if parts:
        try:
            rows = socrata_rows(
                HPD_VIOLATIONS,
                {
                    "$where": " AND ".join(
                        [
                            where_eq("boroid", parts["boroid"]),
                            where_eq("block", parts["block"]),
                            where_eq("lot", parts["lot"]),
                        ]
                    ),
                    "$select": "class,violationstatus,novissueddate,novdescription",
                    "$limit": "1000",
                },
            )
            any_source_succeeded = True
            open_rows = [r for r in rows if r.get("violationstatus") == "Open"]
            recent_rows = [r for r in rows if within_months(r.get("novissueddate"), 24)]
            heat_rows = [r for r in recent_rows if HEAT_RE.search(str(r.get("novdescription") or ""))]
            pest_rows = [r for r in recent_rows if PEST_RE.search(str(r.get("novdescription") or ""))]

            signals["openHpdViolations"] = len(open_rows)
            signals["seriousHpdViolations"] = sum(1 for r in open_rows if str(r.get("class") or "").upper() == "C")
            signals["hpdViolationCount12mo"] = sum(1 for r in rows if within_months(r.get("novissueddate"), 12))
            signals["hpdViolationCount24mo"] = len(recent_rows)
            signals["heatHotWaterViolationPattern"] = len(heat_rows) >= PATTERN_MIN
            signals["pestViolationPattern"] = len(pest_rows) >= PATTERN_MIN

            if signals["heatHotWaterViolationPattern"]:
                warnings.append(f"{len(heat_rows)} recent heat/hot-water violations")
            if signals["pestViolationPattern"]:
                warnings.append(f"{len(pest_rows)} recent pest-related violations")
            if signals["seriousHpdViolations"]:
                warnings.append(f"{signals['seriousHpdViolations']} open class-C serious violations")
            if (signals["openHpdViolations"] or 0) >= MANY_OPEN_VIOLATIONS:
                warnings.append(f"{signals['openHpdViolations']} open HPD violations")
        except Exception as exc:
            warnings.append(f"HPD violations lookup failed: {exc}")

    if bin_:
        try:
            rows = socrata_rows(
                DOB_COMPLAINTS,
                {
                    "$where": where_eq("bin", bin_),
                    "$select": "status,date_entered",
                    "$limit": "1000",
                },
            )
            any_source_succeeded = True
            signals["dobComplaintCount24mo"] = sum(1 for r in rows if within_months(r.get("date_entered"), 24))
            if (signals["dobComplaintCount24mo"] or 0) >= HIGH_DOB_COMPLAINTS_24MO:
                warnings.append(f"{signals['dobComplaintCount24mo']} DOB complaints in the last 24 months")
        except Exception as exc:
            warnings.append(f"DOB complaints lookup failed: {exc}")

    if bbl:
        try:
            rows = socrata_rows(
                BEDBUG,
                {
                    "$where": where_eq("bbl", bbl),
                    "$select": "infested_dwelling_unit_count,filing_date",
                    "$limit": "100",
                },
            )
            any_source_succeeded = True
            signals["bedbugFlag"] = any(
                int(str(r.get("infested_dwelling_unit_count") or "0") or "0") > 0
                and within_months(r.get("filing_date"), 36)
                for r in rows
            )
            if signals["bedbugFlag"]:
                warnings.append("Recent bedbug infestation reported")
        except Exception as exc:
            warnings.append(f"Bedbug lookup failed: {exc}")

    return {
        **signals,
        "riskLevel": derive_risk_level(signals, any_source_succeeded),
        "warnings": warnings,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
    }


def dob_complaints(bin_: str, months: int) -> list[dict[str, Any]]:
    rows = socrata_rows(
        DOB_COMPLAINTS,
        {
            "$where": where_eq("bin", bin_),
            "$select": (
                "complaint_number,status,date_entered,house_number,house_street,"
                "complaint_category,unit,inspection_date,disposition_date,disposition_code"
            ),
            "$limit": "1000",
        },
    )

    filtered = [r for r in rows if within_months(r.get("date_entered"), months)]
    filtered.sort(
        key=lambda r: parse_date(r.get("date_entered")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return [
        {
            "complaintNumber": r.get("complaint_number"),
            "status": r.get("status"),
            "dateEntered": r.get("date_entered"),
            "address": " ".join(str(v) for v in [r.get("house_number"), r.get("house_street")] if v),
            "category": r.get("complaint_category"),
            "categoryDescription": DOB_CATEGORY_DESCRIPTIONS.get(str(r.get("complaint_category") or "").upper()),
            "unit": r.get("unit"),
            "inspectionDate": r.get("inspection_date"),
            "dispositionDate": r.get("disposition_date"),
            "dispositionCode": r.get("disposition_code"),
        }
        for r in filtered
    ]


def print_human(result: dict[str, Any]) -> None:
    building = result["building"]
    risk = result["risk"]

    print(f"Address: {building.get('normalizedAddress') or '(not resolved)'}")
    print(f"BBL: {building.get('bbl') or 'unknown'}")
    print(f"BIN: {building.get('bin') or 'unknown'}")
    print(f"Risk level: {risk['riskLevel']}")
    print("")
    print(f"Open HPD violations: {risk.get('openHpdViolations')}")
    print(f"Open class-C serious violations: {risk.get('seriousHpdViolations')}")
    print(f"HPD violations in last 12 months: {risk.get('hpdViolationCount12mo')}")
    print(f"HPD violations in last 24 months: {risk.get('hpdViolationCount24mo')}")
    print(f"Heat/hot-water pattern: {risk.get('heatHotWaterViolationPattern')}")
    print(f"Pest pattern: {risk.get('pestViolationPattern')}")
    print(f"DOB complaints in last 24 months: {risk.get('dobComplaintCount24mo')}")
    print(f"Recent bedbug flag: {risk.get('bedbugFlag')}")
    print("")
    if risk["warnings"]:
        print("Warnings:")
        for warning in risk["warnings"]:
            print(f"- {warning}")
    else:
        print("Warnings: none")

    complaints = result.get("dobComplaints") or []
    if complaints:
        print("")
        print("Recent DOB complaints:")
        for complaint in complaints:
            desc = complaint.get("categoryDescription") or f"category {complaint.get('category')}"
            print(
                f"- {complaint.get('dateEntered')}: {complaint.get('address')} - "
                f"{desc}; status {complaint.get('status')}; disposition {complaint.get('dispositionCode')}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check NYC public-data building complaint/issue signals.")
    parser.add_argument("address", nargs="?", help="NYC street address to resolve, unless --bbl/--bin are supplied.")
    parser.add_argument("--bbl", help="10-digit borough-block-lot identifier.")
    parser.add_argument("--bin", dest="bin_", help="NYC building identification number.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON. This is the default.")
    parser.add_argument("--human", action="store_true", help="Print a short human-readable summary.")
    parser.add_argument(
        "--dob-details",
        action="store_true",
        help="Include recent DOB complaint rows with category descriptions when a BIN is available.",
    )
    parser.add_argument("--months", type=int, default=24, help="Lookback window for --dob-details. Default: 24.")
    args = parser.parse_args()

    if not args.address and not args.bbl and not args.bin_:
        parser.error("provide an address, --bbl, --bin, or both --bbl and --bin")

    building: dict[str, Any] = {
        "inputAddress": args.address,
        "normalizedAddress": None,
        "bbl": args.bbl,
        "bin": args.bin_,
    }

    if args.address and not (args.bbl and args.bin_):
        resolved = resolve_address(args.address)
        if not resolved:
            raise RuntimeError(f"Could not resolve address: {args.address}")
        building.update(resolved)
        if args.bbl:
            building["bbl"] = args.bbl
        if args.bin_:
            building["bin"] = args.bin_

    risk = check_building(building.get("bbl"), building.get("bin"))
    result = {
        "building": building,
        "risk": risk,
        "sourceNotes": [
            "HPD complaints dataset uwyv-629c is login-restricted, so HPD complaint-like patterns are derived from HPD violations.",
            "This is a public-data screen, not a complete tenant-history or inspection report.",
        ],
    }
    if args.dob_details and building.get("bin"):
        result["dobComplaints"] = dob_complaints(building["bin"], args.months)

    if args.human:
        print_human(result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
