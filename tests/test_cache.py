"""进程内规则缓存（app.cache）的行为测试。

覆盖需求：
- 无新提交时判级不重开连接、不重读版号和规则（只走一次 data_version 探针）；
- 同一版本的并发首查，每个进程只从库成功读取一遍规则；
- 换版提交后开始的查询必须用新版；与换版重叠时版号与等级取自同一版；
- 多进程共用同一库文件时各自能发现换版（子进程黑盒验证）；
- 换版失败（回滚）后仍按已生效的表供数，不触发规则重读；
- 规则载入失败报错且下次可重试，旧快照保留；
- 常驻内存只留现行版，不随历史版本数增长。
"""

from __future__ import annotations

import gc
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import db
from app.cache import RuleCache, RuleLoadError, Snapshot, get_cache, reset_caches
from app.verify import Interval, Rule


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "grading.db")
    db.init_db(path)
    yield path
    reset_caches()


def rule(t0, t1, s0, s1, g):
    return Rule(Interval(t0, t1), Interval(s0, s1), g)


def register(path, rules):
    return db.register_initial(path, rules)


def swap(path, seen, rules, at=(0, 10_000), as_=(0, 10_000)):
    return db.apply_swap(
        path,
        seen_version=seen,
        rules=rules,
        allowed_thickness=Interval(*at),
        allowed_strength=Interval(*as_),
    )


# ---------- 基本命中语义与"无表"状态 ----------

def test_no_table_returns_none(db_path):
    cache = RuleCache(db_path)
    assert cache.get_snapshot() is None
    # 没有新提交时不重复读库：再次获取仍是 None 且不报错
    assert cache.get_snapshot() is None


def test_snapshot_grade_first_match_and_ungraded(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X"), rule(50, 150, 0, 100, "Y")])
    snap = RuleCache(db_path).get_snapshot()
    assert isinstance(snap, Snapshot) and snap.version == 1
    assert snap.grade(75, 50) == "X"
    assert snap.grade(125, 50) == "Y"
    assert snap.grade(999, 999) is None  # 未命中 → 未分级


# ---------- 无新提交时不读版号、不读规则、不重开连接 ----------

class CountingCache(RuleCache):
    """记录连接打开次数与规则/版号实际读取次数。"""

    def __init__(self, path):
        super().__init__(path)
        self.opens = 0
        self.loads = 0
        self.pragma_calls = 0

    def _connect(self):
        self.opens += 1
        return super()._connect()

    def _load_snapshot(self, conn):
        self.loads += 1
        return super()._load_snapshot(conn)

    def _data_version(self, conn):
        self.pragma_calls += 1
        return super()._data_version(conn)


def test_steady_state_grading_avoids_rereads(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = CountingCache(db_path)

    for _ in range(3):
        snap = cache.get_snapshot()
        assert snap is not None and snap.version == 1
    # 只有首查真正打开连接并读取一次规则
    assert cache.opens == 1
    assert cache.loads == 1

    # 集中入仓 1000 件：连接不重开、版号规则不重读，只剩 pragma 探针
    for t in range(1000):
        s = cache.get_snapshot()
        assert s is not None and s.grade(t % 101, 0) == ("X" if t % 101 <= 100 else None)
    assert cache.opens == 1
    assert cache.loads == 1
    assert cache.pragma_calls == 1003


def test_pragma_is_cheaper_than_full_read(db_path):
    """探针路径不访问 meta/rules 表：用 SQLite 追踪佐证只读 pragma。"""
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = RuleCache(db_path)
    cache.get_snapshot()  # 预热

    conn = cache._conn  # 进程内常驻连接
    statements: list[str] = []
    conn.set_trace_callback(lambda sql: statements.append(sql))
    cache.get_snapshot()  # 无变化的查询
    conn.set_trace_callback(None)

    assert statements == ["PRAGMA data_version"]
    assert not any("rules" in s or "meta" in s for s in statements)


# ---------- 换版提交后必须读到新版；回滚不影响现行版 ----------

def test_committed_swap_is_picked_up(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = RuleCache(db_path)
    assert cache.get_snapshot().version == 1

    swap(db_path, 1, [rule(0, 100, 0, 100, "Z")])
    snap = cache.get_snapshot()
    assert snap.version == 2 and snap.grade(50, 50) == "Z"

    # 再次查询无新提交，不再重读
    assert cache.get_snapshot() is snap


def test_repeated_swaps_each_loaded_once(db_path):
    register(db_path, [rule(0, 100, 0, 100, "G1")])
    cache = CountingCache(db_path)
    cache.get_snapshot()
    assert cache.loads == 1

    for v in range(2, 6):
        swap(db_path, v - 1, [rule(0, 100, 0, 100, f"G{v}")])
        snap = cache.get_snapshot()
        assert snap.version == v and snap.grade(0, 0) == f"G{v}"

    assert cache.loads == 5  # 首版 + 4 次换版，每版恰好读一遍
    assert cache.opens == 1  # 常驻连接全程不重开


def test_rolled_back_swap_keeps_effective_table(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = CountingCache(db_path)
    snap_v1 = cache.get_snapshot()

    # 核对失败 → 事务回滚，库没有新提交
    result = swap(
        db_path,
        1,
        [rule(0, 100, 0, 100, "Q"), rule(200, 300, 0, 100, "BAD")],
        at=(0, 100),
    )
    assert isinstance(result, db.SwapRejected)

    snap = cache.get_snapshot()
    assert snap is snap_v1  # 同一快照对象，未发生重读
    assert snap.version == 1 and snap.grade(50, 50) == "X"
    assert cache.loads == 1


def test_external_commit_observed_without_reopen(db_path):
    """绕过本进程写路径、由外部连接提交的修改也必须被发现。"""
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = CountingCache(db_path)
    cache.get_snapshot()

    other = db.connect(db_path)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO tables (version) VALUES (2)")
        other.executemany(
            "INSERT INTO rules (version, ordinal, t_min, t_max, s_min, s_max, grade)"
            " VALUES (2, ?, ?, ?, ?, ?, ?)",
            [(0, 0, 100, 0, 100, "EXT")],
        )
        other.execute("UPDATE meta SET current_version = 2 WHERE id = 1")
        other.execute("COMMIT")
    finally:
        other.close()

    snap = cache.get_snapshot()
    assert snap.version == 2 and snap.grade(0, 0) == "EXT"
    assert cache.opens == 1  # 仍用常驻连接，只是重读了一次版号和规则


# ---------- 同版并发首查：每个进程只读一遍 ----------

def test_concurrent_first_load_reads_rules_once(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = CountingCache(db_path)
    barrier = threading.Barrier(8)
    snapshots: list[Snapshot | None] = []
    errors: list[Exception] = []

    def worker():
        barrier.wait()
        try:
            snapshots.append(cache.get_snapshot())
        except Exception as exc:  # pragma: no cover - 失败时留证
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert cache.loads == 1 and cache.opens == 1
    assert all(s is snapshots[0] for s in snapshots)
    assert snapshots[0].version == 1


def test_concurrent_queries_after_swap_stay_consistent(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = RuleCache(db_path)
    cache.get_snapshot()
    swap(db_path, 1, [rule(0, 100, 0, 100, "Y")])

    results = []

    def worker():
        snap = cache.get_snapshot()
        # 版号与等级必须来自同一快照
        results.append((snap.version, snap.grade(10, 10)))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {(2, "Y")}


# ---------- 载入失败：报错、保留旧快照、下次可重试 ----------

def test_load_failure_raises_and_keeps_last_snapshot(db_path):
    register(db_path, [rule(0, 100, 0, 100, "X")])
    cache = RuleCache(db_path)
    good = cache.get_snapshot()
    assert good.version == 1

    # 破坏库文件，使下一次读库失败（连接被丢弃，旧快照仍保留在内存）
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            p.write_bytes(b"this is not a sqlite database")

    with pytest.raises(RuleLoadError):
        cache.get_snapshot()
    assert cache._snapshot is good  # 旧快照保留，未被失败的载入污染
    assert cache._conn is None  # 失效连接已清理，下次查询重建

    # 重建空库后下次查询可重试：先无表，再登记首版
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            p.unlink()
    db.init_db(db_path)
    assert cache.get_snapshot() is None
    db.register_initial(db_path, [rule(0, 5, 0, 5, "NEW")])
    snap = cache.get_snapshot()
    assert snap is not None and snap.version == 1
    assert snap.grade(1, 1) == "NEW" and snap.grade(9, 9) is None


def test_missing_database_file_retries_after_recreation(tmp_path):
    path = str(tmp_path / "not_yet_exists.db")
    cache = RuleCache(path)
    with pytest.raises(RuleLoadError):
        cache.get_snapshot()

    db.init_db(path)
    # 尚无表 → None，而不是报错
    assert cache.get_snapshot() is None
    db.register_initial(path, [rule(0, 1, 0, 1, "OK")])
    snap = cache.get_snapshot()
    assert snap.version == 1 and snap.grade(0, 0) == "OK"


# ---------- 版号与等级同版：快照原子替换，无撕裂读 ----------

def test_snapshot_is_atomic_pair(db_path):
    register(db_path, [rule(0, 100, 0, 100, "V1")])
    cache = RuleCache(db_path)
    first = cache.get_snapshot()
    swap(db_path, 1, [rule(0, 100, 0, 100, "V2")])
    second = cache.get_snapshot()

    assert first is not second
    # 旧快照对象仍自洽：版号 1 配 V1 等级，不会被后续换版就地改写
    assert (first.version, first.grade(0, 0)) == (1, "V1")
    assert (second.version, second.grade(0, 0)) == (2, "V2")
    assert tuple(r.grade for r in first.rules) == ("V1",)
    assert tuple(r.grade for r in second.rules) == ("V2",)


# ---------- 内存不随历史版本数增长 ----------

def test_memory_retains_only_current_version(db_path):
    import weakref

    register(db_path, [rule(0, 100, 0, 100, "G1")])
    cache = RuleCache(db_path)
    first_ref = weakref.ref(cache.get_snapshot())

    for v in range(2, 12):
        swap(db_path, v - 1, [rule(0, 100, 0, 100, f"G{v}")])
        cache.get_snapshot()

    gc.collect()
    # 库里累积了 11 个版本，旧快照在缓存外无引用时应已回收
    assert first_ref() is None
    # 缓存内只保留现行版这一份
    current = cache.get_snapshot()
    assert current.version == 11
    assert cache._snapshot is current
    current_ref = weakref.ref(current)
    del current
    gc.collect()
    assert current_ref() is not None  # 现行版仍被缓存持有
    cache.close()
    gc.collect()
    assert current_ref() is None  # 关闭后无残留引用


def test_rules_tuple_does_not_accumulate_history(db_path):
    register(db_path, [rule(0, 100, 0, 100, "G1")])
    cache = RuleCache(db_path)
    cache.get_snapshot()
    for v in range(2, 21):
        swap(db_path, v - 1, [rule(0, 100, 0, 100, f"G{v}")])
        cache.get_snapshot()

    snap = cache.get_snapshot()
    # 无论经历多少版，常驻结构里只有现行版的 ≤40 条规则
    assert snap.version == 20
    assert len(snap.rules) == 1
    assert len(cache._snapshot.rules) == 1


# ---------- 进程级注册表与生命周期 ----------

def test_get_cache_same_path_shares_instance(db_path):
    a = get_cache(db_path)
    b = get_cache(db_path)
    assert a is b
    register(db_path, [rule(0, 1, 0, 1, "X")])
    assert a.get_snapshot() is b.get_snapshot()
    reset_caches()
    assert get_cache(db_path) is not a


def test_close_releases_persistent_connection(db_path):
    register(db_path, [rule(0, 1, 0, 1, "X")])
    cache = RuleCache(db_path)
    cache.get_snapshot()
    conn = cache._conn
    cache.close()
    assert cache._conn is None and cache._snapshot is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


# ---------- 多进程共用同一库：子进程独立发现换版 ----------

def test_other_process_observes_swap(tmp_path):
    path = tmp_path / "shared.db"
    db.init_db(str(path))
    db.register_initial(str(path), [rule(0, 100, 0, 100, "OLD")])

    signal = tmp_path / "go"
    worker_code = (
        "import os, sys, time\n"
        "from app.cache import RuleCache\n"
        "path = sys.argv[1]\n"
        "cache = RuleCache(path)\n"
        "s1 = cache.get_snapshot()\n"
        "print(s1.version, s1.grade(10, 10), flush=True)\n"
        "while not os.path.exists(sys.argv[2]):\n"
        "    time.sleep(0.02)\n"
        "s2 = cache.get_snapshot()\n"
        "print(s2.version, s2.grade(10, 10), flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(path), str(signal)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )

    # 等子进程打印第一行（已载入 v1）
    first_line = proc.stdout.readline().strip()
    assert first_line == "1 OLD"

    # 父进程执行换版提交
    result = db.apply_swap(
        str(path),
        seen_version=1,
        rules=[rule(0, 100, 0, 100, "NEW")],
        allowed_thickness=Interval(0, 100),
        allowed_strength=Interval(0, 100),
    )
    assert isinstance(result, db.SwapOk) and result.version == 2
    signal.write_text("go")

    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, err
    assert (first_line + "\n" + out).strip().splitlines() == ["1 OLD", "2 NEW"]
