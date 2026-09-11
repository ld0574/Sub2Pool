#!/usr/bin/env python3
"""Upgrade an existing CPA installation without guessing its storage or networks."""
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

IMAGE = "sub2pool-cpa:local"
DATA_DIR = "/app/data"


class DeploymentError(Exception):
    pass


def run(*args, capture=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        # Never print captured stdout: Compose config / inspect contain credentials.
        detail = (result.stderr or "").strip()
        raise DeploymentError(f"命令失败：{shlex.join(args)}" + (f"\n{detail}" if detail else ""))
    return result.stdout.strip() if capture else ""


def inspect_container(container_id):
    return json.loads(run("docker", "inspect", container_id))[0]


def labels(container):
    return container.get("Config", {}).get("Labels") or {}


def existing_containers():
    ids = run("docker", "ps", "-aq", "--filter", "label=com.docker.compose.service=app").split()
    return json.loads(run("docker", "inspect", *ids)) if ids else []


def select_deployment(repo, options, containers):
    if options:
        config = json.loads(run("docker", "compose", *options, "config", "--format", "json"))
        project = config.get("name")
        matches = [c for c in containers if labels(c).get("com.docker.compose.project") == project]
    else:
        matches = [c for c in containers
                   if labels(c).get("com.docker.compose.project.working_dir")
                   and Path(labels(c)["com.docker.compose.project.working_dir"]).resolve() == repo]
    matches = [c for c in matches if labels(c).get("com.docker.compose.oneoff", "false").lower() != "true"]
    if len(matches) != 1:
        raise DeploymentError("无法唯一确定此部署的现有 app 容器；请传入原部署的 -p、-f 和 --env-file 参数。此脚本不用于首次安装。")
    container = matches[0]
    project = labels(container)["com.docker.compose.project"]
    if not options:
        files = labels(container).get("com.docker.compose.project.config_files", "").split(",")
        if not all(p and Path(p).is_file() for p in files):
            raise DeploymentError("原 Compose 文件不存在；不会回退到仓库默认配置。请恢复原配置或显式传入正确的 -f 参数。")
        options = ["--project-directory", str(repo), "-p", project]
        for path in files:
            options += ["-f", path]
        env_files = labels(container).get("com.docker.compose.project.environment_file", "")
        for path in filter(None, env_files.split(",")):
            if not Path(path).is_file():
                raise DeploymentError("原 Compose 环境文件不存在，请恢复原文件。")
            options += ["--env-file", path]
    return ["docker", "compose", *options], container


def validate(config, container):
    project = labels(container).get("com.docker.compose.project")
    if config.get("name") != project:
        raise DeploymentError("Compose 项目名与现有容器不一致，拒绝发版。")
    app = config.get("services", {}).get("app", {})
    if app.get("image") != IMAGE:
        raise DeploymentError(f"app.image 必须为 {IMAGE}；请使用保留原挂载和网络的镜像覆盖文件。")
    if not container.get("State", {}).get("Running"):
        raise DeploymentError("现有 app 未运行，请先恢复服务，再使用发版脚本。")
    actual_env = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
    target_env = app.get("environment") or {}
    secret = actual_env.get("DJANGO_SECRET_KEY")
    if not secret or target_env.get("DJANGO_SECRET_KEY") != secret:
        raise DeploymentError("DJANGO_SECRET_KEY 与现有容器不一致，拒绝发版。请沿用原环境文件；密钥不会输出。")
    if actual_env.get("PINCH_DATA_DIR") != DATA_DIR or target_env.get("PINCH_DATA_DIR") != DATA_DIR:
        raise DeploymentError("此脚本只支持 PINCH_DATA_DIR=/app/data 的现有部署。")

    actual_mounts = {}
    for mount in container.get("Mounts", []):
        source = mount.get("Name") if mount["Type"] == "volume" else mount.get("Source")
        actual_mounts[mount["Destination"]] = (mount["Type"], source, mount.get("RW", True))
    target_mounts = {}
    for mount in app.get("volumes", []):
        kind, source = mount["type"], mount.get("source")
        if kind == "volume":
            volume = config.get("volumes", {}).get(source, {})
            source = volume.get("name")
        if kind not in ("volume", "bind") or not source:
            raise DeploymentError("不支持匿名卷或无法识别的挂载，请显式配置持久化数据源。")
        target_mounts[mount["target"]] = (kind, source, not mount.get("read_only", False))
    if actual_mounts != target_mounts or DATA_DIR not in target_mounts or not target_mounts[DATA_DIR][2]:
        raise DeploymentError("Compose 挂载与现有容器不一致，拒绝发版。请保留原数据卷名称、绑定路径和读写模式。")

    mode = app.get("network_mode")
    if mode:
        if mode not in ("host", "none") or mode != container["HostConfig"]["NetworkMode"]:
            raise DeploymentError("网络模式不一致或不支持，拒绝发版。")
    else:
        network_defs = config.get("networks", {})
        target_networks = {network_defs.get(key, {}).get("name") for key in app.get("networks", {})}
        actual_networks = set(container.get("NetworkSettings", {}).get("Networks", {}))
        if not target_networks or None in target_networks or target_networks != actual_networks:
            raise DeploymentError("Compose 网络与现有容器不一致，拒绝发版。请将手动连接的 CPA 等网络写入原 Compose 覆盖文件。")
    health = app.get("healthcheck") or {}
    if health.get("disable") or not health.get("test") or health["test"][0] == "NONE":
        raise DeploymentError("app 必须配置健康检查，才能确认发版成功。")


def read_and_validate(compose, container):
    config = json.loads(run(*compose, "config", "--format", "json"))
    validate(config, container)
    data_mount = next(m for m in container["Mounts"] if m["Destination"] == DATA_DIR)
    source = data_mount.get("Name") or data_mount["Source"]
    users = set(run("docker", "ps", "-q", "--no-trunc", "--filter", f"volume={source}").split())
    if users - {container["Id"]}:
        raise DeploymentError("还有其他运行中的容器使用此数据源，无法保证停写备份；请先核对共享挂载。")
    return config


def verify_backup(data_path):
    db = data_path / "pinche.sqlite3"
    if not db.is_file() or not db.stat().st_size:
        raise DeploymentError("备份中没有有效的 pinche.sqlite3，停止发版。")
    # Check the stopped-container copy, including WAL; never modify the source DB.
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise DeploymentError("备份数据库完整性检查失败，停止发版。")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def deploy(repo, options, check_only=False):
    stage = "检查原部署"
    stopped = False
    replacing = False
    old = None
    backup = None
    try:
        if run("git", "branch", "--show-current") != "cpa":
            raise DeploymentError("请在 cpa 分支运行此脚本。")
        run("docker", "compose", "version")
        run("docker", "info")
        compose, old = select_deployment(repo, options, existing_containers())
        read_and_validate(compose, old)
        print("沿用部署配置：" + shlex.join(compose), flush=True)
        print("已核对现有数据挂载、网络和加密密钥。", flush=True)
        for mount in old["Mounts"]:
            print(f"挂载：{mount.get('Name') or mount.get('Source')} -> {mount['Destination']}", flush=True)
        print("网络：" + ", ".join(old["NetworkSettings"]["Networks"]), flush=True)
        if check_only:
            print("检查通过；未拉取代码、构建镜像、停止或替换容器。", flush=True)
            return 0

        stage = "拉取 cpa 最新代码"
        print(f"[1/5] {stage}", flush=True)
        run("git", "pull", "--ff-only", capture=False)
        config = read_and_validate(compose, old)

        # Keep the running image addressable even after the local tag is rebuilt.
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        rollback_image = f"sub2pool-cpa:before-{timestamp}"
        run("docker", "image", "tag", old["Image"], rollback_image)
        stage = "构建新镜像（旧服务继续运行）"
        print(f"[2/5] {stage}", flush=True)
        run("docker", "build", "-t", IMAGE, ".", capture=False)

        stage = "再次核对部署状态"
        ids = run(*compose, "ps", "-a", "-q", "app").split()
        if ids != [old["Id"]]:
            raise DeploymentError("构建期间 app 容器已变化，停止发版。")
        current = inspect_container(old["Id"])
        latest = read_and_validate(compose, current)
        if latest != config:
            raise DeploymentError("构建期间 Compose 配置已变化，停止发版。")

        stage = "停止写入并备份完整数据目录"
        print(f"[3/5] {stage}", flush=True)
        backup_root = repo.parent / f"{repo.name}-backups"
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=timestamp + "-", dir=backup_root))
        print(f"备份目录：{backup}（包含密钥配置，请勿公开）", flush=True)
        write_json(backup / "container.json", current)
        write_json(backup / "compose.resolved.json", config)
        (backup / "previous-image.txt").write_text(rollback_image + "\n")
        data_path = backup / "data"
        data_path.mkdir(mode=0o700)
        stopped = True
        run("docker", "stop", "--time", "45", old["Id"])
        if inspect_container(old["Id"])["State"]["Running"]:
            raise DeploymentError("旧容器尚未停止，不能创建一致备份。")
        run("docker", "cp", f"{old['Id']}:{DATA_DIR}/.", str(data_path))
        verify_backup(data_path)
        (backup / "BACKUP_COMPLETE").write_text("完整数据目录已复制；SQLite quick_check 通过。\n")
        print("备份完成，SQLite 完整性检查通过。", flush=True)

        stage = "替换 app 并等待健康检查"
        print(f"[4/5] {stage}", flush=True)
        replacing = True
        run(*compose, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
            "--wait", "--wait-timeout", "300", "app", capture=False)
        stage = "核对新容器数据卷、网络及健康状态"
        print(f"[5/5] {stage}", flush=True)
        ids = run(*compose, "ps", "-a", "-q", "app").split()
        if len(ids) != 1:
            raise DeploymentError("发版后 app 容器数量异常。")
        new = inspect_container(ids[0])
        validate(config, new)
        if new.get("State", {}).get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("新容器尚未健康。")
        run(*compose, "ps", "app", capture=False)
        print(f"发版成功。备份：{backup}\n旧镜像：{rollback_image}", flush=True)
        return 0
    except (DeploymentError, OSError, ValueError, sqlite3.Error, KeyboardInterrupt) as error:
        print(f"\n发版未完成：{stage}\n{error}", file=sys.stderr)
        if replacing:
            print("新版本可能已执行迁移，不自动回滚数据库或启动旧镜像。请检查 app 状态和日志。", file=sys.stderr)
        elif stopped and old:
            print("尚未启动新版本，尝试恢复原容器。", file=sys.stderr)
            try:
                run("docker", "start", old["Id"])
                print("已启动原容器；请核对其健康状态。", file=sys.stderr)
            except (DeploymentError, OSError) as restart_error:
                print(str(restart_error), file=sys.stderr)
        else:
            print("尚未停止或替换原服务。", file=sys.stderr)
        if backup:
            print(f"备份目录：{backup}；仅 BACKUP_COMPLETE 存在时表示备份已完成。", file=sys.stderr)
        return 1


def main():
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 或更新版本（Debian 11 及更新版本默认满足）。", file=sys.stderr)
        return 1
    if any(option in ("-h", "--help") for option in sys.argv[1:]):
        print("用法：scripts/deploy-cpa.sh [--check] [Compose 全局参数]\n"
              "默认沿用当前仓库现有 app 容器的 Compose 文件、项目名和可追溯的环境文件。\n"
              "--check 只核对当前配置，不拉取、构建、备份或替换容器。\n"
              "例：scripts/deploy-cpa.sh -p sub2pool -f compose.yaml -f compose.recover-data.yaml\n"
              "仅升级现有实例；需 Python 3.9+、Git、Docker Compose（支持 --wait）。")
        return 0
    def interrupted(signum, frame):
        raise KeyboardInterrupt("收到终止信号")
    signal.signal(signal.SIGTERM, interrupted)
    os.umask(0o077)
    repo = Path(__file__).resolve().parent.parent
    os.chdir(repo)
    try:
        lock_path = Path(run("git", "rev-parse", "--git-path", "cpa-deploy.lock"))
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("已有发版脚本运行，请勿重复执行。")
            return deploy(repo, [arg for arg in sys.argv[1:] if arg != "--check"],
                          check_only="--check" in sys.argv[1:])
    except (DeploymentError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
