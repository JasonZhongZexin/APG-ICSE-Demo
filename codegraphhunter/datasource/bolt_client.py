# -*- coding: utf-8 -*-
from __future__ import print_function

from typing import Any, Dict, Optional

from neo4j import GraphDatabase, Query


class BoltClient(object):
    def __init__(self, uri: str, user: str, password: str, database: str = "default"):
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
    
    @property
    def database(self) -> str:
        return self._database

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass

    def run(self, cypher: str, params: Optional[Dict[str, Any]] = None, timeout_s: int = 30) -> Dict[str, Any]:
        """
        Returns:
          {
            "records": [ { ...row... }, ... ],
            "summary": { ...optional... }
          }
        """
        if params is None:
            params = {}

        with self._driver.session(database=self._database) as session:
            # Session.run keyword arguments are Cypher parameters, not transaction settings.
            res = session.run(Query(cypher, timeout=float(timeout_s)), params)

            # materialize records
            data = res.data()  # list[dict]
            try:
                summary = res.consume()
                summary_dict = {
                    "counters": summary.counters.__dict__ if hasattr(summary, "counters") else None
                }
            except Exception:
                summary_dict = None

            return {
                "records": data,
                "summary": summary_dict,
            }
