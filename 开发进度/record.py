"""Record a project milestone for the local development dashboard."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile


DIRECTORY = Path(__file__).resolve().parent
DATA = DIRECTORY / "进度.js"
PREFIX = "window.receiveProgress("
SUFFIX = ");\n"
STATES = ("待开发", "开发中", "待验证", "受阻", "已完成")


@contextmanager
def writer_lock(path):
    import msvcrt
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def read_data(path):
    if not path.exists():
        return {"revision": 0, "tasks": {}}
    payload = path.read_text(encoding="utf-8")
    if not payload.startswith(PREFIX) or not payload.endswith(SUFFIX):
        raise ValueError("进度文件格式异常，原记录已保留")
    return json.loads(payload[len(PREFIX):-len(SUFFIX)])


def record(key, event, directory=DIRECTORY):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
        raise ValueError("任务编号可使用小写字母、数字或连字符")
    if event["state"] not in STATES or not event["note"].strip():
        raise ValueError("请提供有效的状态和进展说明")
    path = directory / DATA.name
    with writer_lock(directory / ".write.lock"):
        board = read_data(path)
        task = board["tasks"].setdefault(key, [])
        if not task and not all(event.get(field) for field in ("title", "group")):
            raise ValueError("新任务需要标题和所属模块")
        moment = datetime.now().astimezone().isoformat(timespec="seconds")
        entry = {**event, "time": event.get("time") or moment, "sequence": board["revision"] + 1}
        task.append(entry)
        board["revision"] += 1
        board["updated"] = moment
        payload = PREFIX + json.dumps(board, indent=2, ensure_ascii=False, allow_nan=False) + SUFFIX
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".draft-", delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temp.unlink(missing_ok=True)
                raise
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="追加一条开发进展，已打开的面板会自动更新")
    parser.add_argument("key", help="沿用已有任务编号，或创建新编号")
    parser.add_argument("state", choices=STATES)
    parser.add_argument("note", help="这一步实际做了什么或发现了什么")
    for field in ("title", "group", "issue", "next", "check"):
        parser.add_argument("--" + field)
    parser.add_argument("--evidence", nargs=2, action="append", metavar=("名称", "文件路径"))
    args = vars(parser.parse_args())
    key = args.pop("key")
    evidence = args.pop("evidence")
    event = {k: v for k, v in args.items() if v is not None}
    if evidence:
        event["links"] = []
        for label, filename in evidence:
            path = Path(filename).resolve(strict=True)
            event["links"].append({"label": label, "url": path.as_uri()})
    record(key, event)


if __name__ == "__main__":
    main()
