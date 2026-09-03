"""Skill 解析与扫描：读取 hermes 开发的 skill（SKILL.md + YAML front-matter）。

hermes skill 目录结构（见各 skill 实际布局）：
    <skills_dir>/<category>/<skill_name>/SKILL.md   （category 如 devops/creative/...）
    <skill_name>/scripts/*.py
    <skill_name>/references/*
SKILL.md 头部是标准 YAML front-matter，含 name/description/version/author/metadata.hermes.tags、
metadata.hermes.related_skills。description 利于模型做工具选型。

本模块仅做解析与扫描，不依赖任何运行时；errors 情况下返回空/default，绝不抛断主流程。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


@dataclass
class SkillSpec:
    name: str
    description: str
    tags: list[str]
    related_skills: list[str]
    body: str
    path: Path
    scripts: list[str] = field(default_factory=list)

    def to_tool_desc(self) -> str:
        """生成给 function_tool 用的描述。"""
        base = self.description or f"技能 {self.name}"
        if self.related_skills:
            base += f"。相关技能: {', '.join(self.related_skills)}"
        return base


def _parse_skill_dir(
    skill_dir: Path, category: str | None = None
) -> SkillSpec | None:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    text = md.read_text(encoding="utf-8", errors="replace")
    meta: dict = {}
    body = text
    m = _FRONT_MATTER_RE.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        body = text[m.end():]
    hermes_meta = (meta.get("metadata") or {}).get("hermes") or {}
    name = meta.get("name") or skill_dir.name
    scripts_dir = skill_dir / "scripts"
    scripts = (
        [p.name for p in sorted(scripts_dir.glob("*")) if p.is_file() and p.name.endswith(".py")]
        if scripts_dir.is_dir()
        else []
    )
    return SkillSpec(
        name=str(name),
        description=str(meta.get("description") or ""),
        tags=list(hermes_meta.get("tags") or []),
        related_skills=list(hermes_meta.get("related_skills") or []),
        body=body,
        path=skill_dir,
        scripts=scripts,
    )


def scan_skills(skills_dir: str, enabled: list[str] | None = None) -> dict[str, SkillSpec]:
    """扫描 skills 目录，返回 {name: SkillSpec}。

    skills 目录可能有 category 子目录（devops/...）也可能 skill 直接在根下，两种都兼容。
    - enabled 为 None/空: 全部启用；否则仅启用白名单内的（按 name）。
    """
    root = Path(skills_dir)
    result: dict[str, SkillSpec] = {}
    if not root.is_dir():
        return result
    # 兼容: 直接遍历到第二层找含 SKILL.md 的目录
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # 情形1: <root>/<skill>/SKILL.md
        spec = _parse_skill_dir(child)
        if spec is not None:
            result[spec.name] = spec
            continue
        # 情形2: <root>/<category>/<skill>/SKILL.md
        for sub in sorted(child.iterdir()):
            if sub.is_dir():
                sub_spec = _parse_skill_dir(sub, category=child.name)
                if sub_spec is not None:
                    result[sub_spec.name] = sub_spec
    if enabled:
        result = {k: v for k, v in result.items() if k in enabled}
    return result