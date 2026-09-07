"""Basic-parser boundaries; no optional academic dependency required."""

import pytest

from rka.services.bibtex import BibtexParseError, parse_basic_bibtex


@pytest.mark.parametrize("opener,closer", [("{", "}"), ("(", ")")])
def test_balanced_values_quotes_numbers_and_comments(opener, closer):
    body = r"""key,
      Title = {A {Nested} title with \{escaped\} braces},
      year = 2026, % comment between fields
      author = "Name with {\"accent} and {nested}",
      url = {https://example.invalid/a@b},
    """
    text = "% ignored @article{bad}\n@article" + opener + body + closer
    entry = parse_basic_bibtex(text)[0]
    assert entry["fields"]["title"] == r"A {Nested} title with \{escaped\} braces"
    assert entry["fields"]["year"] == "2026"
    assert entry["fields"]["url"] == "https://example.invalid/a@b"
    assert entry["raw"] == "@article" + opener + body + closer


@pytest.mark.parametrize(
    "text",
    [
        "@article{a, title={A} # {B}}",
        "@string{journal={J}}",
        "@article{a, title=undefined_macro}",
        '@article{a, title="Unclosed}',
        "@article{a, title={A} year=2026}",
        "@article{a, title={A}} @article{a, title={B}}",
        "@article{a, TITLE={A}, title={B}}",
        "@article{a, title={" + "{" * 65 + "deep" + "}" * 66 + "}",
    ],
)
def test_basic_parser_rejects_unsupported_or_malformed_input(text):
    with pytest.raises(BibtexParseError):
        parse_basic_bibtex(text)


def test_comments_and_empty_input_do_not_create_entries():
    assert parse_basic_bibtex("") == []
    assert parse_basic_bibtex("% only a comment\n@comment{with {nested} braces}") == []


def test_parser_implementation_error_is_not_hidden_by_fallback(monkeypatch):
    from rka.services.academic import AcademicImportService

    service = AcademicImportService(None)

    def fail(content):
        raise AttributeError("synthetic implementation defect")

    monkeypatch.setattr(service, "_parse_bibtex_with_library", fail)
    with pytest.raises(AttributeError, match="implementation defect"):
        service._parse_bibtex("@article{a, title={A}}")
