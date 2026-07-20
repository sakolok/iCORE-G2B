import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


_ASCII_WORD_PATTERN = re.compile(r"^[a-z0-9]+$")


@dataclass(frozen=True)
class KeywordDecision:
    keep: bool
    matched_keyword: str = ""
    excluded_keyword: str = ""


def _clean_keyword(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().split())


def _comparison_value(value: Any) -> str:
    return _clean_keyword(value).casefold()


def normalize_keywords(values: Iterable[Any] | str | None) -> list[str]:
    if values is None:
        return []
    candidates = values.split(",") if isinstance(values, str) else values
    seen: set[str] = set()
    normalized: list[str] = []
    for value in candidates:
        cleaned = _clean_keyword(value)
        comparison = cleaned.casefold()
        if not comparison or comparison in seen:
            continue
        seen.add(comparison)
        normalized.append(cleaned)
    return normalized


def _contains_keyword(text: str, keyword: str) -> bool:
    if not text or not keyword:
        return False
    if _ASCII_WORD_PATTERN.fullmatch(keyword):
        for match in re.finditer(re.escape(keyword), text):
            left_is_ascii_word = (
                match.start() > 0
                and text[match.start() - 1].isascii()
                and text[match.start() - 1].isalnum()
            )
            right_is_ascii_word = (
                match.end() < len(text)
                and text[match.end()].isascii()
                and text[match.end()].isalnum()
            )
            if not (left_is_ascii_word and right_is_ascii_word):
                return True
        return False
    return keyword in text


def evaluate_keyword_title(
    title: Any,
    keywords: Iterable[Any] | str | None,
    excluded_keywords: Iterable[Any] | str | None = None,
) -> KeywordDecision:
    comparison_title = _comparison_value(title)
    includes = normalize_keywords(keywords)
    excludes = normalize_keywords(excluded_keywords)

    for excluded in excludes:
        if _contains_keyword(comparison_title, _comparison_value(excluded)):
            return KeywordDecision(keep=False, excluded_keyword=excluded)

    for included in includes:
        if _contains_keyword(comparison_title, _comparison_value(included)):
            return KeywordDecision(keep=True, matched_keyword=included)

    return KeywordDecision(keep=False)
