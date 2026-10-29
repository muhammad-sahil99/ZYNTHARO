"""Languages and direction (contract 22.11).

An assistant that understands only English-LTR app interfaces is not usable in a large part
of the world, and Urdu and Arabic are explicitly required. The hard parts are not "add a
translation table", they are:

* **Direction.** A right-to-left window lays its controls out mirrored. Clicking the element
  reported at (x, y) is still correct -- coordinates are coordinates -- but *reading* the
  interface, exporting it as text for a model without vision, and rendering the HUD all
  have to carry direction, or the reasoning is silently wrong.
* **Text normalisation.** Arabic is often rendered with presentation forms and ligatures, and
  uses its own digit shapes. Comparing extracted text to expected text fails without
  normalisation, which then looks like a failed action.
* **Model and OCR routing.** Vision models accept language hints; OCR engines need an
  explicit language pack. Both live here, so no caller hard-codes English.

Nothing here guesses a language from a single character: detection requires a meaningful
amount of script-specific text, and mixed text reports the dominant direction with the
minority direction recorded alongside it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

LTR = "ltr"
RTL = "rtl"


@dataclass
class Language:
    code: str
    name: str
    native_name: str
    direction: str = LTR
    ocr: str = ""
    tts: str = ""
    stt: str = ""


LANGUAGES: dict[str, Language] = {
    "en": Language("en", "English", "English", LTR, "eng", "en-US-AriaNeural", "en"),
    "ur": Language("ur", "Urdu", "اردو", RTL, "urd", "ur-PK-AsadNeural", "ur"),
    "ar": Language("ar", "Arabic", "العربية", RTL, "ara", "ar-SA-ZariyahNeural", "ar"),
    "fa": Language("fa", "Persian", "فارسی", RTL, "fas", "fa-IR-DilaraNeural", "fa"),
    "he": Language("he", "Hebrew", "עברית", RTL, "heb", "he-IL-HilaNeural", "he"),
    "hi": Language("hi", "Hindi", "हिन्दी", LTR, "hin", "hi-IN-SwaraNeural", "hi"),
    "es": Language("es", "Spanish", "Español", LTR, "spa", "es-ES-ElviraNeural", "es"),
    "fr": Language("fr", "French", "Français", LTR, "fra", "fr-FR-DeniseNeural", "fr"),
    "de": Language("de", "German", "Deutsch", LTR, "deu", "de-DE-KatjaNeural", "de"),
    "zh": Language("zh", "Chinese", "中文", LTR, "chi_sim", "zh-CN-XiaoxiaoNeural", "zh"),
}

RTL_LANGUAGES = frozenset(code for code, lang in LANGUAGES.items() if lang.direction == RTL)

#: Letters that exist in Urdu *and* Persian but not in standard Arabic orthography
#: (the Persian/Urdu kaf and yeh, and the p/che/dje/ze/gaf letters).
PERSIAN_SPECIFIC = frozenset("پچژگکی")
URDU_SHARED_WITH_PERSIAN = PERSIAN_SPECIFIC
#: Letters Urdu has and Persian does not: retroflex ٹ ڈ ڑ, noon ghunna ں, do-chashmi he ھ,
#: bare he ے, and the Urdu heh/teh marbuta forms.
URDU_ONLY = frozenset("ٹڈڑںھۂۃےۓڪڭہ")
URDU_SPECIFIC = URDU_ONLY | URDU_SHARED_WITH_PERSIAN
#: Letters that occur in standard Arabic and are largely absent from Persian/Urdu prose.
#: Their presence is evidence *for* Arabic and against Persian.
ARABIC_ONLY = frozenset("يكةىئإأؤذصضطظثعغ")

#: Unicode blocks that only occur in right-to-left scripts.
_RTL_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic (includes Urdu's extended letters)
    (0x0700, 0x074F),  # Syriac
    (0x0750, 0x077F),  # Arabic supplement
    (0x08A0, 0x08FF),  # Arabic extended-A
    (0xFB1D, 0xFB4F),  # Hebrew presentation forms
    (0xFB50, 0xFDFF),  # Arabic presentation forms-A
    (0xFE70, 0xFEFF),  # Arabic presentation forms-B
)
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_EXTENDED_ARABIC_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
#: Urdu/Arabic combining marks that make string comparison fail for no good reason.
_TATWEEL = "\u0640"
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")


def is_rtl_char(char: str) -> bool:
    point = ord(char)
    return any(low <= point <= high for low, high in _RTL_RANGES)


def direction_of(text: str) -> str:
    """Dominant direction of a string. Empty or script-neutral text defaults to LTR."""
    rtl = sum(1 for c in text if is_rtl_char(c))
    ltr_letters = sum(1 for c in text if c.isalpha() and not is_rtl_char(c))
    if rtl == 0:
        return LTR
    # A single stray character in a mostly-Latin label is not an RTL interface.
    return RTL if rtl * 3 >= max(1, ltr_letters) else LTR


def detect(text: str) -> str:
    """Best-effort language code. Requires enough script-specific text to be meaningful.

    Urdu and Persian share the Arabic block with Arabic itself, so a raw character count
    decides nothing. The decision is made from *positive* evidence instead:

    * a retroflex or Urdu-only letter (``ٹ ڈ ڑ ں ھ ے``) is Urdu evidence, and it cannot occur
      in Arabic or Persian by accident;
    * a Persian/Urdu letter (``پ چ ژ گ ک ی``) is evidence against Arabic; when such letters are
      dense enough and no Arabic-only letter (``ي ك ة ى``) appears, the answer is Persian;
    * otherwise the text is Arabic.

    A Persian sentence made only of letters that Arabic also uses (``سلام``, say) is genuinely
    indistinguishable without a dictionary, and is reported as Arabic rather than guessed at.
    """
    if not text.strip():
        return "en"
    counts: dict[str, int] = {}
    arabic_block = 0
    urdu_markers = 0
    persian_markers = 0
    arabic_only = 0
    for char in text:
        point = ord(char)
        if 0x0600 <= point <= 0x06FF:
            arabic_block += 1
            if char in URDU_ONLY:
                urdu_markers += 1
            elif char in PERSIAN_SPECIFIC:
                persian_markers += 1
            elif char in ARABIC_ONLY:
                arabic_only += 1
        elif 0x0590 <= point <= 0x05FF:
            counts["he"] = counts.get("he", 0) + 1
        elif 0x0900 <= point <= 0x097F:
            counts["hi"] = counts.get("hi", 0) + 1
        elif 0x4E00 <= point <= 0x9FFF:
            counts["zh"] = counts.get("zh", 0) + 1
        elif char.isalpha():
            counts["en"] = counts.get("en", 0) + 1
    if arabic_block:
        if urdu_markers >= 2 or (urdu_markers == 1 and persian_markers <= 2):
            counts["ur"] = arabic_block + urdu_markers * 2
        elif persian_markers >= 2 and arabic_only == 0 and persian_markers * 5 >= arabic_block:
            counts["fa"] = arabic_block + persian_markers
        else:
            counts["ar"] = arabic_block + arabic_only
    if not counts:
        return "en"
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] >= 3 else "en"


def normalize(text: str) -> str:
    """Make extracted text comparable: NFKC, no tatweel/diacritics, ASCII digits.

    Used on *both* sides of a comparison -- what the agent typed and what it read back --
    otherwise Arabic UI text "fails" verification while looking identical on screen.
    """
    out = unicodedata.normalize("NFKC", text)
    out = out.replace(_TATWEEL, "")
    out = _DIACRITICS.sub("", out)
    out = out.translate(_ARABIC_INDIC_DIGITS).translate(_EXTENDED_ARABIC_DIGITS)
    return re.sub(r"\s+", " ", out).strip()


def equivalent(a: str, b: str) -> bool:
    """Are these two strings the same text, allowing for script rendering differences?"""
    return normalize(a).casefold() == normalize(b).casefold()


def ocr_languages(codes: Iterable[str] = ()) -> str:
    """OCR language pack string, e.g. ``eng+urd+ara`` for Tesseract/RapidOCR wiring."""
    selected = [c for c in codes if c in LANGUAGES and LANGUAGES[c].ocr]
    if not selected:
        selected = ["en"]
    return "+".join(LANGUAGES[c].ocr for c in selected)


def tts_voice(code: str, fallback: str = "") -> str:
    language = LANGUAGES.get(code)
    return language.tts if language else (fallback or LANGUAGES["en"].tts)


def stt_language(code: str) -> str:
    language = LANGUAGES.get(code)
    return language.stt if language else "en"


@dataclass
class LanguageProfile:
    """What the rest of the system needs to know about the text in front of it."""

    primary: str = "en"
    secondary: list[str] = field(default_factory=list)
    direction: str = LTR
    mixed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "direction": self.direction,
            "mixed": self.mixed,
            "ocr": ocr_languages([self.primary, *self.secondary]),
            "ui_dir": self.direction,
        }


def profile_for(text: str, hints: Sequence[str] = ()) -> LanguageProfile:
    """Describe the text: dominant language, direction, and whether it mixes scripts."""
    detected = detect(text)
    primary = detected
    if hints:
        primary = hints[0]
    secondary = [h for h in hints if h != primary]
    if detected != primary and detected not in secondary:
        secondary.append(detected)
    flags = {direction_of(text)}
    flags.update(LANGUAGES[c].direction for c in (primary, *secondary) if c in LANGUAGES)
    mixed = len(flags) > 1
    direction = direction_of(text) if text.strip() else LANGUAGES.get(primary, LANGUAGES["en"]).direction
    return LanguageProfile(primary=primary, secondary=secondary, direction=direction, mixed=mixed)
