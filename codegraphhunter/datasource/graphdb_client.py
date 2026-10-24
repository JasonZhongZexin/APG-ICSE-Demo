# -*- coding: utf-8 -*-
from __future__ import print_function

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from neo4j import GraphDatabase
except Exception as e:
    GraphDatabase = None


@dataclass
class GraphDBConfig:
    uri: str
    user: str
    password: str
    database: Optional[str] = None
    fetch_size: int = 1000
    timeout_s: int = 30


class GraphDBClient(object):
    """
    TuGraph via Neo4j Bolt protocol (recommended).
    This client is intentionally tiny:
      - execute_read(query, params) -> {ok, rows, summary}
      - errors are normalized to {ok:false, error:{...}}
    """

    def __init__(self, cfg: GraphDBConfig):
        if GraphDatabase is None:
            raise RuntimeError(
                "neo4j driver not available. Please `pip install neo4j`."
            )
        if not cfg or not cfg.uri or not cfg.user:
            raise ValueError("GraphDBConfig(uri,user,password,...) is required.")
        self.cfg = cfg
        self._driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass

    def execute_read(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        try:
            with self._driver.session(database=self.cfg.database) as session:
                # Neo4j driver uses timeout in transaction function; keep it simple
                def _run(tx):
                    res = tx.run(cypher, **params)
                    rows = [r.data() for r in res]
                    summary = res.consume()
                    return rows, summary

                rows, summary = session.execute_read(_run, timeout=self.cfg.timeout_s)
                out = {
                    "ok": True,
                    "rows": rows,
                    "summary": {
                        "counters": getattr(summary, "counters", None).to_dict() if getattr(summary, "counters", None) else None,
                        "result_available_after_ms": getattr(summary, "result_available_after", None),
                        "result_consumed_after_ms": getattr(summary, "result_consumed_after", None),
                        "query_type": getattr(summary, "query_type", None),
                    },
                }
                return out
        except Exception as e:
            return {
                "ok": False,
                "error": {
                    "type": type(e).__name__,
                    "message": str(e),
                },
            }
