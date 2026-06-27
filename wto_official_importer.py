"""
Official WTO Dispute Cases Importer for Trade Cases
===================================================
Purpose: replace generated/static summaries with official WTO dispute-settlement case pages.
Primary listing source requested by the owner:
  https://www.wto.org/english/tratop_e/dispu_e/find_dispu_cases_e.htm
Official case source pattern:
  https://www.wto.org/english/tratop_e/dispu_e/cases_e/ds{n}_e.htm

Usage on Render / GitHub deploy:
  python scripts/wto_official_importer.py --all --out data/wto_disputes_official.json

Important data rule:
  This importer does NOT fabricate document links. It stores each case's official WTO page
  as the legal source, and stores document links only when exposed on the official page.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.wto.org"
FIND_CASES_URL = f"{BASE}/english/tratop_e/dispu_e/find_dispu_cases_e.htm"
STATUS_URL = f"{BASE}/english/tratop_e/dispu_e/dispu_status_e.htm"
CASE_URL = f"{BASE}/english/tratop_e/dispu_e/cases_e/ds{{ds}}_e.htm"

HEADERS = {
    "User-Agent": "TradeCasesOfficialImporter/2.0 (official WTO case-page validation)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COUNTRY_ALIASES = {
    "European Communities": "European Union",
    "EC": "European Union",
    "EU": "European Union",
    "Turkey": "Türkiye",
    "Republic of Türkiye": "Türkiye",
    "United States of America": "United States",
    "U.S.": "United States",
    "US": "United States",
    "USA": "United States",
    "Korea, Republic of": "Korea",
    "Republic of Korea": "Korea",
    "Kingdom of Saudi Arabia": "Saudi Arabia",
    "KSA": "Saudi Arabia",
}

AGREEMENT_CODES = [
    "GATT", "GATS", "TRIPS", "SPS", "TBT", "SCM", "ASCM", "ADA", "ADP",
    "Safeguards", "Agreement on Safeguards", "Agriculture", "DSU", "TRIMs",
    "CVA", "ATC", "Import Licensing", "Rules of Origin", "Customs Valuation",
]

FIELD_STOPWORDS = [
    "Complainant", "Complainants", "Respondent", "Respondents", "Third parties", "Third Parties",
    "Request for consultations", "Panel established", "Panel report", "Appellate Body report",
    "Arbitration", "Status", "Current status", "Subject", "Agreements cited", "Documents",
]


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def get(url: str, *, timeout: int = 35) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    return r.text


def split_parties(value: str) -> List[str]:
    value = clean_text(value)
    if not value:
        return []
    # Remove accidental trailing labels from a greedy extraction.
    for stop in FIELD_STOPWORDS:
        value = re.split(rf"\b{re.escape(stop)}\b\s*:?,?", value, flags=re.I)[0].strip()
    parts = re.split(r"\s*;\s*|\s*,\s*(?=[A-Z])|\s+and\s+(?=[A-Z])", value)
    out: List[str] = []
    for p in parts:
        p = clean_text(p).strip(" .,:;()[]")
        if not p or len(p) <= 1:
            continue
        out.append(COUNTRY_ALIASES.get(p, p))
    return list(dict.fromkeys(out))


def find_case_numbers_from_html(html: str) -> List[int]:
    nums = set(int(x) for x in re.findall(r"cases_e/ds(\d+)_e\.htm", html, flags=re.I))
    nums.update(int(x) for x in re.findall(r"\bDS\s*(\d{1,4})\b", html, flags=re.I))
    return sorted(n for n in nums if n > 0)


def discover_case_numbers(max_ds: int = 900) -> List[int]:
    """Discover all DS numbers from WTO official listing pages, with sequential fallback."""
    found: set[int] = set()
    for url in (FIND_CASES_URL, STATUS_URL):
        try:
            found.update(find_case_numbers_from_html(get(url)))
        except Exception as exc:  # keep deploy resilient
            print(f"[warn] listing discovery failed for {url}: {exc}")
    if found:
        return sorted(found, reverse=True)
    # Fallback: check sequentially from max_ds down. Non-existing pages will be skipped.
    return list(range(max_ds, 0, -1))


def extract_field_by_label(soup: BeautifulSoup, labels: List[str]) -> str:
    # Table/list based extraction.
    for label in labels:
        pattern = rf"^\s*{re.escape(label)}\s*:?"
        for node in soup.find_all(string=re.compile(pattern, flags=re.I)):
            parent = node.parent
            if not parent:
                continue
            if parent.name in {"td", "th", "dt", "strong", "b", "span", "p"}:
                # Prefer next sibling cell/block.
                for sib in [parent.find_next_sibling("td"), parent.find_next_sibling("dd"), parent.find_next_sibling("p")]:
                    if sib:
                        txt = clean_text(sib.get_text(" "))
                        if txt:
                            return txt
                # Otherwise take same text with label removed.
                txt = clean_text(parent.get_text(" "))
                txt = re.sub(pattern, "", txt, flags=re.I).strip(" :-")
                if txt:
                    return txt
    # Plain text fallback bounded by next known label.
    text = soup.get_text("\n")
    for label in labels:
        stop = "|".join(re.escape(x) for x in FIELD_STOPWORDS)
        m = re.search(rf"{re.escape(label)}\s*:?\s*(.+?)(?=\n(?:{stop})\s*:?|$)", text, flags=re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""

def extract_title(ds: int, soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["h1", "h2", "h3"]):
        txt = clean_text(tag.get_text(" "))
        if txt and not txt.lower().startswith("wto") and "dispute settlement" not in txt.lower():
            # Avoid generic section headers.
            if len(txt) > 3 and not re.match(r"^(Current status|Summary|Documents)$", txt, re.I):
                return txt
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    title = re.sub(r"^WTO\s*\|\s*dispute settlement\s*-\s*", "", title, flags=re.I)
    title = re.sub(r"^the disputes\s*-\s*", "", title, flags=re.I)
    return title or f"DS{ds}"


def extract_dates(text: str) -> Dict[str, str]:
    patterns = {
        "consultations_request": r"Request for consultations\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        "panel_established": r"Panel established\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        "panel_report": r"Panel report\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        "ab_report": r"Appellate Body report\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        "adoption": r"Adoption\s*:?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
    }
    out: Dict[str, str] = {}
    for k, pat in patterns.items():
        m = re.search(pat, text, flags=re.I)
        if m:
            out[k] = clean_text(m.group(1))
    return out


def extract_documents(soup: BeautifulSoup) -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        url = urljoin(BASE, href)
        label = clean_text(a.get_text(" ")) or "WTO document"
        if "docs.wto.org" not in url and "directdoc" not in url and not re.search(r"/WT/DS|WT/DS\d+", url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        docs.append({"label": label, "url": url})
    return docs


def infer_agreements(text: str) -> List[str]:
    t = text.upper()
    found: List[str] = []
    for code in AGREEMENT_CODES:
        c = code.upper()
        if c in t:
            normalized = "SCM" if code == "ASCM" else ("ADA" if code == "ADP" else code)
            if normalized not in found:
                found.append(normalized)
    return found


def parse_case(ds: int) -> Optional[Dict[str, Any]]:
    url = CASE_URL.format(ds=ds)
    try:
        html = get(url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {403, 404, 410}:
            return None
        raise
    if "Page not found" in html or "404" in html[:500]:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" "))

    title = extract_title(ds, soup)
    complainants = split_parties(extract_field_by_label(soup, ["Complainant", "Complainants", "Complaint by"]))
    respondents = split_parties(extract_field_by_label(soup, ["Respondent", "Respondents"]))
    third = split_parties(extract_field_by_label(soup, ["Third parties", "Third party", "Third Parties"]))
    current_status = extract_field_by_label(soup, ["Current status", "Status"]) or "See official WTO page"
    subject = extract_field_by_label(soup, ["Measures at issue", "Subject", "Summary"])
    # Keep summary conservative. Do not create legal findings by AI.
    if not subject:
        subject = "See official WTO page for full official case details."

    timeline = extract_dates(text)
    year = None
    for date_value in timeline.values():
        m = re.search(r"\b(19|20)\d{2}\b", date_value)
        if m:
            year = int(m.group(0)); break
    if year is None:
        m = re.search(r"\b(19|20)\d{2}\b", text)
        year = int(m.group(0)) if m else None
    agreements = infer_agreements(text)
    docs = extract_documents(soup)

    return {
        "ds_number": f"DS{ds}",
        "title": title,
        "short_title": re.sub(rf"^DS\s*{ds}\s*:?\s*", "", title, flags=re.I).strip(),
        "complainant": ", ".join(complainants),
        "complainant_list": complainants,
        "respondent": respondents[0] if len(respondents) == 1 else ", ".join(respondents),
        "respondent_list": respondents,
        "third_parties": third,
        "agreements": agreements,
        "agreement_codes": agreements,
        "agreement_names": {},
        "article_codes": [],
        "subject": subject,
        "product": "",
        "sector": "Unclassified",
        "measure_type": "Unclassified",
        "year": year,
        "request_date": str(year or ""),
        "stage": "Official WTO case page",
        "status": current_status,
        "timeline": timeline,
        "summary_en": "",
        "summary_ar": "",
        "saudi_relevance": "UNASSESSED",
        "saudi_impact": "يحتاج تقييم قانوني بناءً على مصالح المملكة في القطاع المعني",
        "keywords": agreements,
        "source": "WTO official dispute settlement case page",
        "source_listing": FIND_CASES_URL,
        "wto_url": url,
        "official_case_url": url,
        "docs_url": docs[0]["url"] if docs else "",
        "official_documents": docs,
        "has_ab": bool(timeline.get("ab_report")) or "Appellate Body" in text,
        "has_compliance": bool(re.search(r"Article\s+21\.5|compliance", text, re.I)),
        "multi_complainant": len(complainants) > 1,
        "has_third_parties": bool(third),
        "parties_count": len(complainants) + len(respondents) + len(third),
        "data_quality": {
            "official_source": True,
            "document_links_not_fabricated": True,
            "classification_source": "Unclassified unless explicitly inferred from official text",
            "parser_note": "Each case is imported from the official WTO case page. Empty fields mean the importer did not verify them from source text."
        },
        "last_imported_utc": datetime.now(timezone.utc).isoformat(),
    }


def import_cases(*, limit: Optional[int], max_ds: int, delay: float) -> List[Dict[str, Any]]:
    numbers = discover_case_numbers(max_ds=max_ds)
    cases: List[Dict[str, Any]] = []
    misses_after_found = 0
    for i, ds in enumerate(numbers, 1):
        try:
            case = parse_case(ds)
            if case:
                cases.append(case)
                misses_after_found = 0
                print(f"[{i}/{len(numbers)}] imported DS{ds}")
            else:
                misses_after_found += 1
        except Exception as exc:
            misses_after_found += 1
            print(f"[warn] failed DS{ds}: {exc}")
        if limit and len(cases) >= limit:
            break
        # If sequential fallback is being used and there are too many misses after we've found cases,
        # stop to avoid wasting deploy minutes.
        if not numbers and misses_after_found > 120 and cases:
            break
        if delay:
            time.sleep(delay)
    # Sort ascending by DS number for stable UI.
    cases.sort(key=lambda d: int(str(d.get("ds_number", "DS0")).replace("DS", "") or 0))
    return cases


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Limit number of cases; omit with --all for all discovered cases")
    ap.add_argument("--all", action="store_true", help="Import all discovered WTO cases")
    ap.add_argument("--max-ds", type=int, default=900, help="Upper bound for sequential DS fallback")
    ap.add_argument("--out", default="data/wto_disputes_official.json")
    ap.add_argument("--delay", type=float, default=0.08)
    args = ap.parse_args()
    limit = None if args.all else (args.limit or 500)
    cases = import_cases(limit=limit, max_ds=args.max_ds, delay=args.delay)
    payload = {
        "metadata": {
            "source": "WTO official dispute settlement case pages",
            "source_url": FIND_CASES_URL,
            "status_url": STATUS_URL,
            "limit": limit,
            "all_requested": bool(args.all),
            "imported_count": len(cases),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "legal_note": "Official case page is authoritative. Document links are stored only when found on official WTO pages; not generated by assumption.",
        },
        "cases": cases,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(cases)} official WTO cases to {out}")


if __name__ == "__main__":
    main()