"""迁移 SQL 的 PostgreSQL 版本兼容性静态检查。

背景：线上库是 PostgreSQL 9.x，而开发机通常是 14+。高版本语法在开发机上跑得通、
到上线才报错，是这个项目反复返工的来源。本测试把「不许出现的高版本语法」钉死，
让问题在 CI 阶段就暴露，而不是在部署窗口里。

覆盖范围：``backend/migrations/*.sql`` 全部文件（不只 iOS 虚拟机那份）——
既有文件已经过上线检验，正好当作回归基线。
"""
import re
from pathlib import Path

import pytest


_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# 语法 → (引入版本, 替代写法)。全部按「不区分大小写、允许中间多个空白」匹配。
_FORBIDDEN = {
    r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS": (
        "9.5+",
        "改用 DO 块 + pg_class 目录检查",
    ),
    r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS": (
        "9.5+",
        "改用 DO 块 + pg_class 目录检查",
    ),
    r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS": (
        "9.6+",
        "改用 DO 块 + EXCEPTION duplicate_column",
    ),
    r"DROP\s+COLUMN\s+IF\s+EXISTS": (
        "9.0+ 可用但语义危险",
        "迁移不应删列",
    ),
    r"\bGENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY": (
        "10+",
        "主键由应用层生成短 id",
    ),
    r"\bON\s+CONFLICT\b": (
        "9.5+",
        "改用先 SELECT 再 INSERT，或交给 ORM 层处理",
    ),
    r"CREATE\s+TABLE\s+.*\bPARTITION\s+BY\b": (
        "10+",
        "不使用声明式分区",
    ),
    r"\bALTER\s+TABLE\s+\S+\s+ALTER\s+COLUMN\s+\S+\s+ADD\s+GENERATED": (
        "12+",
        "不使用生成列",
    ),
}


def _sql_files():
    return sorted(_MIGRATIONS.glob("*.sql"))


def _strip_comments(text: str) -> str:
    """去掉 ``--`` 行注释：文件头的兼容性说明里会成段引用这些被禁语法。"""
    return "\n".join(
        line.split("--", 1)[0] for line in text.splitlines()
    )


def test_migrations_directory_exists():
    assert _MIGRATIONS.is_dir()
    assert _sql_files(), "migrations 目录下应当有 SQL 文件"


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_no_high_version_syntax(path: Path):
    body = _strip_comments(path.read_text(encoding="utf-8"))
    hits = []
    for pattern, (version, hint) in _FORBIDDEN.items():
        for m in re.finditer(pattern, body, re.IGNORECASE):
            line_no = body[: m.start()].count("\n") + 1
            hits.append(
                f"{path.name}:{line_no} 用到了 {version} 才有的语法 "
                f"「{m.group(0)}」——{hint}"
            )
    assert not hits, (
        "迁移 SQL 使用了线上 PostgreSQL 9.x 不支持的语法：\n  "
        + "\n  ".join(hits)
    )


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_uses_if_not_exists_for_create_table(path: Path):
    """建表必须幂等，否则重复执行会中断整份迁移。"""
    body = _strip_comments(path.read_text(encoding="utf-8"))
    bad = [
        m.group(0)
        for m in re.finditer(r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)\S+", body, re.IGNORECASE)
    ]
    assert not bad, f"{path.name} 有非幂等的 CREATE TABLE：{bad}"


def test_ios_sim_migration_declares_version_floor():
    """iOS 虚拟机那份必须在文件头写明兼容性下限，供后来人核对。"""
    path = _MIGRATIONS / "ios_sim_v1.sql"
    head = path.read_text(encoding="utf-8")[:2000]
    assert "PostgreSQL 9.2" in head
    assert "CREATE INDEX IF NOT EXISTS" in head, "应当列出刻意不使用的语法"


def test_ios_sim_migration_qualifies_index_schema():
    """索引目标要显式写 public.，避免 search_path 不同时建到别的 schema。"""
    body = _strip_comments(
        (_MIGRATIONS / "ios_sim_v1.sql").read_text(encoding="utf-8")
    )
    creates = re.findall(r"CREATE\s+INDEX\s+\S+\s+ON\s+(\S+)", body, re.IGNORECASE)
    assert creates, "应当有索引创建语句"
    for target in creates:
        assert target.startswith("public."), f"索引目标未限定 schema：{target}"
