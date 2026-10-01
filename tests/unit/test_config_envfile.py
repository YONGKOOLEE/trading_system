""".env 파서 (R-12, R-11): 값은 어떤 경로로도 출력되지 않는다."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_system.config.envfile import EnvFileError, Secret, SecretStore, parse_env_file, parse_env_text

SECRET = "FAKE-SECRET-VALUE-123"


def test_parses_comments_blank_lines_and_quotes() -> None:
    store = parse_env_text('# 주석\n\nTRADE_MODE=PAPER\nKIWOOM_PAPER_APP_KEY="quoted value"\nKIWOOM_PAPER_X=\'single\'\nEMPTY=\n')
    assert store.get("TRADE_MODE").reveal() == "PAPER"
    assert store.get("KIWOOM_PAPER_APP_KEY").reveal() == "quoted value"
    assert store.get("KIWOOM_PAPER_X").reveal() == "single"
    assert store.get("EMPTY").reveal() == ""
    assert store.keys() == ["TRADE_MODE", "KIWOOM_PAPER_APP_KEY", "KIWOOM_PAPER_X", "EMPTY"]


def test_value_keeps_hash_and_equals_inside() -> None:
    store = parse_env_text("K=ab#cd=ef\n")
    assert store.get("K").reveal() == "ab#cd=ef"


def test_crlf_and_bom() -> None:
    store = parse_env_text("﻿A=1\r\nB=2\r\n")
    assert store.keys() == ["A", "B"]


def test_secret_never_leaks_through_str_repr_format() -> None:
    s = Secret(SECRET)
    for text in (str(s), repr(s), f"{s}", f"{s!r}", "%s" % s, format(s, ">20")):
        assert SECRET not in text
    store = parse_env_text(f"KIWOOM_PAPER_APP_KEY={SECRET}\n")
    assert SECRET not in repr(store) and SECRET not in str(store)
    assert SECRET not in repr(store.get("KIWOOM_PAPER_APP_KEY"))


def test_secret_is_not_hashable_or_comparable_by_value() -> None:
    with pytest.raises(TypeError):
        hash(Secret(SECRET))
    assert Secret(SECRET) != SECRET


@pytest.mark.parametrize("text", [
    f"NO_EQUALS_{SECRET}\n",
    f"lower_case={SECRET}\n",
    f"1BAD={SECRET}\n",
    f"BAD KEY={SECRET}\n",
    f"=nokey{SECRET}\n",
])
def test_malformed_lines_rejected_without_echo(text: str) -> None:
    with pytest.raises(EnvFileError) as exc:
        parse_env_text(text)
    assert SECRET not in str(exc.value)
    assert "1" in str(exc.value)  # 줄 번호는 알려 준다


def test_duplicate_key_rejected_without_value() -> None:
    with pytest.raises(EnvFileError) as exc:
        parse_env_text(f"A={SECRET}\nA={SECRET}\n")
    assert SECRET not in str(exc.value)
    assert "A" in str(exc.value)


def test_missing_key_lookup() -> None:
    store = SecretStore({})
    assert not store.has("X")
    with pytest.raises(KeyError):
        store.get("X")


def test_file_not_found_reports_name_only(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError) as exc:
        parse_env_file(tmp_path / ".env.paper")
    assert ".env.paper" in str(exc.value)


def test_parse_file(tmp_path: Path) -> None:
    p = tmp_path / ".env.paper"
    p.write_text(f"TRADE_MODE=PAPER\nKIWOOM_PAPER_APP_KEY={SECRET}\n", encoding="utf-8")
    store = parse_env_file(p)
    assert store.get("KIWOOM_PAPER_APP_KEY").reveal() == SECRET
