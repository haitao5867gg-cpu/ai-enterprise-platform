"""Shared, lazily loaded Chinese/English keyword tokenization."""

import tempfile
import unicodedata
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def _tokenizer() -> Any:
    import jieba

    tokenizer = jieba.Tokenizer()
    tokenizer.tmp_dir = tempfile.gettempdir()
    tokenizer.initialize()
    return tokenizer


@lru_cache(maxsize=1)
def _stopwords() -> frozenset[str]:
    return frozenset(files(__package__).joinpath("stopwords.txt").read_text("utf-8").split())


def warm() -> None:
    """Load the dictionary and bundled stopwords in a startup worker thread."""
    _tokenizer()
    _stopwords()


def terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    stopwords = _stopwords()
    return [
        term
        for term in _tokenizer().cut_for_search(normalized)
        if any(char.isalnum() for char in term) and term not in stopwords
    ]


def chunk_keywords(text: str, heading: str = "") -> str:
    return " ".join(terms(f"{heading}\n{text}" if heading else text))
