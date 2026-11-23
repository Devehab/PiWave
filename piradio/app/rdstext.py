"""Make text safe for RDS.

RDS radios use a Latin-only character set, so Arabic letters would show as
blanks. Arabic is converted to "Arabizi" (e.g. "عمرو دياب" -> "3mrw Dyab"),
everything else is reduced to printable ASCII.
"""
import re
import unicodedata

_ARABIC = {
    "ا": "a", "أ": "a", "إ": "e", "آ": "a", "ٱ": "a", "ء": "'", "ب": "b",
    "ت": "t", "ث": "th", "ج": "j", "ح": "7", "خ": "kh", "د": "d", "ذ": "th",
    "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t",
    "ظ": "z", "ع": "3", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l",
    "م": "m", "ن": "n", "ه": "h", "و": "w", "ي": "y", "ى": "a", "ة": "a",
    "ئ": "'e", "ؤ": "'o", "ـ": "", "،": ",", "؛": ";", "؟": "?", "پ": "p",
    "چ": "ch", "ڤ": "v", "گ": "g", "ک": "k", "ی": "y", "٪": "%",
}
_ARABIC.update({chr(0x0660 + i): str(i) for i in range(10)})   # ٠..٩
_ARABIC.update({chr(0x06F0 + i): str(i) for i in range(10)})   # ۰..۹ (Persian)
_ARABIC_RANGE = re.compile("[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def to_rds(text, maxlen):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))  # folds Arabic presentation forms
    has_arabic = bool(_ARABIC_RANGE.search(text))
    out = []
    for ch in text:
        if ch in _ARABIC:
            out.append(_ARABIC[ch])
            continue
        # strip accents: é -> e
        base = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in base if not unicodedata.combining(c))
        out.append("".join(c for c in base if " " <= c <= "~") or " ")
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    if has_arabic:
        s = " ".join(w[:1].upper() + w[1:] for w in s.split(" "))
    return s[:maxlen].rstrip()


def ps_segments(text, width=8):
    """Split text into <=8-char chunks on word boundaries (for scrolling PS)."""
    words = to_rds(text, 64).split()
    segs, cur = [], ""
    for w in words:
        while len(w) > width:
            if cur:
                segs.append(cur)
                cur = ""
            segs.append(w[:width])
            w = w[width:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            segs.append(cur)
            cur = w
    if cur:
        segs.append(cur)
    return segs
