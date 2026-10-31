import os, re, requests, csv, argparse, platform
import secrets, string
from typing import Dict, Iterator, List, Optional, Tuple, Union
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

#configuration

BASE_URL = os.getenv("FHIR_BASE", "").strip()
API_KEY = os.getenv("FHIR_API_KEY", "").strip()
DEFAULT_TIMEOUT = 30
DEFAULT_COUNT = 200

CSV_WITH_IDENT_NAME = "feasibility_study_data_with_identifiers.csv"
CSV_ANON_NAME       = "feasibility_study_data_anonymized.csv"

#headers and urls

def build_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h: Dict[str, str] = {
        "Accept": "application/fhir+json",
        "User-Agent": "smart-change-fhir-client/1.0",
    }

    token = API_KEY.strip()
    if token and token.lower() != "none":
        #pass the token without bearer prefix
        
        token = re.sub(r"^Bearer\s+", "", token, flags=re.IGNORECASE).strip()
        h["Authorization"] = f"Bearer {token}"

    if extra:
        h.update(extra)
    return h

HEADERS = build_headers()

def normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"/metadata$", "", u, flags=re.IGNORECASE)
    return u

SERVICE_ROOT = normalize_base(BASE_URL)

def url(path: str) -> str:
    return SERVICE_ROOT.rstrip("/") + "/" + path.lstrip("/")

#requests

def http_get(u: str, params: Optional[Dict[str, Union[str, int]]] = None, **kw) -> requests.Response:
    timeout = kw.pop("timeout", DEFAULT_TIMEOUT)
    r = requests.get(u, headers=HEADERS, params=params, timeout=timeout, **kw)
    r.raise_for_status()
    return r

def search(resource_type: str, params: Optional[Dict[str, Union[str, int]]] = None) -> Iterator[Dict]:
    u = url(f"/{resource_type}")
    p = dict(params or {})
    if "_count" not in p:
        p["_count"] = DEFAULT_COUNT
    while True:
        r = http_get(u, params=p)
        b = r.json()
        yield b
        next_link = next((ln.get("url") for ln in b.get("link", []) if ln.get("relation") == "next"), None)
        if not next_link:
            break
        u, p = next_link, {}

def search_by_subject(resource_type: str, patient_id: str, extra: Optional[Dict[str, Union[str,int]]] = None) -> Iterator[Dict]:
    p = dict(extra or {})
    p["subject"] = f"Patient/{patient_id}"
    return search(resource_type, p)

#resource counts

def get_capability() -> Dict:
    meta_url = url("/metadata")
    print("\nService root:", SERVICE_ROOT)
    print("\nLoading capability statement...")
    print("[HTTP GET]", meta_url)
    r = http_get(meta_url)
    return r.json()

def get_count_hapi(resource_type: str, hard_limit: int = 2_000_000):
    try:
        total = 0
        next_url = url(f"/{resource_type}")
        params = {"_count": 200, "_elements": "id"}
        while next_url:
            r = requests.get(next_url, headers=HEADERS, params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            b = r.json()
            total += len(b.get("entry", []) or [])
            if total >= hard_limit:
                return f">{hard_limit}"
            next_url = next((ln.get("url") for ln in b.get("link", []) if ln.get("relation") == "next"), None)
            params = {}
        return total
    except Exception:
        return "unknown"

#helpers

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _parse_birthdate(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None

def measurement_date_key(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.date().isoformat()

def fmt2(v: Union[int, float, str, None]) -> str:
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        s = f"{f:.2f}".rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(v)

def age_years(at: Optional[datetime], dob: Optional[date]) -> str:
    if not at or not dob:
        return ""
    days = (at.date() - dob).days
    if days < 0:
        return ""
    return fmt2(days / 365.2425)

def set_cell(row: Dict[str, Union[str, float, int, bool, None]], col: str, value: Union[str, float, int, bool, None]) -> int:
    if col is None:
        return 0
    before = row.get(col, None)
    row[col] = value
    return 1 if before != value else 0

def ensure_row(store: Dict[Tuple[str, str], Dict[str, Union[str, float, int, bool, None]]],
               pid: str, date_key: str,
               base: Dict[str, str]) -> Dict[str, Union[str, float, int, bool, None]]:
    key = (pid, date_key or "")

    if key not in store:
        row: Dict[str, Union[str, float, int, bool, None]] = {c: "" for c in CSV_COLS_WITH_IDENT}
        row.update({
            "ORIGIN": base.get("ORIGIN",""),
            "GROUP": base.get("GROUP", ""),
            "ID-ORIGINAL": base.get("ID",""),
            "ID": base.get("ID",""),
            "NAME": base.get("NAME",""),
            "EMAIL": base.get("EMAIL",""),
            "SEX": base.get("SEX",""),
            "DOB": base.get("DOB",""),
            "MEASUREMENT DATE": date_key or "",
            "AGE": base.get("AGE", ""),
        })
        store[key] = row
    else:
        for k in ("ORIGIN","GROUP","ID-ORIGINAL","ID","NAME","EMAIL","SEX","DOB","MEASUREMENT DATE","AGE"):
            if not store[key].get(k):
                store[key][k] = base.get(k,"")

    return store[key]

def _name_text(p: Dict) -> str:
    nm = (p.get("name") or [{}])[0] or {}
    given = " ".join(nm.get("given") or [])
    fam = nm.get("family") or ""
    txt = (nm.get("text") or "").strip()
    s = txt or " ".join([given, fam]).strip()
    return s or ""

def _email_from_patient(p: Dict) -> str:
    for t in p.get("telecom", []) or []:
        if (t.get("system") or "").lower() == "email" and t.get("value"):
            return str(t["value"])
    for iden in p.get("identifier", []) or []:
        v = iden.get("value") or ""
        if "@" in v:
            return v
    return ""

def coding_display(concept: Optional[Dict]) -> str:
    if not concept:
        return ""

    for c in concept.get("coding", []) or []:
        if c.get("display"):
            return str(c.get("display"))

    return str(concept.get("text") or "")

def qualitative_risk_display(qrisk: Optional[Dict]) -> str:
    #risk level text
    return coding_display(qrisk)

#codes

NI_CODE_TO_COL = {
    "72511004": "FRUITS",
    "22836000": "VEGETABLES",
    "13577000": "NUTS",
    "818989004": "SWEETENED BEVERAGES",
    "28647000": "MEATS",
    "23182003": "CEREALS",
}

SMOKING_CODE_TO_TEXT = {
    "8392000": "Non-smoker",
    "8517006": "Ex-smoker",
    "77176002": "Smoker",
    "373066001": "Declined to answer",
}

OBS_CODE_TO_COL = {
    "29463-7": "WEIGHT",
    "8302-2": "HEIGHT",
    "39156-5": "BMI",
    "276361009": "WAIST CIRC",
    "444981005": "RESTING HEART RATE",
    "14647-2": "CHOLESTEROL",
    "14646-4": "HDL",
    "22748-8": "LDL",
    "76629-5": "FASTING GLUCOSE",
    "77592-4": "MODERATE ACT",
    "77593-2": "VIGOROUS ACT",
    "41950-7": "STEPS",
    "41981-2": "TOTAL CALORIES PER DAY",
    "93819-1": "ACTIVE CALORIES PER DAY",
    "93832-4": "SLEEP",
    "35094-2": None,
}

BP_COMPONENTS = {
    "8480-6": "SYSTOLIC BP",
    "8462-4": "DIASTOLIC BP",
}

#csv columns

CSV_COLS_WITH_IDENT = [
    "ORIGIN", "GROUP", "ID-ORIGINAL", "ID", "NAME", "EMAIL", "SEX", "DOB", "MEASUREMENT DATE", "AGE",
    "HEIGHT", "WEIGHT", "BMI", "WAIST CIRC", "HIGH GLUCOSE", "FASTING GLUCOSE",
    "CHOLESTEROL", "HDL", "LDL", "SYSTOLIC BP", "DIASTOLIC BP", "RESTING HEART RATE",
    "MODERATE ACT", "VIGOROUS ACT", "SHUTTLE RUN DATE", "SHUTTLE RUN SPEED", "SHUTTLE RUN LAPS", 
    "FITNESS PERCEPTION", "NON TRACKED ACTIVITY", "NON TRACKED TIME", "STEPS", "SMOKING", "ALCOHOL",
    "VEGETABLES", "FRUITS", "SWEETENED BEVERAGES", "MEATS", "CEREALS", "NUTS", "SLEEP",
    "DIABETES", "DIABETES TYPE", "CVD", "FAMILY DIABETES", "FAMILY CVD", "DIABETES MEMBERS", "CVD MEMBERS", "BLOOD PRESSURE MEDICATION",
    "TOTAL CALORIES PER DAY", "ACTIVE CALORIES PER DAY",
    "FAMILY CARS", "BEDROOM", "FAMILY VACATIONS", "FAMILY COMPUTERS", "BATHROOMS", "DISHWASHER", "FAS SCORE", "SCREEN HOURS",
    "RISK ASSESSMENT OUTCOME", "RISK ASSESSMENT PERCENT", "RISK ASSESSMENT RANGE LOW", 
    "RISK ASSESSMENT RANGE HIGH", "RISK ASSESSMENT LEVEL",
]

CSV_COLS_ANON = [
    "ORIGIN", "GROUP", "ID", "SEX", "AGE", "CONSECUTIVE DAY",
    "HEIGHT", "WEIGHT", "WAIST CIRC", "HIGH GLUCOSE", "FASTING GLUCOSE",
    "CHOLESTEROL", "HDL", "LDL", "SYSTOLIC BP", "DIASTOLIC BP", "RESTING HEART RATE",
    "MODERATE ACT", "VIGOROUS ACT", "SHUTTLE RUN SPEED", "SHUTTLE RUN LAPS", 
    "FITNESS PERCEPTION", "NON TRACKED ACTIVITY", "NON TRACKED TIME", "STEPS", "SMOKING", "ALCOHOL",
    "VEGETABLES", "FRUITS", "SWEETENED BEVERAGES", "MEATS", "CEREALS", "NUTS", "SLEEP",
    "DIABETES", "DIABETES TYPE", "CVD", "FAMILY DIABETES", "FAMILY CVD", "BLOOD PRESSURE MEDICATION",
    "TOTAL CALORIES PER DAY", "ACTIVE CALORIES PER DAY",
    "FAMILY CARS", "BEDROOM", "FAMILY VACATIONS", "FAMILY COMPUTERS", "BATHROOMS", "DISHWASHER", "FAS SCORE", "SCREEN HOURS",    "DIABETES FIRST DEGREE", "DIABETES SECOND DEGREE", "CVD FIRST DEGREE", "CVD SECOND DEGREE",
    "RISK ASSESSMENT OUTCOME", "RISK ASSESSMENT PERCENT", "RISK ASSESSMENT RANGE LOW", 
    "RISK ASSESSMENT RANGE HIGH", "RISK ASSESSMENT LEVEL",
]

#data extraction

def load_patients(patient_ids: Optional[set] = None) -> Dict[str, Dict[str, str]]:
    idx: Dict[str, Dict[str, str]] = {}
    for bundle in search("Patient", params={"_elements": "id,name,telecom,gender,birthDate,address,identifier"}):
        for ent in bundle.get("entry", []) or []:
            p = ent.get("resource") or {}
            pid = p.get("id")
            
            if not pid:
                continue

            if patient_ids is not None and pid not in patient_ids:
                continue

            origin = ""
            addr = p.get("address") or []
            if isinstance(addr, list) and addr:
                origin = (addr[0] or {}).get("country", "") or ""
            idx[pid] = {
                "ID": pid,
                "NAME": _name_text(p),
                "EMAIL": _email_from_patient(p),
                "SEX": p.get("gender") or "",
                "DOB": p.get("birthDate") or "",
                "ORIGIN": origin,
            }
    return idx

def normalize_group_value(v):
    if not v:
        return ""
    s = str(v).strip().lower()
    if s == "control":
        return "Control"
    if s == "intervention":
        return "Intervention"
    return str(v).strip()

def load_research_subject_groups(patient_ids: Optional[set] = None) -> Dict[str, str]:
    groups = {}

    for bundle in search(
        "ResearchSubject",
        {
            "_elements": "id,subject,assignedComparisonGroup,status",
            "_count": 200,
        },
    ):
        for ent in bundle.get("entry", []) or []:
            rs = ent.get("resource") or {}

            pid = (
                (rs.get("subject") or {})
                .get("reference", "")
                .replace("Patient/", "")
            )

            if not pid:
                continue

            if patient_ids is not None and pid not in patient_ids:
                continue

            group = normalize_group_value(rs.get("assignedComparisonGroup"))

            if group:
                groups[pid] = group

    print(f"ResearchSubject group assignments found: {len(groups)}")
    return groups

def _base_for(pid: str, pat_idx: Dict[str, Dict[str, str]], at_dt: Optional[datetime]) -> Dict[str, str]:
    meta = pat_idx.get(pid, {})
    return {
        "ORIGIN": meta.get("ORIGIN", ""),
        "GROUP": meta.get("GROUP", ""),
        "ID": pid,
        "NAME": meta.get("NAME", ""),
        "EMAIL": meta.get("EMAIL", ""),
        "SEX": meta.get("SEX", ""),
        "DOB": meta.get("DOB", ""),
        "AGE": age_years(at_dt, _parse_birthdate(meta.get("DOB"))),
    }

#nutrition intake
def collect_nutrition_intake_rows_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0
    
    for bundle in search_by_subject("NutritionIntake", pid, {"_sort": "date,_id"}):
        for ent in bundle.get("entry", []) or []:
            ni = ent.get("resource") or {}
            if ni.get("resourceType") != "NutritionIntake":
                continue
            occ = _parse_dt(ni.get("occurrenceDateTime"))
            base = _base_for(pid, pat_idx, occ)
            
            row = ensure_row(rows_store, pid, measurement_date_key(occ), base)
            for item in ni.get("consumedItem") or []:
                coding = (((item.get("nutritionProduct") or {}).get("concept") or {}).get("coding") or [])
                amt = (item.get("amount") or {})
                val = amt.get("value", None)
                try:
                    val = float(val) if val is not None else None
                except Exception:
                    val = None
                if val is None:
                    continue
                for c in coding:
                    if c.get("system") == "http://snomed.info/sct":
                        col = NI_CODE_TO_COL.get(str(c.get("code", "")))
                        if col:
                            cells += set_cell(row, col, val)
                            break
    return cells

#questionnaires

def _answers_to_display_list(ans_list) -> str:
    out: List[str] = []
    for a in ans_list or []:
        vc = a.get("valueCoding")
        if vc and isinstance(vc, dict):
            disp = vc.get("display")
            if disp:
                out.append(str(disp))
    return ", ".join(out)

def collect_qr_health_history_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0
    QURL = "http://it4h.eng.it/uhm/Questionnaire/prevention-patient-health-history"
    
    for bundle in search_by_subject("QuestionnaireResponse", pid, {"questionnaire": QURL,"_sort": "authored,_id"}):
        for ent in bundle.get("entry", []) or []:
            qr = ent.get("resource") or {}
            if qr.get("resourceType") != "QuestionnaireResponse":
                continue
            authored = _parse_dt(qr.get("authored"))
            base = _base_for(pid, pat_idx, authored)
            
            row = ensure_row(rows_store, pid, measurement_date_key(authored), base)
            for it in qr.get("item") or []:
                lid = it.get("linkId")
                ans = it.get("answer") or [{}]
                if lid == "diagnosed_diabetes":
                    cells += set_cell(row, "DIABETES", ans[0].get("valueBoolean"))
                elif lid == "diagnosed_diabetes_type":
                    vc = ans[0].get("valueCoding") or {}
                    cells += set_cell(row, "DIABETES TYPE", vc.get("display", "") or "")
                elif lid == "latent_diabetes":
                    cells += set_cell(row, "HIGH GLUCOSE", ans[0].get("valueBoolean"))
                elif lid == "diagnosed_cvd":
                    cells += set_cell(row, "CVD", ans[0].get("valueBoolean"))
                elif lid == "hypertension_drugs":
                    cells += set_cell(row, "BLOOD PRESSURE MEDICATION", ans[0].get("valueBoolean"))
                elif lid == "diabetes_family_history":
                    cells += set_cell(row, "FAMILY DIABETES", ans[0].get("valueBoolean"))
                elif lid == "cvd_family_history":
                    cells += set_cell(row, "FAMILY CVD", ans[0].get("valueBoolean"))
                elif lid == "diabetes_family_history_members":
                    cells += set_cell(row, "DIABETES MEMBERS", _answers_to_display_list(ans))
                elif lid == "cvd_family_history_members":
                    cells += set_cell(row, "CVD MEMBERS", _answers_to_display_list(ans))
    return cells

def collect_qr_lifestyle_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0
    QURL = "http://it4h.eng.it/uhm/Questionnaire/prevention-patient-lifestyle"
    
    for bundle in search_by_subject("QuestionnaireResponse", pid, {"questionnaire": QURL,"_sort": "authored,_id"}):

        for ent in bundle.get("entry", []) or []:
            qr = ent.get("resource") or {}
            if qr.get("resourceType") != "QuestionnaireResponse":
                continue
            authored = _parse_dt(qr.get("authored"))
            base = _base_for(pid, pat_idx, authored)
            
            row = ensure_row(rows_store, pid, measurement_date_key(authored), base)
            for it in qr.get("item") or []:
                lid = it.get("linkId")
                
                if lid == "pa-status":
                    for it2 in it.get("item") or []:
                        sub_id = it2.get("linkId")
                        ans = (it2.get("answer") or [{}])[0]

                        if sub_id == "20-m-srt-date":
                            cells += set_cell(row, "SHUTTLE RUN DATE", ans.get("valueDate"))

                        elif sub_id == "20-m-srt":
                            cells += set_cell(row, "SHUTTLE RUN SPEED", ans.get("valueDecimal"))

                        elif sub_id == "20-m-srt-laps":
                            cells += set_cell(row, "SHUTTLE RUN LAPS", ans.get("valueInteger"))

                        elif sub_id == "physical-fitness-perception":
                            vc = ans.get("valueCoding") or {}
                            cells += set_cell(row, "FITNESS PERCEPTION", vc.get("display", "") or "")
                        
                        elif sub_id == "non-tracked-activity":
                            cells += set_cell(row, "NON TRACKED ACTIVITY", ans.get("valueBoolean"))

                        elif sub_id == "non-tracked-time":
                            cells += set_cell(row, "NON TRACKED TIME", ans.get("valueInteger"))

                elif lid == "lifestyle":
                    for it2 in it.get("item") or []:
                        lid2 = it2.get("linkId")
                        ans = (it2.get("answer") or [{}])[0]
                        if lid2 == "smoking-status":
                            vc = ans.get("valueCoding") or {}
                            cells += set_cell(row, "SMOKING", SMOKING_CODE_TO_TEXT.get(str(vc.get("code","")), ""))
                        elif lid2 == "alcohol-consumption":
                            vc = ans.get("valueCoding") or {}
                            cells += set_cell(row, "ALCOHOL", (vc.get("display") or vc.get("code") or ""))
    return cells

def collect_qr_initial_risk_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0
    QURL = "http://it4h.eng.it/uhm/Questionnaire/initial-risk-questionnaire"
    
    for bundle in search_by_subject("QuestionnaireResponse", pid, {"questionnaire": QURL,"_sort": "authored,_id"}):

        for ent in bundle.get("entry", []) or []:
            qr = ent.get("resource") or {}
            if qr.get("resourceType") != "QuestionnaireResponse":
                continue
            authored = _parse_dt(qr.get("authored"))
            base = _base_for(pid, pat_idx, authored)
            
            row = ensure_row(rows_store, pid, measurement_date_key(authored), base)
            
            for it in qr.get("item") or []:
                lid = it.get("linkId")
                
                if lid == "lifestyle":
                    for it2 in it.get("item") or []:
                        if it2.get("linkId") == "screen-time-hours":
                            ans = (it2.get("answer") or [{}])[0]
                            cells += set_cell(row, "SCREEN HOURS", ans.get("valueInteger"))
                
                elif lid == "pa-status":
                    for it2 in it.get("item") or []:
                        sub_id = it2.get("linkId")
                        ans = (it2.get("answer") or [{}])[0]

                        if sub_id == "physical-fitness-perception":
                            vc = ans.get("valueCoding") or {}
                            cells += set_cell(row, "FITNESS PERCEPTION", vc.get("display", "") or "")

                        elif sub_id == "non-tracked-activity":
                            cells += set_cell(row, "NON TRACKED ACTIVITY", ans.get("valueBoolean"))

                        elif sub_id == "non-tracked-time":
                            cells += set_cell(row, "NON TRACKED TIME", ans.get("valueInteger"))



                elif lid == "family-ses":
                    for it2 in it.get("item") or []:
                        lid2 = it2.get("linkId")
                        ans = (it2.get("answer") or [{}])[0]
                        if lid2 == "family-vehicle-count":
                            cells += set_cell(row, "FAMILY CARS", ans.get("valueString","") or "")
                        elif lid2 == "family-has-own-bedroom":
                            cells += set_cell(row, "BEDROOM", ans.get("valueBoolean"))
                        elif lid2 == "family-computer-count":
                            cells += set_cell(row, "FAMILY COMPUTERS", ans.get("valueString","") or "")
                        elif lid2 == "family-bathroom-count":
                            cells += set_cell(row, "BATHROOMS", ans.get("valueString","") or "")
                        elif lid2 == "family-has-dishwasher":
                            cells += set_cell(row, "DISHWASHER", ans.get("valueBoolean"))
                        elif lid2 == "family-intl-travel-last-year-count":
                            cells += set_cell(row, "FAMILY VACATIONS", ans.get("valueString","") or "")
    return cells

def _range_low_high(rng):
    if not rng:
        return ("", "")
    low = ((rng.get("low") or {}).get("value", ""))
    high = ((rng.get("high") or {}).get("value", ""))
    return (low, high)

def collect_risk_assessments_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0

    for bundle in search_by_subject("RiskAssessment", pid, {"_sort": "date,_id"}):
        for ent in bundle.get("entry", []) or []:
            ra = ent.get("resource") or {}
            if ra.get("resourceType") != "RiskAssessment":
                continue

            occ = _parse_dt(ra.get("occurrenceDateTime"))
            base = _base_for(pid, pat_idx, occ)
            row = ensure_row(rows_store, pid, measurement_date_key(occ), base)

            for pred in ra.get("prediction") or []:
                outcome = coding_display(pred.get("outcome"))
                probability = pred.get("probabilityDecimal")
                level = qualitative_risk_display(pred.get("qualitativeRisk"))

                prob_low, prob_high = _range_low_high(pred.get("probabilityRange"))

                if outcome:
                    cells += set_cell(row, "RISK ASSESSMENT OUTCOME", outcome)

                if probability is not None:
                    cells += set_cell(row, "RISK ASSESSMENT PERCENT", float(probability) * 100)

                if prob_low != "":
                    cells += set_cell(row, "RISK ASSESSMENT RANGE LOW", prob_low)

                if prob_high != "":
                    cells += set_cell(row, "RISK ASSESSMENT RANGE HIGH", prob_high)

                if level:
                    cells += set_cell(row, "RISK ASSESSMENT LEVEL", level)

    return cells

#observations
def collect_observations_for_patient(pid: str, pat_idx, rows_store) -> int:
    cells = 0
    
    for bundle in search_by_subject("Observation", pid, {"_sort": "date,_id"}):
        for ent in bundle.get("entry", []) or []:
            ob = ent.get("resource") or {}
            if ob.get("resourceType") != "Observation":
                continue
            eff = _parse_dt(ob.get("effectiveDateTime"))
            base = _base_for(pid, pat_idx, eff)
            
            row = ensure_row(rows_store, pid, measurement_date_key(eff), base)

            primary_code = None
            for c in (ob.get("code") or {}).get("coding", []) or []:
                sys = c.get("system")
                code = str(c.get("code",""))
                if sys in ("http://loinc.org", "http://snomed.info/sct"):
                    primary_code = code
                    break
            if not primary_code:
                continue

            if primary_code == "35094-2":
                for comp in ob.get("component") or []:
                    codings = (comp.get("code") or {}).get("coding") or []
                    if not codings:
                        continue
                    comp_code = str((codings[0] or {}).get("code",""))
                    col = BP_COMPONENTS.get(comp_code)
                    if not col:
                        continue
                    vq = comp.get("valueQuantity") or {}
                    if "value" in vq:
                        cells += set_cell(row, col, vq.get("value"))
                continue

            col = OBS_CODE_TO_COL.get(primary_code)
            if not col:
                continue
            vq = ob.get("valueQuantity")
            if not vq or "value" not in vq:
                continue
            cells += set_cell(row, col, vq.get("value"))
    return cells

#anonym helpers

# public category labels; the original numeric coding is not included
ORIGIN_TO_NUM = {"SVN": "a", "PRT": "b", "NLD": "c", "FIN": "d", "TWN": "e"}
SEX_TO_NUM = {"female": "a", "male": "b", "other": "c", "unknown": "c"}

def fas_piece(v):
    if v in (None, ""):
        return 0
    if isinstance(v, bool):
        return 1 if v is True else 0
    s = str(v).strip()
    if s == "":
        return 0
    if s == "2+":
        return 2
    try:
        return int(float(s))
    except Exception:
        return 0

def calculate_fas_score(r):
    fields = [
        "FAMILY CARS",
        "BEDROOM",
        "FAMILY COMPUTERS",
        "BATHROOMS",
        "DISHWASHER",
        "FAMILY VACATIONS",
    ]

    has_any_value = any(r.get(field) not in (None, "") for field in fields)

    if not has_any_value:
        return ""

    return sum(fas_piece(r.get(field)) for field in fields)

def round_half_up_whole(x: Union[str, float, int, None]) -> str:
    if x in (None, ""):
        return ""
    try:
        d = Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return str(int(d))
    except Exception:
        return ""

def bucket_by_ranges(v: Union[str, float, int, None], bounds: List[Tuple[Optional[float], Optional[float], str]]) -> str:
    if v in (None, ""):
        return ""
    try:
        x = float(v)
    except Exception:
        return ""
    for low, high, b in bounds:
        if (low is None or x >= low) and (high is None or x <= high):
            return str(b)
    return ""

# illustrative cutoffs for the public version; not validated for clinical use
SYSTOLIC_BUCKETS = [
    (None, 81.999, "a"), (82, 86, "b"), (87, 91, "c"), (92, 96, "d"),
    (97, 101, "e"), (102, 106, "f"), (107, 111, "g"), (112, 116, "h"),
    (117, 121, "i"), (122, 126, "j"), (127, 131, "k"), (132, 136, "l"),
    (137, 141, "m"), (142, 146, "n"), (147, 151, "o"), (152, 156, "p"),
    (157, 161, "q"), (162, 166, "r"), (167, 171, "s"), (172, 176, "t"),
    (177, 181, "u"), (182, None, "v"),
]

DIASTOLIC_BUCKETS = [
    (None, 51.999, "a"), (52, 56, "b"), (57, 61, "c"), (62, 66, "d"),
    (67, 71, "e"), (72, 76, "f"), (77, 81, "g"), (82, 86, "h"),
    (87, 91, "i"), (92, 96, "j"), (97, 101, "k"), (102, 106, "l"),
    (107, 111, "m"), (112, 116, "n"), (117, 121, "o"), (122, None, "p"),
]

CHOLESTEROL_BUCKETS = [
    (None, 4.499, "a"), (4.5, 5.29, "b"), (5.3, 6.09, "c"), (6.1, 6.49, "d"),
    (6.5, 7.09, "e"), (7.1, None, "f"),
]

HDL_BUCKETS = [
    (None, 1.099, "a"), (1.1, 1.29, "b"), (1.3, 1.59, "c"), (1.6, 1.69, "d"),
    (1.7, 1.89, "e"), (1.9, None, "f"),
]

LDL_BUCKETS = [
    (None, 2.699, "a"), (2.7, 2.89, "b"), (2.9, 3.49, "c"), (3.5, 4.29, "d"),
    (4.3, 5.09, "e"), (5.1, None, "f"),
]

GLUCOSE_BUCKETS = [
    (None, 5.099, "a"), (5.1, 5.69, "b"), (5.7, 6.09, "c"), (6.1, 6.59, "d"),
    (6.6, 7.09, "e"), (7.1, None, "f"),
]

RHR_BUCKETS = [
    (None, 62.999, "a"), (63, 72, "b"), (73, 82, "c"), (83, 92, "d"),
    (93, 102, "e"), (103, None, "f"),
]
SMOKING_TO_NUM = {
    "smoker": 0, "non-smoker": 1, "ex-smoker": 2, "declined to answer": 3
}

ALCOHOL_TO_NUM = {
    "never": 0,
    "less than once a week": 1,
    "less-than-once": 1,
    "once a week": 2,
    "2-3 per week": 3,
    "2-3 times a week": 3,
    "4 or more per week": 4,
}

DIABETES_TYPE_TO_NUM = {
    "type1 diabetes mellitus": 0,
    "type2 diabetes mellitus": 1,
    "gestational diabetes mellitus": 2,
    "other specified diabetes mellitus": 3,
}

FITNESS_PERCEPTION_TO_NUM = {
    "poor": 0,
    "fair": 1,
    "good": 2,
    "excellent": 3,
}

# public risk category labels
RISK_LEVEL_TO_NUM = {
    "low likelihood": "a",
    "moderate likelihood": "b",
    "high likelihood": "c",
}

RISK_OUTCOME_TO_NUM = {
    "diabetes mellitus": "a",
    "preventive medicine risk assessment": "b",
    "cardiovascular disease": "c",
}

def normalize_count_2plus(v: Union[str, float, int, None]) -> str:
    if v in (None, ""):
        return ""
    s = str(v).strip()
    if s == "2+":
        return "3"
    return s

FIRST_DEGREE = {"parent","sibling","own child","child"}
SECOND_DEGREE = {"grandparent","aunt","uncle","cousin"}

def degree_counts(members_str: Union[str, None]) -> Tuple[str, str]:
    if not members_str:
        return ("","")
    tokens = [t.strip().lower() for t in str(members_str).split(",") if t.strip()]
    if not tokens:
        return ("","")
    first = 0
    second = 0
    for t in tokens:
        if t in FIRST_DEGREE:
            first += 1
        elif t in SECOND_DEGREE:
            second += 1
    return (str(first), str(second))

def bool_to_01(v: Optional[bool]) -> str:
    if v is True:
        return "0"
    if v is False:
        return "1"
    return ""

def as_bool_text(v: Optional[bool]) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return ""

def parse_patient_ids(raw: Optional[str]) -> Optional[set]:
    #optional patient ids
    if not raw:
        return None
    ids = {x.strip() for x in raw.split(",") if x.strip()}
    return ids or None

#ids

def _random_letters(n: int) -> str:
    return "".join(secrets.choice(string.ascii_uppercase) for _ in range(n))

def _random_digits(n: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(n))

def generate_pseudo_id(existing: set) -> str:
    """Create a unique SMCH-<5digits>-<3letters> not in 'existing'."""
    while True:
        candidate = f"SMCH-{_random_digits(5)}-{_random_letters(3)}"
        if candidate not in existing:
            return candidate

def build_pseudo_map(pat_idx: Dict[str,Dict[str,str]]) -> Dict[str,str]:
    """Map original patient IDs -> unique pseudo IDs for this run."""
    pseudo_map: Dict[str,str] = {}
    used: set = set()
    for pid in pat_idx.keys():
        pseudo = generate_pseudo_id(used)
        pseudo_map[pid] = pseudo
        used.add(pseudo)
    return pseudo_map

#csv export

def whole_age(v):
    if v in (None, ""):
        return ""
    try:
        return round_half_up_whole(v)
    except Exception:
        return ""


def build_consecutive_day_map(sorted_rows):
    groups = {}

    for idx, r in enumerate(sorted_rows):
        pid = str(r.get("ID-ORIGINAL") or r.get("ID") or "")
        age = whole_age(r.get("AGE"))
        mdate = str(r.get("MEASUREMENT DATE") or "")

        if not pid or not age or not mdate:
            continue

        try:
            d = date.fromisoformat(mdate)
        except Exception:
            continue

        groups.setdefault((pid, age), []).append((idx, d))

    out = {}

    for key, items in groups.items():
        items = sorted(items, key=lambda x: x[1])
        first_date = items[0][1]

        for row_idx, d in items:
            out[row_idx] = str((d - first_date).days + 1)

    return out

IDENT_METADATA_COLS = {
    "ORIGIN",
    "GROUP",
    "ID-ORIGINAL",
    "ID",
    "NAME",
    "EMAIL",
    "SEX",
    "DOB",
}

ANON_METADATA_COLS = {
    "ORIGIN",
    "GROUP",
    "ID",
    "SEX",
}

def has_ident_study_data(row):
    for k, v in row.items():
        if k in IDENT_METADATA_COLS:
            continue

        if v not in ("", None):
            return True

    return False

def has_anon_study_data(out):
    for k, v in out.items():
        if k in ANON_METADATA_COLS:
            continue

        if v not in ("", None):
            return True

    return False

def write_csvs_selected(rows_store: Dict[Tuple[str,str], Dict[str,Union[str,float,int,bool,None]]],
                        pat_idx: Dict[str,Dict[str,str]],
                        outdir: str,
                        which: str) -> List[str]:
    os.makedirs(outdir, exist_ok=True)

    #generate ids for this export
    pseudo_map = build_pseudo_map(pat_idx)

    #add empty rows for patients without data
    existing_by_pid = {pid for (pid, _age) in rows_store.keys()}
    for pid, meta in pat_idx.items():
        if pid not in existing_by_pid:
            key = (pid, "")
            row: Dict[str,Union[str,float,int,bool,None]] = {c: "" for c in CSV_COLS_WITH_IDENT}
            row["ORIGIN"] = meta.get("ORIGIN","")
            row["GROUP"] = meta.get("GROUP", "")
            row["ID-ORIGINAL"] = pid
            row["ID"] = pid
            row["NAME"] = meta.get("NAME","")
            row["EMAIL"] = meta.get("EMAIL","")
            row["SEX"] = meta.get("SEX","")
            row["DOB"] = meta.get("DOB","")
            row["MEASUREMENT DATE"] = ""
            row["AGE"] = ""
            rows_store[key] = row

    def _age_sort(a: str) -> Tuple[int, float]:
        if a == "":
            return (1, float("inf"))
        try:
            return (0, float(a))
        except Exception:
            return (0, float("inf"))

    sorted_keys = sorted(rows_store.keys(), key=lambda k: (int(k[0]) if str(k[0]).isdigit() else str(k[0]), k[1]))
    sorted_rows = [rows_store[k] for k in sorted_keys]

    consecutive_day_map = build_consecutive_day_map(sorted_rows)

    written: List[str] = []

    #with identifiers
    if which in ("ident", "both"):
        target = os.path.join(outdir, CSV_WITH_IDENT_NAME)
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS_WITH_IDENT)
            w.writeheader()
            for row_idx, r in enumerate(sorted_rows):
                out = {}
                for col in CSV_COLS_WITH_IDENT:
                    #keep original id for matching
                    if col == "ID-ORIGINAL":
                        out[col] = r.get("ID-ORIGINAL","") or r.get("ID","")
                        continue
                    if col == "ID":
                        orig = r.get("ID-ORIGINAL","") or r.get("ID","")
                        out[col] = pseudo_map.get(str(orig), "")
                        continue

                    val = r.get(col, "")
                    if col == "FAS SCORE":
                        val = calculate_fas_score(r)
                    if isinstance(val, bool):
                        out[col] = as_bool_text(val)
                    else:
                        if col in (
                            "NAME","EMAIL","DOB","SMOKING","ALCOHOL", "FITNESS PERCEPTION",
                            "DIABETES TYPE","DIABETES MEMBERS","CVD MEMBERS","SHUTTLE RUN DATE", 
                            "RISK ASSESSMENT OUTCOME", "RISK ASSESSMENT LEVEL"
                            ):
                            out[col] = "" if val is None else val
                        else:
                            out[col] = fmt2(val)
                if not has_ident_study_data(out):
                    continue
                w.writerow(out)
        written.append(os.path.abspath(target))

    #anonymized
    if which in ("anon", "both"):
        target = os.path.join(outdir, CSV_ANON_NAME)
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS_ANON)
            w.writeheader()
            for row_idx, r in enumerate(sorted_rows):
                out = {}
                origin = (r.get("ORIGIN") or "").strip().upper()
                out["ORIGIN"] = str(ORIGIN_TO_NUM.get(origin, "")) if origin else ""

                out["GROUP"] = r.get("GROUP", "")

                #use generated id
                orig_id = r.get("ID-ORIGINAL","") or r.get("ID","")
                out["ID"] = pseudo_map.get(str(orig_id), "")

                sex = (r.get("SEX") or "").strip().lower()
                out["SEX"] = str(SEX_TO_NUM.get(sex, "")) if sex else ""
                
                anon_age = whole_age(r.get("AGE"))
                mdate = str(r.get("MEASUREMENT DATE") or "")
                orig_id_for_day = str(r.get("ID-ORIGINAL") or r.get("ID") or "")

                out["AGE"] = anon_age
                out["CONSECUTIVE DAY"] = consecutive_day_map.get(row_idx, "")

                out["HEIGHT"] = round_half_up_whole(r.get("HEIGHT"))
                out["WEIGHT"] = round_half_up_whole(r.get("WEIGHT"))
                
                out["WAIST CIRC"] = round_half_up_whole(r.get("WAIST CIRC"))

                out["HIGH GLUCOSE"] = bool_to_01(r.get("HIGH GLUCOSE") if isinstance(r.get("HIGH GLUCOSE"), bool) else None)
                out["FASTING GLUCOSE"] = bucket_by_ranges(r.get("FASTING GLUCOSE"), GLUCOSE_BUCKETS)
                out["CHOLESTEROL"] = bucket_by_ranges(r.get("CHOLESTEROL"), CHOLESTEROL_BUCKETS)
                out["HDL"] = bucket_by_ranges(r.get("HDL"), HDL_BUCKETS)
                out["LDL"] = bucket_by_ranges(r.get("LDL"), LDL_BUCKETS)

                out["SYSTOLIC BP"]  = bucket_by_ranges(r.get("SYSTOLIC BP"), SYSTOLIC_BUCKETS)
                out["DIASTOLIC BP"] = bucket_by_ranges(r.get("DIASTOLIC BP"), DIASTOLIC_BUCKETS)
                out["RESTING HEART RATE"] = bucket_by_ranges(r.get("RESTING HEART RATE"), RHR_BUCKETS)

                out["MODERATE ACT"] = fmt2(r.get("MODERATE ACT"))
                out["VIGOROUS ACT"] = fmt2(r.get("VIGOROUS ACT"))
                out["SHUTTLE RUN SPEED"] = fmt2(r.get("SHUTTLE RUN SPEED"))
                out["SHUTTLE RUN LAPS"]  = fmt2(r.get("SHUTTLE RUN LAPS"))

                fit = (r.get("FITNESS PERCEPTION") or "").strip().lower()
                out["FITNESS PERCEPTION"] = str(FITNESS_PERCEPTION_TO_NUM.get(fit, "")) if fit else ""

                out["NON TRACKED ACTIVITY"] = bool_to_01(
                    r.get("NON TRACKED ACTIVITY") if isinstance(r.get("NON TRACKED ACTIVITY"), bool) else None
                )

                out["NON TRACKED TIME"] = fmt2(r.get("NON TRACKED TIME"))

                out["STEPS"] = fmt2(r.get("STEPS"))

                smk = (r.get("SMOKING") or "").strip().lower()
                out["SMOKING"] = str(SMOKING_TO_NUM.get(smk, "")) if smk else ""

                alc = (r.get("ALCOHOL") or "").strip().lower()
                out["ALCOHOL"] = str(ALCOHOL_TO_NUM.get(alc, "")) if alc else ""

                for col in ["VEGETABLES","FRUITS","SWEETENED BEVERAGES","MEATS","CEREALS","NUTS","SLEEP"]:
                    out[col] = fmt2(r.get(col))

                out["DIABETES"] = bool_to_01(r.get("DIABETES") if isinstance(r.get("DIABETES"), bool) else None)
                dtype = (r.get("DIABETES TYPE") or "").strip().lower()
                out["DIABETES TYPE"] = str(DIABETES_TYPE_TO_NUM.get(dtype, "")) if dtype else ""
                out["CVD"] = bool_to_01(r.get("CVD") if isinstance(r.get("CVD"), bool) else None)
                out["FAMILY DIABETES"] = bool_to_01(r.get("FAMILY DIABETES") if isinstance(r.get("FAMILY DIABETES"), bool) else None)
                out["FAMILY CVD"] = bool_to_01(r.get("FAMILY CVD") if isinstance(r.get("FAMILY CVD"), bool) else None)
                out["BLOOD PRESSURE MEDICATION"] = bool_to_01(r.get("BLOOD PRESSURE MEDICATION") if isinstance(r.get("BLOOD PRESSURE MEDICATION"), bool) else None)

                out["TOTAL CALORIES PER DAY"] = fmt2(r.get("TOTAL CALORIES PER DAY"))
                out["ACTIVE CALORIES PER DAY"] = fmt2(r.get("ACTIVE CALORIES PER DAY"))

                out["FAMILY CARS"] = normalize_count_2plus(r.get("FAMILY CARS"))
                out["FAMILY VACATIONS"] = normalize_count_2plus(r.get("FAMILY VACATIONS"))
                out["FAMILY COMPUTERS"] = normalize_count_2plus(r.get("FAMILY COMPUTERS"))
                out["BATHROOMS"] = normalize_count_2plus(r.get("BATHROOMS"))

                out["BEDROOM"] = bool_to_01(r.get("BEDROOM") if isinstance(r.get("BEDROOM"), bool) else None)
                out["DISHWASHER"] = bool_to_01(r.get("DISHWASHER") if isinstance(r.get("DISHWASHER"), bool) else None)
                out["FAS SCORE"] = str(calculate_fas_score(r))
                
                out["SCREEN HOURS"] = fmt2(r.get("SCREEN HOURS"))

                diab_fd, diab_sd = degree_counts(r.get("DIABETES MEMBERS") or "")
                cvd_fd, cvd_sd   = degree_counts(r.get("CVD MEMBERS") or "")
                out["DIABETES FIRST DEGREE"]  = diab_fd
                out["DIABETES SECOND DEGREE"] = diab_sd
                out["CVD FIRST DEGREE"]       = cvd_fd
                out["CVD SECOND DEGREE"]      = cvd_sd

                risk_outcome = (r.get("RISK ASSESSMENT OUTCOME") or "").strip()
                risk_level = (r.get("RISK ASSESSMENT LEVEL") or "").strip()

                out["RISK ASSESSMENT OUTCOME"] = (RISK_OUTCOME_TO_NUM.get(risk_outcome.lower(), risk_outcome) if risk_outcome else "")
                out["RISK ASSESSMENT PERCENT"] = fmt2(r.get("RISK ASSESSMENT PERCENT"))
                out["RISK ASSESSMENT RANGE LOW"] = fmt2(r.get("RISK ASSESSMENT RANGE LOW"))
                out["RISK ASSESSMENT RANGE HIGH"] = fmt2(r.get("RISK ASSESSMENT RANGE HIGH"))
                out["RISK ASSESSMENT LEVEL"] = (RISK_LEVEL_TO_NUM.get(risk_level.lower(), risk_level) if risk_level else "")

                if not has_anon_study_data(out):
                    continue
                w.writerow({k: out.get(k, "") for k in CSV_COLS_ANON})
        written.append(os.path.abspath(target))

    return written

#arguments

def _default_downloads_dir() -> str:
    out_dir_env = os.getenv("OUT_DIR")
    if out_dir_env:
        return out_dir_env

    system = platform.system().lower()
    if system.startswith("win"):
        base = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return os.path.join(base, "Downloads")
    return os.path.join(os.path.expanduser("~"), "Downloads")

def main():
    parser = argparse.ArgumentParser(description="Export FHIR data to CSV")
    #fhir url can also be set by argument
    parser.add_argument("--fhir-base", help="FHIR base URL (overrides FHIR_BASE env var)")
    parser.add_argument("--csv", choices=["ident", "anon", "both"], default="both",
                        help="Which CSV to generate")
    parser.add_argument("--outdir", default=_default_downloads_dir(),
                        help="Directory to write CSV(s) to (default: Downloads or $OUT_DIR)")
    parser.add_argument(
        "--patient-ids",
        help="Comma-separated list of FHIR Patient IDs to export. If omitted, all patients are exported."
    )
    args = parser.parse_args()

    global BASE_URL, SERVICE_ROOT
    if args.fhir_base:
        BASE_URL = args.fhir_base.strip()
    SERVICE_ROOT = normalize_base(BASE_URL)

    if not SERVICE_ROOT:
        raise SystemExit("Missing FHIR base URL. Set FHIR_BASE env var or pass --fhir-base")

    #server info
    cap = get_capability()
    name = cap.get("software", {}).get("name", "Unknown")
    version = cap.get("software", {}).get("version", "Unknown")
    fhir_ver = cap.get("fhirVersion", "Unknown")
    print(f"\nConnected! Server: {name}, version: {version}, FHIR: {fhir_ver}")

    #load patients
    patient_ids = parse_patient_ids(args.patient_ids)
    patients = load_patients(patient_ids)

    patient_groups = load_research_subject_groups(patient_ids)

    for pid, group in patient_groups.items():
        if pid in patients:
            patients[pid]["GROUP"] = group

    if patient_ids is not None:
        found = set(patients.keys())
        missing = sorted(patient_ids - found)
        print(f"\nPatient ID filter active: requested={len(patient_ids)}, found={len(found)}")
        if missing:
            print("Warning: these requested Patient IDs were not found:", ", ".join(missing))

    #patient data
    rows_store: Dict[Tuple[str,str], Dict[str,Union[str,float,int,bool,None]]] = {}
    total_cells = 0
    for pid in sorted(patients.keys(), key=lambda x: (len(x), x)):
        total_cells += collect_nutrition_intake_rows_for_patient(pid, patients, rows_store)
        total_cells += collect_qr_health_history_for_patient(pid, patients, rows_store)
        total_cells += collect_qr_lifestyle_for_patient(pid, patients, rows_store)
        total_cells += collect_qr_initial_risk_for_patient(pid, patients, rows_store)
        total_cells += collect_observations_for_patient(pid, patients, rows_store)
        total_cells += collect_risk_assessments_for_patient(pid, patients, rows_store)

    #csv export
    written_paths = write_csvs_selected(rows_store, patients, args.outdir, args.csv)
    for p in written_paths:
        print(f"Download: {p}")

    #resource counts
    print("\nResource counts:")
    for t in ["Patient", "ResearchSubject", "Observation", "NutritionIntake", "QuestionnaireResponse", "RiskAssessment"]:
        try:
            c = get_count_hapi(t)
            print(f"  {t}: {c}")
        except Exception as e:
            print(f"  {t}: error: {e}")

    print(f"\nCells filled via per-patient subject queries: {total_cells}\n")

if __name__ == "__main__":
    main()
