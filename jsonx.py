"""jsonx.py - robust extraction of the findings array from agent output."""
import json, re

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_SEV_ALIASES = {"crit": "critical", "major": "high", "error": "high", "moderate": "medium", "warning": "medium",
                "warn": "medium", "minor": "low", "nit": "low", "informational": "info", "note": "info"}
CATEGORIES = ("locking", "lifetime", "error-path", "integer-overflow", "user-pointer", "endianness",
              "memory-ordering", "other")


class ExtractError(ValueError):
    pass


def parse_envelope(stdout):
    """Agent --output-format json prints one object. Tolerate log noise / stream-json lines.
    Returns (result_text, envelope_dict_or_None)."""
    s = (stdout or "").strip()
    if not s:
        raise ExtractError("empty agent output")
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            return str(d.get("result", "")), d
    except ValueError:
        pass
    env = None
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("type") == "result":
                env = d
                break
    if env is not None:
        return str(env.get("result", "")), env
    return s, None  # plain text output (e.g. --output-format text)


def _candidates(txt):
    # 1) the whole text  2) contents of ``` fences  3) every '[' / '{' raw_decode attempt
    yield txt.strip()
    for m in re.finditer(r"```(?:json|JSON)?\s*\n(.*?)```", txt, re.S):
        yield m.group(1).strip()
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", txt):
        try:
            obj, _ = dec.raw_decode(txt, m.start())
        except ValueError:
            continue
        yield obj


def _as_list(obj):
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return None
    if isinstance(obj, dict):
        for k in ("findings", "issues", "results", "bugs"):
            if isinstance(obj.get(k), list):
                return obj[k]
        if "severity" in obj and ("rationale" in obj or "line" in obj):
            return [obj]
        return None
    if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
        return obj
    return None


def normalize(f):
    out = {}
    try:
        out["line"] = int(str(f.get("line", 0)).split("-")[0].strip().lstrip("L") or 0)
    except ValueError:
        out["line"] = 0
    sev = str(f.get("severity", "low")).strip().lower()
    sev = _SEV_ALIASES.get(sev, sev)
    out["severity"] = sev if sev in SEV_RANK else "low"
    cat = str(f.get("category", "other")).strip().lower().replace("_", "-").replace(" ", "-")
    out["category"] = cat if cat in CATEGORIES else "other"
    out["rationale"] = str(f.get("rationale") or f.get("description") or f.get("message") or "").strip()
    try:
        c = float(f.get("confidence", 0.5))
        if c > 1:
            c = c / 100.0
    except (TypeError, ValueError):
        c = 0.5
    out["confidence"] = round(max(0.0, min(1.0, c)), 2)
    return out


def extract_findings(result_text):
    """Return a normalized list of findings or raise ExtractError."""
    if result_text is None:
        raise ExtractError("no result text")
    for cand in _candidates(result_text):
        lst = _as_list(cand)
        if lst is None:
            continue
        good = [normalize(x) for x in lst if isinstance(x, dict)]
        good = [g for g in good if g["rationale"] or g["line"]]
        if lst and not good:
            continue
        return good
    raise ExtractError("no JSON findings array found in model output")
