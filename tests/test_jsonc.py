"""@brief 各包内联 JSONC 解析器测试 / Tests for package-local JSONC parsers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from fogmoe_bot import config as bot_config
from fogmoe_dashboard import config as dashboard_config
from fogmoe_dbctl import config as dbctl_config


#: @brief 三个可执行程序各自内联的文本解析入口 / Text parser owned locally by each executable package.
PARSERS: tuple[Callable[[str], Mapping[str, object]], ...] = (
    bot_config._parse_jsonc,
    dbctl_config._parse_jsonc,
    dashboard_config._parse_jsonc,
)
#: @brief 三个可执行程序各自内联的文件解析入口 / File parser owned locally by each executable package.
LOADERS: tuple[Callable[[Path], Mapping[str, object]], ...] = (
    bot_config._load_jsonc,
    dbctl_config._load_jsonc,
    dashboard_config._load_jsonc,
)


@pytest.mark.parametrize("parse", PARSERS)
def test_parsers_preserve_comment_markers_inside_strings(
    parse: Callable[[str], Mapping[str, object]],
) -> None:
    """@brief URL 与字符串中的注释标记不是注释 / URL and string comment markers are not comments.

    @return None / None.
    """

    document = parse(
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


@pytest.mark.parametrize("parse", PARSERS)
def test_parsers_accept_line_and_block_comments(
    parse: Callable[[str], Mapping[str, object]],
) -> None:
    """@brief 行注释和块注释可出现在 JSON 空白处 / Line and block comments may occur where JSON whitespace occurs.

    @return None / None.
    """

    document = parse(
        """{
        /* explanation spanning
           two lines */
        "enabled": true, // safe default
        "nested": { /* local note */ "retries": 3 }
        }"""
    )

    assert document == {"enabled": True, "nested": {"retries": 3}}


@pytest.mark.parametrize("parse", PARSERS)
def test_parsers_reject_unterminated_block_comment(
    parse: Callable[[str], Mapping[str, object]],
) -> None:
    """@brief 未闭合块注释必须失败 / An unterminated block comment must fail.

    @return None / None.
    """

    with pytest.raises(ValueError, match="unterminated block comment"):
        parse('{"enabled": true /* missing terminator')


@pytest.mark.parametrize("parse", PARSERS)
def test_parsers_reject_duplicate_keys(
    parse: Callable[[str], Mapping[str, object]],
) -> None:
    """@brief 同一 JSON 对象中的重复键必须失败 / Duplicate keys within one JSON object must fail.

    @return None / None.
    """

    with pytest.raises(ValueError, match="duplicate object key"):
        parse('{"enabled": true, "nested": {"mode": "a", "mode": "b"}}')


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("source", ('{"enabled": true,}', '{"items": [1,]}'))
def test_parsers_reject_trailing_commas(
    parse: Callable[[str], Mapping[str, object]], source: str
) -> None:
    """@brief 逗号后必须有 JSON 值或对象成员 / A comma must be followed by a JSON value or object member.

    @param source 含尾逗号的 JSONC 文本 / JSONC text with a trailing comma.
    @return None / None.
    """

    with pytest.raises(ValueError):
        parse(source)


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize(
    "source",
    (
        "{'enabled': true}",
        "{enabled: true}",
        '{"value": 0x10}',
        '{"value": .5}',
    ),
)
def test_parsers_reject_json5_extensions_other_than_comments(
    parse: Callable[[str], Mapping[str, object]], source: str
) -> None:
    """@brief 仅注释是扩展，JSON5 其他语法必须失败 / Comments are the only extension.

    @param source 含 JSON5 非注释扩展的文本 / Text containing a non-comment JSON5 extension.
    @return None / None.
    """

    with pytest.raises(ValueError):
        parse(source)


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("source", ("[]", '"scalar"', "42", "null"))
def test_parsers_reject_non_object_top_levels(
    parse: Callable[[str], Mapping[str, object]], source: str
) -> None:
    """@brief 顶层必须是对象而非数组或标量 / The top level must be an object, not an array or scalar.

    @param source 顶层非对象的 JSONC 文本 / JSONC text whose top level is not an object.
    @return None / None.
    """

    with pytest.raises(ValueError, match="top-level JSONC value must be an object"):
        parse(source)


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize(
    "token",
    ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"),
)
def test_parsers_reject_non_standard_numeric_constants(
    parse: Callable[[str], Mapping[str, object]], token: str
) -> None:
    """@brief Python 默认宽容的 NaN/Infinity 不是合法 JSON / NaN and Infinity accepted by Python by default are not valid JSON.

    @param token 非标准数值 token / Non-standard numeric token.
    @return None / None.
    """

    with pytest.raises(
        ValueError,
        match="(?:non-standard JSON numeric constant|non-finite JSON numeric value)",
    ):
        parse(f'{{"value": {token}}}')


@pytest.mark.parametrize("load", LOADERS)
def test_loaders_read_utf8_file(
    load: Callable[[Path], Mapping[str, object]], tmp_path: Path
) -> None:
    """@brief load 只读取显式传入的 UTF-8 文件 / load reads only its explicit UTF-8 file.

    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    path = tmp_path / "config.json"
    path.write_text('{"name": "雾萌" // comment\n}', encoding="utf-8")

    assert load(path) == {"name": "雾萌"}


@pytest.mark.parametrize("load", LOADERS)
def test_loaders_wrap_file_read_errors(
    load: Callable[[Path], Mapping[str, object]], tmp_path: Path
) -> None:
    """@brief load 将文件读取失败转换为解析错误 / load turns file-read failures into decode errors.

    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    missing_path = tmp_path / "missing.json"

    with pytest.raises(ValueError, match="cannot read JSONC file"):
        load(missing_path)
