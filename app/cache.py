"""分级规则的进程内常驻缓存。

样品集中入仓时，``GET /grade`` 不能逐件重开 SQLite 连接、重读版号和规则。
本模块为每个数据库路径维护一个进程级缓存，用一条**常驻只读连接**配合
``PRAGMA data_version`` 探测库的变化：

- ``data_version`` 是连接级计数器：自该连接上次读事务结束以来，本连接看到的
  数据库被其他连接提交修改过时，计数增加；回滚、检查点之外的无变化读取都不动它。
  因此"规则已载入且库没有新的提交"时，判级路径只剩一条廉价的 pragma，
  不重开连接、不读版号、不读规则。
- 计数变化（即有新提交）时才在**同一个读事务**内重读版号与整表规则，原子替换
  缓存快照；版号与规则必然取自同一已生效版本，不会出现各取一版的撕裂读。
- 每个进程各持一份缓存与一条常驻连接，因此多进程共用同一个库文件时，任一进程
  都能在换版提交后的下一次查询里独立发现变化并载入新版；与换版重叠的查询可能
  使用换版前任一已生效版本，但版号和等级始终来自同一快照。
- 缓存只保留**现行版**这一份快照，历史版本仍可经 ``GET /tables/{version}``
  直接查库；常驻内存占用不随历史版本数增长。
- 规则载入失败（库文件缺失、I/O 错误、库损坏等）抛 :class:`RuleLoadError`，
  缓存状态保持在上一次成功载入的快照，下一次查询会重新尝试读库，即可恢复。

并发：所有对常驻连接与缓存状态的访问都在同一把锁内串行化。同一版本的并发首查
只有一个线程真正读库，其余线程等待并复用同一快照，即每个进程对每个版本只从库
成功读取一遍规则。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .verify import Rule


class RuleLoadError(Exception):
    """规则表无法从库中载入；下次查询会重试。"""


@dataclass(frozen=True)
class Snapshot:
    """某一已生效版本的完整判级视图：版号与规则同属一版。"""

    version: int
    rules: Tuple[Rule, ...]

    def grade(self, thickness: int, strength: int) -> Optional[str]:
        """首条命中规则的等级；未命中为 None（未分级）。"""
        for rule in self.rules:
            if (
                rule.thickness.lo <= thickness <= rule.thickness.hi
                and rule.strength.lo <= strength <= rule.strength.hi
            ):
                return rule.grade
        return None


def _row_to_rule(row: sqlite3.Row) -> Rule:
    # 与 db._row_to_rule 保持一致；本地复制以避免缓存层反向依赖写路径模块。
    from .verify import Interval

    return Rule(
        thickness=Interval(row["t_min"], row["t_max"]),
        strength=Interval(row["s_min"], row["s_max"]),
        grade=row["grade"],
    )


class RuleCache:
    """单库的进程级规则缓存（线程安全）。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        # 上次成功载入快照后、常驻连接观察到的 data_version；
        # None 表示尚未成功载入过任何快照（含"还没有表"的状态）。
        self._loaded_dv: Optional[int] = None
        self._snapshot: Optional[Snapshot] = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        # 与写路径一致使用 WAL；只读长连接不阻塞写，也不会让 WAL 无限增长
        # （WAL 仅在存在读事务时无法检查点，本连接事务都极短）。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _data_version(self, conn: sqlite3.Connection) -> int:
        # 3.41 起该 pragma 带列名 data_version，更早版本为无名单列；两种情况下
        # fetchone() 都只有一个整数。必须在任何读事务之外执行，才能观察到新提交。
        return int(conn.execute("PRAGMA data_version").fetchone()[0])

    def _load_snapshot(self, conn: sqlite3.Connection) -> Optional[Snapshot]:
        """在读事务内一次性读取现行版号与该版全部规则，保证同版一致。"""
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT current_version FROM meta WHERE id = 1"
            ).fetchone()
            if row is None:
                snapshot: Optional[Snapshot] = None
            else:
                version = int(row["current_version"])
                rules = tuple(
                    _row_to_rule(r)
                    for r in conn.execute(
                        "SELECT * FROM rules WHERE version = ? ORDER BY ordinal",
                        (version,),
                    )
                )
                snapshot = Snapshot(version=version, rules=rules)
            conn.execute("COMMIT")
            return snapshot
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def get_snapshot(self) -> Optional[Snapshot]:
        """返回现行版快照；库中尚无分级表时返回 None。

        无新提交时直接复用内存快照，不触碰版号与规则数据；探测到变化或首次
        访问时才读库。读库失败抛 :class:`RuleLoadError`，旧快照原样保留。
        """
        with self._lock:
            try:
                if self._conn is None:
                    self._conn = self._connect()
                conn = self._conn
                current_dv = self._data_version(conn)
                if self._loaded_dv is not None and current_dv == self._loaded_dv:
                    return self._snapshot

                snapshot = self._load_snapshot(conn)
            except sqlite3.Error as exc:
                # 连接本身可能已失效：丢弃后由下次查询重建，实现可重试。
                self._discard_conn_locked()
                raise RuleLoadError(str(exc)) from exc
            self._snapshot = snapshot
            self._loaded_dv = current_dv
            return snapshot

    def _discard_conn_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._loaded_dv = None

    def close(self) -> None:
        """关闭常驻连接（应用停服时调用）。已缓存的快照随之失效。"""
        with self._lock:
            self._discard_conn_locked()
            self._snapshot = None


# 进程级注册表：同一 db_path 共享一个缓存；create_app 每次构造的应用互不串用。
_caches: Dict[str, RuleCache] = {}
_caches_guard = threading.Lock()


def get_cache(db_path: str) -> RuleCache:
    with _caches_guard:
        cache = _caches.get(db_path)
        if cache is None:
            cache = RuleCache(db_path)
            _caches[db_path] = cache
        return cache


def reset_caches() -> None:
    """清空进程内缓存注册表（仅供测试与生命周期重建使用）。"""
    with _caches_guard:
        for cache in _caches.values():
            cache.close()
        _caches.clear()
