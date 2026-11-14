"""冻结类别表: "YOLO 类别号 <-> 奕子名" 的唯一真值源 (修掉类别号漂移 bug)。

为什么要这张表 (2026-10-12 取证结论)
------------------------------------
旧实现 `train_yolo/collect_data_agent/ledger.py: class_id()` 每遇到新类就
`classes.append(name); classes.sort(); return classes.index(name)` ——
**同一帧内先写入的标签行, 会因为后面出现新类、排序重排而下标整体位移**:

    frame_222513 的对象顺序 = [崔丝塔娜, 约里克, 奥恩, 蔚, 约里克]
    写第 1 行时 classes=[ball,卡蜜尔,崔丝塔娜]            → 崔丝塔娜=2
    写第 2 行时 classes=[ball,卡蜜尔,崔丝塔娜,约里克]      → 约里克=3
    写第 3 行时插入"奥恩"(U+5965 < U+5D14) → 崔丝塔娜/约里克整体 +1
    实际落盘: 2,3,2,5,4 —— 与正确值 4,5,3,6,5 对照, 5 行全错。

实测影响: 当前数据集 18 个 unit 框里 **10 个类别号是错的** (卡蜜尔被写成 ball);
历史 32 帧 148 个实例中 86 个错 (58%)。而 `review.audit()` 只查"下标越界/坐标越界",
所以自检一直报 `problems 0`。

口径 (用户 2026-10-12 确认)
---------------------------
合并拉克丝皮肤: `units.json` 的 65 个 unit + `extraUnits` 的 9 个 "拉克丝 <皮肤/羁绊>"
统一归到 "拉克丝" → **65 类**。ball 不进表 (任务目标是"我方奕子的位置 + 类别")。

用法
----
    from train_yolo.class_table import CLASS_NAMES, name_to_id, id_to_name
    idx = name_to_id("卡蜜尔")        # → 41 (与出现顺序无关, 永远稳定)
    name = id_to_name(41)             # → "卡蜜尔"

命令行:
    python -m train_yolo.class_table                # 打印表 + 自检
    python -m train_yolo.class_table --rebuild      # 从爬虫库重建 (会改类别号! 慎用)
    python -m train_yolo.class_table --add 新奕子    # 只在末尾追加 (安全, 不动已有下标)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = Path(__file__).resolve().parent / "classes.json"

# 皮肤/羁绊变体 → 基础名 (用户口径: 合并拉克丝)
_SKIN_RE = re.compile(r"^(拉克丝)\s+\S+$")


def normalize(name: str | None) -> str:
    """把皮肤/羁绊变体名归一到基础名 ("拉克丝 地狱火" → "拉克丝"); 其余原样返回."""
    text = str(name or "").strip()
    hit = _SKIN_RE.match(text)
    return hit.group(1) if hit else text


def library_units_path() -> Path | None:
    """爬虫库里版本号最大的 chess_info/units.json (与 units/db.py 同口径)."""
    base = PROJECT_ROOT / "workspace" / "scrape_info"
    versions = sorted(p for p in base.glob("v*") if p.is_dir())
    return (versions[-1] / "chess_info" / "units.json") if versions else None


def price_map(path: Path | None = None) -> dict[str, int]:
    """类别名 (含别名, 已归一) -> 奕子费用 (1~5 费). 取不到就返回空表, 只影响报表标注。"""
    src = Path(path) if path else library_units_path()
    if src is None or not src.exists():
        return {}
    out: dict[str, int] = {}
    try:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001 - 价格只是报表标注, 坏了不影响闸门
        return out
    for unit in data.get("units") or []:
        cost = unit.get("price")
        if cost is None:
            continue
        for key in (unit.get("name"), *(unit.get("otherNames") or [])):
            if key:
                out[normalize(key)] = int(cost)
    return out


def build_table(path: Path | None = None) -> dict:
    """从爬虫库生成类别表 (只在 --rebuild 时调用; 生成顺序 = units.json 顺序)."""
    src = Path(path) if path else library_units_path()
    if src is None or not src.exists():
        raise FileNotFoundError(f"找不到爬虫库 units.json: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    classes: list[str] = []
    for unit in data.get("units") or []:
        base = normalize(unit.get("name"))
        if base and base not in classes:
            classes.append(base)
    for unit in data.get("extraUnits") or []:      # 皮肤变体: 归并到已存在的基础名就不新增类
        base = normalize(unit.get("name"))
        if base and base not in classes:
            classes.append(base)
    return {"version": src.parent.parent.name, "merge_skins": True, "classes": classes, "extra": []}


@lru_cache(maxsize=1)
def load() -> dict:
    """读冻结表 (进程内缓存一次). 表文件缺失时**报错而不猜** —— 类别号错位比没有数据更糟."""
    if not TABLE_PATH.exists():
        raise FileNotFoundError(
            f"缺少冻结类别表 {TABLE_PATH}; 用 `python -m train_yolo.class_table --rebuild` 生成一次并提交")
    data = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    data.setdefault("extra", [])
    return data


def save(data: dict, path: Path | None = None) -> None:
    target = Path(path) if path else TABLE_PATH
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_names() -> list[str]:
    """全部类别名 (冻结段 + 追加段), 下标即类别号."""
    data = load()
    return list(data.get("classes") or []) + list(data.get("extra") or [])


def num_classes() -> int:
    return len(all_names())


def _alias_map() -> dict:
    """别名 → 正式名 (复用爬虫库的 otherNames, 如 妖姬→乐芙兰); 库不可用时返回空表."""
    try:
        from units import db
        return dict(db.load_db()["unit_alias"])
    except Exception:      # noqa: BLE001 - 标注/数据集构建不该因为库缺失而崩
        return {}


def name_to_id(name: str | None) -> int | None:
    """奕子名 → 类别号; 不在表里返回 None (**不做模糊匹配**: 标错类比不标更糟)."""
    text = normalize(name)
    if not text:
        return None
    names = all_names()
    if text in names:
        return names.index(text)
    alias = _alias_map().get(text)
    if alias and alias in names:
        return names.index(alias)
    return None


def id_to_name(idx: int | None) -> str | None:
    """类别号 → 奕子名; 越界返回 None."""
    if idx is None:
        return None
    names = all_names()
    return names[int(idx)] if 0 <= int(idx) < len(names) else None


def is_known(name: str | None) -> bool:
    return name_to_id(name) is not None


def names_with(extra_names: list[str] | tuple[str, ...] = ()) -> list[str]:
    """冻结表 + 附加热号 (如 --keep-ball 时的 "ball"); 下标即类别号."""
    return all_names() + [n for n in extra_names if n not in all_names()]


def classes_txt_text(extra_names: list[str] | tuple[str, ...] = ()) -> str:
    """classes.txt 内容 (一行一类, 与 ultralytics 约定一致)."""
    return "\n".join(names_with(extra_names)) + "\n"


def data_yaml_text(dataset_dir: str | Path, train: str = "images/train", val: str = "images/val",
                   extra_names: list[str] | tuple[str, ...] = ()) -> str:
    """ultralytics 可直接吃的 data.yaml; path 用绝对路径 (相对路径会按 cwd 解释, 容易踩坑)."""
    names = names_with(extra_names)
    lines = [f"# 自动生成: train_yolo/class_table.py (冻结类别表 v{load().get('version')})",
             f"path: {Path(dataset_dir).as_posix()}", f"train: {train}", f"val: {val}", "",
             f"nc: {len(names)}", "names:"]
    lines += [f"  {i}: {n}" for i, n in enumerate(names)]
    return "\n".join(lines) + "\n"


def add_class(name: str) -> int:
    """**只在末尾追加**一个新类 (不动已有下标), 返回其类别号. 已存在则直接返回原下标."""
    text = normalize(name)
    if not text:
        raise ValueError("类别名不能为空")
    data = load()
    names = list(data.get("classes") or []) + list(data.get("extra") or [])
    if text in names:
        return names.index(text)
    data["extra"] = list(data.get("extra") or []) + [text]
    save(data)
    load.cache_clear()
    return len(names)


def _selftest() -> int:
    names = all_names()
    classes = list(load().get("classes") or [])
    problems = []
    if len(set(names)) != len(names):
        problems.append("类别名有重复")
    if not classes:
        problems.append("冻结段 (classes) 为空")
    for idx, name in enumerate(names):
        if id_to_name(idx) != name:
            problems.append(f"往返不一致 idx={idx}")
        if name_to_id(name) != idx:
            problems.append(f"名字反查不一致 {name}")
    for skin in ("拉克丝 地狱火", "拉克丝 魔女"):
        if name_to_id(skin) != name_to_id("拉克丝"):
            problems.append(f"皮肤未合并: {skin}")
    # 附加热号必须排在冻结段之后 (否则新加类别会顶掉已训练模型的下标)
    for extra in ("基础装备锻造器", "成装锻造器", "ball", "小小英雄"):
        idx = name_to_id(extra)
        if idx is None:
            problems.append(f"附加热号缺失: {extra}")
        elif idx < len(classes):
            problems.append(f"附加热号 {extra} 落在冻结段内 (下标 {idx}), 会顶掉奕子类别号")
    for dead in ("", None, "不存在的奕子", "成装锻造器2"):
        if name_to_id(dead) is not None:
            problems.append(f"不该命中: {dead!r}")
    print(f"类别表: {len(names)} 类 (version={load().get('version')}, "
          f"奕子 {len(classes)} + 附加 {len(names) - len(classes)})")
    print("  " + " ".join(f"{i}:{n}" for i, n in enumerate(names)))
    if problems:
        print("[X] 自检失败:")
        for p in problems:
            print("   -", p)
        return 1
    print("[OK] 自检通过: 往返/皮肤合并/未知名拒绝 全部正确")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="冻结 YOLO 类别表 (train_yolo/classes.json)")
    ap.add_argument("--rebuild", action="store_true",
                    help="从爬虫库重建整张表 (会重排类别号 → 已训练模型作废, 仅换赛季时用)")
    ap.add_argument("--add", metavar="名字", help="在末尾追加一个类别 (安全)")
    ap.add_argument("--from", dest="src", help="指定 units.json 路径 (配合 --rebuild)")
    ap.add_argument("--selftest", action="store_true", help="只跑自检")
    args = ap.parse_args(argv)

    if args.rebuild:
        data = build_table(Path(args.src) if args.src else None)
        save(data)
        load.cache_clear()
        print(f"已重建类别表 → {TABLE_PATH} ({len(data['classes'])} 类, version={data['version']})")
    if args.add:
        print(f"追加类别 {args.add!r} → 类别号 {add_class(args.add)}")
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())
