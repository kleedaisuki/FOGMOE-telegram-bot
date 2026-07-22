"""@brief 中立严格 JSONC 解码器测试 / Tests for the neutral strict JSONC decoder."""

from __future__ import annotations

from pathlib import Path

import pytest

from fogmoe_config.jsonc import JsoncDecodeError, load_jsonc, parse_jsonc


def test_parser_preserves_comment_markers_inside_strings() -> None:
    """@brief URL 与字符串中的注释标记不是注释 / URL and string comment markers are not comments.

    @return None / None.
    """

    document = parse_jsonc(
        r"""{
            "endpoint": "https://example.test/v1//models/*stable*/",
            "literal": "/* this is text */ // this too",
            "escaped_quote": "quote: \\\" // still inside the string"
        }"""
    )

    assert document == {
        "endpoint": "https://example.test/v1//models/*stable*/",
        "literal": "/* this is text */ // this too",
        "escaped_quote": 'quote: \\" // still inside the string',
    }


def test_parser_accepts_line_and_block_comments() -> None:
    """@brief 行注释和块注释可出现在 JSON 空白处 / Line and block comments may occur where JSON whitespace occurs.

    @return None / None.
    """

    document = parse_jsonc(
        """{
        /* explanation spanning
           two lines */
        "enabled": true, // safe default
        "nested": { /* local note */ "retries": 3 }
        }"""
    )

    assert document == {"enabled": True, "nested": {"retries": 3}}


def test_parser_preserves_syntax_error_location_after_comments() -> None:
    """@brief 注释替换不改变后续语法错误行列 / Comment replacement preserves later syntax-error locations.

    @return None / None.
    """

    source = """{
  /* preserved width */
  "enabled" true
}"""

    with pytest.raises(
        JsoncDecodeError,
        match=r"invalid JSON at line 3, column 13: Expecting ':' delimiter",
    ):
        parse_jsonc(source)


def test_parser_rejects_unterminated_block_comment() -> None:
    """@brief 未闭合块注释必须失败 / An unterminated block comment must fail.

    @return None / None.
    """

    with pytest.raises(
        JsoncDecodeError,
        match=r"unterminated block comment at line 1, column 18",
    ):
        parse_jsonc('{"enabled": true /* missing terminator')


def test_parser_rejects_duplicate_keys() -> None:
    """@brief 同一 JSON 对象中的重复键必须失败 / Duplicate keys within one JSON object must fail.

    @return None / None.
    """

    with pytest.raises(JsoncDecodeError, match="duplicate object key 'mode'"):
        parse_jsonc('{"enabled": true, "nested": {"mode": "a", "mode": "b"}}')


@pytest.mark.parametrize("source", ('{"enabled": true,}', '{"items": [1,]}'))
def test_parser_rejects_trailing_commas(source: str) -> None:
    """@brief 逗号后必须有 JSON 值或对象成员 / A comma must be followed by a JSON value or object member.

    @param source 含尾逗号的 JSONC 文本 / JSONC text with a trailing comma.
    @return None / None.
    """

    with pytest.raises(JsoncDecodeError):
        parse_jsonc(source)


@pytest.mark.parametrize(
    "source",
    (
        "{'enabled': true}",
        "{enabled: true}",
        '{"value": 0x10}',
        '{"value": .5}',
    ),
)
def test_parser_rejects_json5_extensions_other_than_comments(source: str) -> None:
    """@brief 仅注释是扩展，JSON5 其他语法必须失败 / Comments are the only extension.

    @param source 含 JSON5 非注释扩展的文本 / Text containing a non-comment JSON5 extension.
    @return None / None.
    """

    with pytest.raises(JsoncDecodeError):
        parse_jsonc(source)


@pytest.mark.parametrize("source", ("[]", '"scalar"', "42", "null"))
def test_parser_rejects_non_object_top_levels(source: str) -> None:
    """@brief 顶层必须是对象而非数组或标量 / The top level must be an object, not an array or scalar.

    @param source 顶层非对象的 JSONC 文本 / JSONC text whose top level is not an object.
    @return None / None.
    """

    with pytest.raises(
        JsoncDecodeError,
        match="top-level JSONC value must be an object",
    ):
        parse_jsonc(source)


@pytest.mark.parametrize(
    "token",
    ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"),
)
def test_parser_rejects_non_standard_numeric_constants(token: str) -> None:
    """@brief Python 默认宽容的 NaN/Infinity 不是合法 JSON / NaN and Infinity accepted by Python by default are not valid JSON.

    @param token 非标准数值 token / Non-standard numeric token.
    @return None / None.
    """

    with pytest.raises(
        JsoncDecodeError,
        match="(?:non-standard JSON numeric constant|non-finite JSON numeric value)",
    ):
        parse_jsonc(f'{{"value": {token}}}')


def test_loader_reads_utf8_file(tmp_path: Path) -> None:
    """@brief load 只读取显式传入的 UTF-8 文件 / load reads only its explicit UTF-8 file.

    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    path = tmp_path / "config.json"
    path.write_text('{"name": "雾萌" // comment\n}', encoding="utf-8")

    assert load_jsonc(path) == {"name": "雾萌"}


def test_loader_wraps_file_read_errors(tmp_path: Path) -> None:
    """@brief load 将文件读取失败转换为解析错误 / load turns file-read failures into decode errors.

    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    missing_path = tmp_path / "missing.json"

    with pytest.raises(JsoncDecodeError, match="cannot read JSONC file"):
        load_jsonc(missing_path)
