"""Skill-as-tool：把 hermes 开发的 skill 暴露为可通过 agent 调用的 function_tool。

三种工具：
  list_skills(tag?)       列出可用技能及其用途（供模型选型）
  load_skill(name)        读取某 skill 的完整 SKILL.md 指引（body）
  run_skill_script(...)   运行某 skill scripts/ 下的 Python 脚本并回显输出

skill 内容来自 skill_loader 扫描的目录（volume 挂载 /srv/gateway/skills 或拷入镜像）。
tools 通过全局注册表取得扫描结果，每次 agent 构造时传入。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from agents import function_tool

from . import config, logger
from .skill_loader import SkillSpec, scan_skills

# 全局 skill 注册表：在 app 启动时 refresh_skill_registry() 填充，供 tools 闭包读取。
_registry: dict[str, SkillSpec] = {}


def refresh_skill_registry() -> None:
    """重新扫描 skill 目录并更新注册表（启动时调用；volume 改动可加定时/手动刷新）。

    扫描源 = 只读预装目录(config.SKILLS_DIR) + 可写安装目录(config.SKILLS_INSTALL_DIR)，
    后装技能（/skills/install 落 install_dir）也可见。
    """
    global _registry
    dirs = [config.SKILLS_DIR]
    install_dir = getattr(config, "SKILLS_INSTALL_DIR", None)
    if install_dir:
        dirs.append(install_dir)
    merged: dict[str, SkillSpec] = {}
    for d in dirs:
        merged.update(scan_skills(d, config.SKILLS_ENABLED))
    _registry = merged
    logger.log("info", "skills_refreshed", {"count": len(_registry), "names": list(_registry)})


def get_install_dir() -> str:
    """返回可写技能安装目录（不存在则创建）。"""
    return getattr(config, "SKILLS_INSTALL_DIR", None) or config.SKILLS_DIR


def list_skill_specs() -> list[dict]:
    """返回所有可用技能的简化 dict（name/description/scripts），供 HTTP API 使用。"""
    return [
        {
            "name": s.name,
            "description": s.description,
            "scripts": s.scripts,
        }
        for s in sorted(_registry.values(), key=lambda x: x.name)
    ]


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_]*$")


def install_skill_zip(data: bytes, force_name: str | None = None) -> dict:
    """把技能 ZIP 安装到 SKILLS_DIR 并热刷新注册表。

    结构约定（与预装技能一致）：
      <name>/SKILL.md                     —— 必需
      <name>/scripts/*.py                 —— 可选
      <name>/references/*  <name>/templates/*   —— 可选
    ZIP 根可以是技能名目录，也可以不带顶层目录（自动以 SKILL.md 的父目录为技能根）。
    高危：路径穿越防护 —— 解压时把每个成员路径 clean 后必须仍落在目标目录内。
    返回 {name, path, scripts...}。
    """
    root = Path(get_install_dir())
    root.mkdir(parents=True, exist_ok=True)

    # 1. 预扫描 zip 顶层，判断是否带技能名目录
    skill_name: str | None = None
    members = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        members = [n for n in z.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效的 ZIP: {e}") from e

    if not members:
        raise ValueError("ZIP 为空")

    # 找 SKILL.md 所在顶层目录（技能名）
    for m in members:
        if m.endswith("/SKILL.md") or m == "SKILL.md":
            parts = m.split("/")
            if len(parts) == 1:
                # SKILL.md 在 zip 根：技能名取显式 name，或另一个成员/其他目录的顶层名
                skill_name = force_name
                break
            if len(parts) >= 2:
                skill_name = parts[-2]
                break
    if not skill_name:
        # 无 SKILL.md 顶层目录信息 → 取第一个非 SKILL.md 成员的最高层目录
        for m in members:
            if m == "SKILL.md":
                continue
            parts = m.split("/")
            if len(parts) >= 1 and parts[0] not in ("SKILL.md",):
                skill_name = parts[0]
                break
    if not skill_name:
        raise ValueError("无法确定技能名（请用 <skillname>/SKILL.md 结构或指定 name）")

    # 显式 name 覆盖（去扩展名）
    final_name = force_name or skill_name or members[0].split("/")[0]
    final_name = final_name.rstrip("/")
    if final_name.endswith(".zip"):
        final_name = final_name[:-4]
    if not _SKILL_NAME_RE.match(final_name):
        raise ValueError(f"非法技能名: {final_name!r}（只能含小写字母/数字/-/_）")

    dest_dir = root / final_name
    # 覆盖式安装：先清旧目录（安全：只清目标技能目录）
    import shutil
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        for n in z.namelist():
            if n.endswith("/"):
                continue
            # 归一化：去掉可能的顶层技能目录前缀（如果 zip 根是 <name>/）
            rel = n
            parts = n.split("/")
            if len(parts) >= 2 and parts[0] == skill_name:
                rel = "/".join(parts[1:])
            if not rel:
                continue
            # 路径穿越防护
            target = (dest_dir / rel).resolve()
            if not str(target).startswith(str(dest_dir.resolve()) + os.sep) and target != dest_dir.resolve():
                raise ValueError(f"非法的 zip 路径: {n}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as dst:
                dst.write(src.read())
        # 清理可能的 __MACOSX 等
        for extra in ["__MACOSX"]:
            ep = dest_dir / extra
            if ep.exists():
                shutil.rmtree(ep)
    except Exception as e:
        raise ValueError(f"解压失败: {e}") from e

    # 校验 SKILL.md 存在
    if not (dest_dir / "SKILL.md").is_file():
        # 如果解压后 SKILL.md 不在技能根（可能在子目录），扫描查找
        found = list(dest_dir.rglob("SKILL.md"))
        if not found:
            raise ValueError("ZIP 内缺少 SKILL.md")
        # 把 SKILL.md 所在子目录视为技能根 → 若结构是 <name>/xxx/SKILL.md，重建
        raise ValueError("ZIP 结构应为 <skillname>/SKILL.md 或根目录 SKILL.md")

    # 热刷新注册表
    refresh_skill_registry()
    logger.log("info", "skill_installed", {"name": final_name, "path": str(dest_dir)})

    new_spec = _registry.get(final_name)
    return {
        "name": final_name,
        "path": str(dest_dir),
        "installed": True,
        "skill_count_after": len(_registry),
        "scripts": list(new_spec.scripts) if new_spec else [],
    }


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