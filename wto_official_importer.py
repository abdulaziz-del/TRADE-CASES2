#!/usr/bin/env python3
"""
WTO official dispute case importer.
Fetches official WTO DS case pages and builds a JSON dataset for the Flask app.
Source pattern: https://www.wto.org/english/tratop_e/dispu_e/cases_e/ds{N}_e.htm

Design rule: do not invent legal findings or document URLs. If a field cannot be
extracted from the official page, leave it blank or generic and keep the official page URL.
"""
from __future__ import annotations
import argparse, json, re, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.wto.org/english/tratop_e/dispu_e/cases_e/ds{n}_e.htm"
TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 WTO-dispute-database-builder/1.0"}

AGREEMENT_CODES = [
    "GATT", "GATS", "TRIPS", "DSU", "SPS", "TBT", "SCM", "ASCM", "ADA",
    "Anti-Dumping", "Safeguards", "Agriculture", "TRIMs", "ATC", "CVA", "ROO",
    "Import Licensing", "Customs Valuation", "Preshipment Inspection"
]

COUNTRY_ALIASES = {
    "United States of America": "United States",
    "US": "United States",
    "U.S.": "United States",
    "USA": "United States",
    "European Communities": "European Union",
    "EC": "European Union",
    "EU": "European Union",
    "Republic of Korea": "Korea",
    "Korea, Republic of": "Korea",
    "Turkey": "Türkiye",
    "Chinese Taipei": "Chinese Taipei",
    "Russian Federation": "Russia",
    "Viet Nam": "Vietnam",
    "Kingdom of Saudi Arabia": "Saudi Arabia",
}

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(rf"\b(\d{{1,2}} (?:{MONTHS}) \d{{4}})\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm_country(s: str) -> str:
    s = clean(s).strip(".;,")
    return COUNTRY_ALIASES.get(s, s)


def split_parties(s: str) -> List[str]:
    s = clean(s)
    if not s:
        return []
    s = re.sub(r"\b(and|&)\b", ",", s, flags=re.I)
    parts = [norm_country(p) for p in re.split(r",|;|/", s) if clean(p)]
    # remove common non-country words
    bad = {"the", "a", "an", "consultations", "dispute", "complaint"}
    out = []
    for p in parts:
        if p and p.lower() not in bad and p not in out:
            out.append(p)
    return out


def title_from_soup(soup: BeautifulSoup, ds: str) -> str:
    candidates = []
    for tag in ["h1", "h2", "title"]:
        for x in soup.find_all(tag):
            t = clean(x.get_text(" "))
            if t:
                candidates.append(t)
    for t in candidates:
        t = re.sub(r"^WTO\s*\|\s*", "", t, flags=re.I)
        t = re.sub(r"^Dispute settlement\s*[-–:]\s*", "", t, flags=re.I)
        t = re.sub(rf"^{ds}\s*[:\-–]\s*", "", t, flags=re.I)
        if len(t) > 8 and "dispute settlement" not in t.lower():
            return t
    return ds


def extract_labeled(text: str, labels: List[str]) -> str:
    for label in labels:
        # captures a short value after the label up to the next known label/newline-like phrase
        pat = re.compile(label + r"\s*:?\s*(.{1,300}?)(?=\s+(?:Respondent|Complainant|Third parties|Third party|Subject|Agreements|Status|Current status|Date|Request for consultations)\b|$)", re.I)
        m = pat.search(text)
        if m:
            return clean(m.group(1))
    return ""


def extract_from_tables(soup: BeautifulSoup) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        if len(cells) >= 2:
            key = cells[0].strip(":").lower()
            val = cells[1]
            if key and val:
                out[key] = val
    return out


def extract_doc_links(soup: BeautifulSoup, base_url: str) -> Dict[str, str]:
    links = {"wto_url": base_url, "docs_url": "", "consultations_url": "", "panel_report_url": "", "ab_report_url": ""}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        text = clean(a.get_text(" ")).lower()
        hlow = href.lower()
        if "docs.wto.org" in hlow or "/dol2fe/" in hlow:
            if not links["docs_url"]:
                links["docs_url"] = href
            if "consult" in text or "request for consultations" in text:
                links["consultations_url"] = href
            elif "appellate" in text or "ab report" in text:
                links["ab_report_url"] = href
            elif "panel" in text and "report" in text:
                links["panel_report_url"] = href
    return links


def extract_agreements(text: str) -> List[str]:
    found = []
    up = text.upper()
    for code in AGREEMENT_CODES:
        token = code.upper()
        if token in up and code not in found:
            found.append(code)
    # Normalize SCM/ASCM
    if "SCM" in found and "ASCM" not in found:
        found.append("ASCM")
    return found


def infer_sector(text: str, agreements: List[str]) -> str:
    low = text.lower()
    if "services" in low or "gats" in [a.lower() for a in agreements]:
        return "Services"
    if "patent" in low or "copyright" in low or "trademark" in low or "trips" in [a.lower() for a in agreements]:
        return "Intellectual Property"
    if "steel" in low or "aluminium" in low or "aluminum" in low or "iron" in low:
        return "Metals & Mining"
    if "agricultur" in low or "sps" in [a.lower() for a in agreements] or "food" in low or "meat" in low:
        return "Agriculture & Food Safety"
    if "anti-dumping" in low or "dumping" in low:
        return "Anti-Dumping"
    if "subsid" in low or "countervailing" in low:
        return "Subsidies & Countervailing"
    if "safeguard" in low:
        return "Safeguards"
    if "tbt" in [a.lower() for a in agreements] or "technical regulation" in low or "standard" in low:
        return "Standards & TBT"
    return "Other"


def infer_measure_type(text: str) -> str:
    low = text.lower()
    if "anti-dumping" in low or "dumping" in low:
        return "Anti-Dumping Measure"
    if "countervailing" in low:
        return "Countervailing Duty"
    if "safeguard" in low:
        return "Safeguard Measure"
    if "subsid" in low:
        return "Subsidy Measure"
    if "technical regulation" in low or "standard" in low:
        return "TBT Measure"
    if "sanitary" in low or "phytosanitary" in low:
        return "SPS Measure"
    if "tariff" in low or "customs" in low or "duty" in low:
        return "Tariff/Customs Measure"
    if "patent" in low or "copyright" in low or "trademark" in low:
        return "IP Measure"
    return "Trade Measure"


def parse_case(n: int, html: str, url: str) -> Dict[str, Any]:
    ds = f"DS{n}"
    soup = BeautifulSoup(html, "html.parser")
    for x in soup(["script", "style", "noscript"]):
        x.decompose()
    text = clean(soup.get_text(" "))
    tables = extract_from_tables(soup)
    title = title_from_soup(soup, ds)

    # Prefer table labels when available, otherwise flexible text labels.
    table_lower = {k.lower(): v for k, v in tables.items()}
    complainant_raw = ""
    respondent_raw = ""
    third_raw = ""
    for k, v in table_lower.items():
        if "complainant" in k and not complainant_raw:
            complainant_raw = v
        elif "respondent" in k and not respondent_raw:
            respondent_raw = v
        elif "third" in k and not third_raw:
            third_raw = v
    complainant_raw = complainant_raw or extract_labeled(text, [r"Complainant\(s\)", r"Complainant", r"Complaint by"])
    respondent_raw = respondent_raw or extract_labeled(text, [r"Respondent\(s\)", r"Respondent"])
    third_raw = third_raw or extract_labeled(text, [r"Third parties", r"Third party"])

    # Narrative fallback: "X requested consultations with Y"
    if not complainant_raw or not respondent_raw:
        m = re.search(r"On\s+" + DATE_RE.pattern.replace('\\b','') + r"\s*,?\s*([^\.]{2,160}?)\s+requested consultations with\s+([^\.]{2,160}?)(?:\s+concerning|\s+regarding|\.|,)", text, flags=re.I)
        if m:
            complainant_raw = complainant_raw or m.group(2) if False else complainant_raw
    # Better fallback regex without nested date group confusion
    if not complainant_raw or not respondent_raw:
        m = re.search(r"On\s+\d{1,2}\s+(?:" + MONTHS + r")\s+\d{4},\s+([^\.]{2,160}?)\s+requested consultations with\s+([^\.]{2,160}?)(?:\s+concerning|\s+regarding|\.|,)", text, flags=re.I)
        if m:
            complainant_raw = complainant_raw or m.group(1)
            respondent_raw = respondent_raw or m.group(2)

    complainants = split_parties(complainant_raw)
    respondent = norm_country(respondent_raw.split(";")[0].split(",")[0]) if respondent_raw else ""
    third_parties = split_parties(third_raw)

    dates = DATE_RE.findall(text)
    consultation_date = dates[0] if dates else ""
    year_m = YEAR_RE.search(consultation_date or text)
    year = int(year_m.group(1)) if year_m else None

    agreements = extract_agreements(text)
    codes = []
    for a in agreements:
        code = {"Anti-Dumping":"ADA", "Safeguards":"SA", "Agriculture":"AA"}.get(a, a)
        if code not in codes:
            codes.append(code)

    status = "Unknown"
    low = text.lower()
    if "mutually agreed solution" in low:
        status = "Mutually agreed solution"
    elif "panel report" in low and "adopted" in low:
        status = "Panel/Appellate report adopted"
    elif "panel established" in low:
        status = "Panel established"
    elif "requested consultations" in low:
        status = "Consultations requested"

    links = extract_doc_links(soup, url)
    subject = ""
    # Use first paragraph that has concerning/regarding and is not too long
    for p in soup.find_all(["p", "li"]):
        pt = clean(p.get_text(" "))
        if 40 < len(pt) < 600 and ("concerning" in pt.lower() or "regarding" in pt.lower() or "measures" in pt.lower()):
            subject = pt
            break

    case = {
        "ds_number": ds,
        "title": title,
        "short_title": title,
        "complainant": ", ".join(complainants),
        "complainant_list": complainants,
        "respondent": respondent,
        "third_parties": third_parties,
        "agreements": agreements,
        "agreement_codes": codes,
        "agreement_names": {},
        "subject": subject,
        "product": "",
        "sector": infer_sector(text + " " + title, agreements),
        "year": year,
        "stage": status,
        "status": status,
        "timeline": {"consultations": consultation_date} if consultation_date else {},
        "summary_en": subject,
        "summary_ar": "",
        "saudi_relevance": "HIGH" if ("Saudi Arabia" in complainants or respondent == "Saudi Arabia" or "Saudi Arabia" in third_parties) else "LOW",
        "saudi_impact": "Saudi Arabia is involved in this dispute." if ("Saudi Arabia" in complainants or respondent == "Saudi Arabia" or "Saudi Arabia" in third_parties) else "",
        "request_date": consultation_date or (str(year) if year else ""),
        "keywords": [x.lower() for x in codes] + [ds.lower()],
        "source": "WTO Official DS case page",
        "measure_type": infer_measure_type(text + " " + title),
        "article_codes": [],
        "hs_range": "",
        **links,
        "parties_count": len(complainants) + (1 if respondent else 0) + len(third_parties),
        "multi_complainant": len(complainants) > 1,
        "has_third_parties": bool(third_parties),
        "has_compliance": "21.5" in text or "compliance" in low,
        "has_ab": "appellate body" in low,
    }
    return case


def fetch_case(session: requests.Session, n: int) -> Optional[Dict[str, Any]]:
    url = BASE.format(n=n)
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception:
        return None
    if r.status_code != 200 or "Dispute settlement" not in r.text[:20000] and f"DS{n}" not in r.text[:20000]:
        return None
    if "not found" in r.text[:3000].lower() or len(r.text) < 1000:
        return None
    try:
        return parse_case(n, r.text, url)
    except Exception as exc:
        return {
            "ds_number": f"DS{n}", "title": f"DS{n}", "short_title": f"DS{n}",
            "complainant": "", "complainant_list": [], "respondent": "", "third_parties": [],
            "agreements": [], "agreement_codes": [], "agreement_names": {}, "subject": "", "product": "",
            "sector": "Other", "year": None, "stage": "Unknown", "status": "Unknown", "timeline": {},
            "summary_en": "", "summary_ar": "", "saudi_relevance": "LOW", "saudi_impact": "",
            "request_date": "", "keywords": [f"ds{n}"], "source": "WTO Official DS case page",
            "measure_type": "Trade Measure", "article_codes": [], "hs_range": "",
            "wto_url": url, "docs_url": "", "parties_count": 0, "multi_complainant": False,
            "has_third_parties": False, "has_compliance": False, "has_ab": False,
            "parse_error": str(exc),
        }


def build(max_ds: int, delay: float) -> Dict[str, Any]:
    session = requests.Session()
    cases: List[Dict[str, Any]] = []
    for n in range(1, max_ds + 1):
        c = fetch_case(session, n)
        if c:
            cases.append(c)
        if delay:
            time.sleep(delay)
    return {
        "metadata": {
            "source": "WTO official DS case pages",
            "base_url": "https://www.wto.org/english/tratop_e/dispu_e/cases_e/ds{n}_e.htm",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_ds_checked": max_ds,
            "case_count": len(cases),
            "legal_accuracy_note": "Fields are extracted only from official WTO pages. Unavailable fields are left blank; document URLs are not guessed.",
        },
        "cases": cases,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-ds", type=int, default=700)
    ap.add_argument("--out", default="data/wto_disputes_official.json")
    ap.add_argument("--delay", type=float, default=0.05)
    args = ap.parse_args()
    payload = build(args.max_ds, args.delay)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(payload['cases'])} WTO official cases to {out}")
    if len(payload["cases"]) < 500:
        raise SystemExit(f"ERROR: only {len(payload['cases'])} cases imported. Check Render network or WTO availability.")

if __name__ == "__main__":
    main()
