"""Language code helpers.

Canonical form everywhere in the app: lowercase ISO 639-3 (``deu``, ``fra``, ``gsw``)
plus two sentinels, ``instrumental`` and ``unknown``.
"""
from __future__ import annotations

INSTRUMENTAL = "instrumental"
UNKNOWN = "unknown"

# MusicBrainz special language codes
MB_NO_LINGUISTIC_CONTENT = "zxx"
MB_MULTIPLE = "mul"
MB_UNDETERMINED = "und"

NAMES: dict[str, str] = {
    "eng": "English", "deu": "German", "gsw": "Swiss German", "bar": "Bavarian",
    "fra": "French", "spa": "Spanish", "ita": "Italian", "por": "Portuguese",
    "nld": "Dutch", "swe": "Swedish", "dan": "Danish", "nob": "Norwegian Bokmål",
    "nno": "Norwegian Nynorsk", "fin": "Finnish", "isl": "Icelandic", "pol": "Polish",
    "ces": "Czech", "slk": "Slovak", "hun": "Hungarian", "ron": "Romanian",
    "rus": "Russian", "ukr": "Ukrainian", "bul": "Bulgarian", "srp": "Serbian",
    "hrv": "Croatian", "slv": "Slovene", "ell": "Greek", "tur": "Turkish",
    "ara": "Arabic", "heb": "Hebrew", "fas": "Persian", "hin": "Hindi",
    "ben": "Bengali", "urd": "Urdu", "tam": "Tamil", "tel": "Telugu",
    "jpn": "Japanese", "kor": "Korean", "zho": "Chinese", "tha": "Thai",
    "vie": "Vietnamese", "ind": "Indonesian", "msa": "Malay", "tgl": "Tagalog",
    "swa": "Swahili", "afr": "Afrikaans", "gle": "Irish", "cym": "Welsh",
    "lat": "Latin", "eus": "Basque", "cat": "Catalan", "glg": "Galician",
    "est": "Estonian", "lav": "Latvian", "lit": "Lithuanian", "ltz": "Luxembourgish",
    "yid": "Yiddish", "epo": "Esperanto", "sqi": "Albanian", "mkd": "Macedonian",
}

_ISO1 = {
    "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por",
    "nl": "nld", "sv": "swe", "da": "dan", "no": "nob", "nb": "nob", "nn": "nno",
    "fi": "fin", "is": "isl", "pl": "pol", "cs": "ces", "sk": "slk", "hu": "hun",
    "ro": "ron", "ru": "rus", "uk": "ukr", "bg": "bul", "sr": "srp", "hr": "hrv",
    "sl": "slv", "el": "ell", "tr": "tur", "ar": "ara", "he": "heb", "fa": "fas",
    "hi": "hin", "bn": "ben", "ur": "urd", "ta": "tam", "te": "tel", "ja": "jpn",
    "ko": "kor", "zh": "zho", "th": "tha", "vi": "vie", "id": "ind", "ms": "msa",
    "tl": "tgl", "sw": "swa", "af": "afr", "ga": "gle", "cy": "cym", "la": "lat",
    "eu": "eus", "ca": "cat", "gl": "glg", "et": "est", "lv": "lav", "lt": "lit",
    "lb": "ltz", "yi": "yid", "eo": "epo", "sq": "sqi", "mk": "mkd",
}

# ISO 639-2/B and other legacy spellings occasionally seen in metadata
_LEGACY = {
    "ger": "deu", "fre": "fra", "dut": "nld", "gre": "ell", "chi": "zho",
    "cze": "ces", "rum": "ron", "slo": "slk", "ice": "isl", "wel": "cym",
    "baq": "eus", "per": "fas", "may": "msa", "alb": "sqi", "mac": "mkd",
}

_NAME_ALIASES = {
    "norwegian": "nob", "chinese": "zho", "mandarin": "zho", "farsi": "fas",
    "swiss": "gsw", "schweizerdeutsch": "gsw", "deutsch": "deu",
}

# Dialects that should also appear in their parent language's playlist by default.
DIALECT_PARENT = {"gsw": "deu", "bar": "deu"}


def normalize(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().lower()
    if c in (INSTRUMENTAL, UNKNOWN):
        return c
    return _ISO1.get(c, _LEGACY.get(c, c))


def display(code: str | None) -> str:
    if not code:
        return "—"
    if code == INSTRUMENTAL:
        return "Instrumental"
    if code == UNKNOWN:
        return "Unknown"
    return NAMES.get(code, code.upper())


def resolve(text: str) -> str | None:
    """Turn user input ("German", "de", "deu", "inst") into a canonical code, or None."""
    t = text.strip().lower()
    if not t:
        return None
    if t in ("instrumental", "inst", "zxx"):
        return INSTRUMENTAL
    by_name = {n.lower(): c for c, n in NAMES.items()}
    by_name.update(_NAME_ALIASES)
    if t in by_name:
        return by_name[t]
    code = normalize(t)
    return code if code in NAMES else None


def matches(language: str | None, wanted: str, include_dialects: bool = True) -> bool:
    if language is None:
        return False
    return language == wanted or (include_dialects and DIALECT_PARENT.get(language) == wanted)
