"""Skill-as-tool：把 hermes 开发的 skill 暴露为可通过 agent 调用的 function_tool。

三种工具：
  list_skills(tag?)       列出可用技能及其用途（供模型选型）
  load_skill(name)        读取某 skill 的完整 SKILL.md 指引（body）
  run_skill_script(...)   运行某 skill scripts/ 下的 Python 脚本并回显输出

skill 内容来自 skill_loader 扫描的目录（volume 挂载 /srv/gateway/skills 或拷入镜像）。
tools 通过全局注册表取得扫描结果，每次 agent 构造时传入。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from agents import function_tool

from . import config, logger
from .skill_loader import SkillSpec, scan_skills

# 全局 skill 注册表：在 app 启动时 refresh_skill_registry() 填充，供 tools 闭包读取。
_registry: dict[str, SkillSpec] = {}


def refresh_skill_registry() -> None:
    """重新扫描 skill 目录并更新注册表（启动时调用；volume 改动可加定时/手动刷新）。"""
    global _registry
    _registry = scan_skills(config.SKILLS_DIR, config.SKILLS_ENABLED)
    logger.log("info", "skills_refreshed", {"count": len(_registry), "names": list(_registry)})


def build_skill_tools() -> list[Any]:
    """构造 skill 相关 function_tool 列表，供 Agent(tools=[...]) 使用。"""

    @function_tool
    def list_skills(tag: str | None = None) -> str:
        """列出当前可用的所有技能及其用途。tag 可传如 'devops' 过滤；模型据此选择要用的技能。"""
        if not _registry:
            return "当前没有可用技能（skills 目录为空或未扫描）。"
        lines = []
        for spec in sorted(_registry.values(), key=lambda s: s.name):
            if tag and tag not in spec.tags:
                continue
            scripts = f"scripts: {', '.join(spec.scripts)}" if spec.scripts else ""
            lines.append(f"- {spec.name}: {spec.description} {scripts}".rstrip())
        return "\n".join(lines)

    @function_tool
    def load_skill(skill_name: str) -> str:
        """读取一个技能的完整操作指引(SKILL.md 正文)。调用前应先 list_skills 确定技能名。"""
        spec = _registry.get(skill_name)
        if spec is None:
            return f"技能不存在: {skill_name}。可用技能见 list_skills。"
        head = f"# 技能 {spec.name}\n"
        if spec.scripts:
            head += f"\n可用脚本: {', '.join(spec.scripts)}\n"
        return head + spec.body

    @function_tool
    def run_skill_script(skill_name: str, script: str, args: list[str] | None = None) -> str:
        """运行某技能 scripts/ 目录下的 Python 脚本并返回其 stdout/stderr（非交互、超时控制）。"""
        spec = _registry.get(skill_name)
        if spec is None:
            return f"技能不存在: {skill_name}。"
        script_path = spec.path / "scripts" / script
        if not script_path.is_file():
            return f"脚本不存在: {skill_name}/scripts/{script}。可用脚本: {', '.join(spec.scripts) or '无'}。"
        argv = [sys_executable(), str(script_path), *(args or [])]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=config.SKILL_SCRIPT_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return f"脚本超时(>{config.SKILL_SCRIPT_TIMEOUT_S}s): {script}"
        except Exception as e:  # noqa: BLE001
            return f"脚本执行出错: {e!r}"
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        ret = f"(exit {proc.returncode})"
        if out:
            ret += f"\nstdout:\n{out}"
        if err:
            ret += f"\nstderr:\n{err}"
        return ret

    return [list_skills, load_skill, run_skill_script]


def sys_executable() -> str:
    return os.environ.get("PYTHON", "python3")