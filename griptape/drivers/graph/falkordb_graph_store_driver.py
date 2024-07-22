from __future__ import annotations

import logging
from typing import Any, Optional

import redis
from falkordb import FalkorDB

from .base_graph_store_driver import BaseGraphStoreDriver

logger = logging.getLogger(__name__)


class FalkorDBGraphStoreDriver(BaseGraphStoreDriver):
    """FalkorDB Graph Store Driver with triplet handling, schema management, and relationship mapping."""

    def __init__(self, url: str, database: str = "falkor", node_label: str = "Entity", **kwargs: Any) -> None:
        """Initialize the graph store driver."""
        self._node_label = node_label
        self._driver = FalkorDB.from_url(url).select_graph(database)

        try:
            if not self.index_exists("id"):
                self._driver.query(f"CREATE INDEX FOR (n:`{self._node_label}`) ON (n.id)")
        except redis.exceptions.ResponseError as e:
            if "already indexed" in str(e):
                logger.warning("Index on 'id' already exists: %s", e)
            else:
                raise e

        self._database = database
        self.schema = ""
        self.get_query = f"""
            MATCH (n1:`{self._node_label}`)-[r]->(n2:`{self._node_label}`)
            WHERE n1.id = $subj RETURN type(r), n2.id
        """

    @property
    def client(self) -> Any:
        return self._driver

    def index_exists(self, attribute: str) -> bool:
        """Check if an index exists on a given attribute."""
        query = f"CALL db.indexes() YIELD label, properties WHERE label='{self._node_label}' AND '{attribute}' IN properties RETURN count(*)"
        result = self._driver.query(query)
        return result.result_set[0][0] > 0

    def get(self, subj: str) -> list[list[str]]:
        """Get triplets for a given subject."""
        result = self._driver.query(self.get_query, params={"subj": subj})
        return result.result_set

    def get_rel_map(
        self, subjs: Optional[list[str]] = None, depth: int = 2, limit: int = 30
    ) -> dict[str, list[list[str]]]:
        """Get flat relationship map."""
        rel_map: dict[Any, list[Any]] = {}
        if subjs is None or len(subjs) == 0:
            return rel_map

        query = f"""
            MATCH (n1:{self._node_label})
            WHERE n1.id IN $subjs
            WITH n1
            MATCH p=(n1)-[e*1..{depth}]->(z)
            RETURN p LIMIT {limit}
        """

        data = self.query(query, params={"subjs": subjs})
        if not data:
            return rel_map

        for record in data:
            nodes = record[0].nodes()
            edges = record[0].edges()

            subj_id = nodes[0].properties["id"]
            path = []
            for i, edge in enumerate(edges):
                dest = nodes[i + 1]
                dest_id = dest.properties["id"]
                path.append(edge.relation)
                path.append(dest_id)

            paths = rel_map.get(subj_id, [])
            paths.append(path)
            rel_map[subj_id] = paths

        return rel_map

    def upsert_triplet(self, subj: str, rel: str, obj: str) -> None:
        """Upsert a triplet."""
        query = """
            MERGE (n1:`%s` {id:$subj})
            MERGE (n2:`%s` {id:$obj})
            MERGE (n1)-[:`%s`]->(n2)
        """

        prepared_statement = query % (
            self._node_label,
            self._node_label,
            rel.replace(" ", "_").upper(),
        )

        self._driver.query(prepared_statement, params={"subj": subj, "obj": obj})

    def delete(self, subj: str, rel: str, obj: str) -> None:
        """Delete a triplet."""

        def delete_rel(subj: str, obj: str, rel: str) -> None:
            rel = rel.replace(" ", "_").upper()
            query = f"""
                MATCH (n1:`{self._node_label}`)-[r:`{rel}`]->(n2:`{self._node_label}`)
                WHERE n1.id = $subj AND n2.id = $obj DELETE r
            """
            self._driver.query(query, params={"subj": subj, "obj": obj})

        def delete_entity(entity: str) -> None:
            query = f"MATCH (n:`{self._node_label}`) WHERE n.id = $entity DELETE n"
            self._driver.query(query, params={"entity": entity})

        def check_edges(entity: str) -> bool:
            query = f"""
                MATCH (n1:`{self._node_label}`)--()
                WHERE n1.id = $entity RETURN count(*)
            """
            result = self._driver.query(query, params={"entity": entity})
            return bool(result.result_set)

        delete_rel(subj, obj, rel)
        if not check_edges(subj):
            delete_entity(subj)
        if not check_edges(obj):
            delete_entity(obj)

    def refresh_schema(self) -> None:
        """Refresh the FalkorDB graph schema information."""
        node_properties = self.query("CALL DB.PROPERTYKEYS()")
        relationships = self.query("CALL DB.RELATIONSHIPTYPES()")
        self.schema = f"""
        Properties: {node_properties}
        Relationships: {relationships}
        """

    def get_schema(self, *, refresh: bool = False) -> str:
        """Get the schema of the FalkorDBGraph store."""
        if self.schema and not refresh:
            return self.schema
        self.refresh_schema()
        logger.debug(f"get_schema() schema:\n{self.schema}")
        return self.schema

    def query(self, query: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Execute a query on the database."""
        result = self._driver.query(query, params=params)
        return result.result_set

    def create_connection(self, connection_params: dict) -> Optional[FalkorDB]:
        """Create a connection to FalkorDB."""
        try:
            connection = FalkorDB(**connection_params)
            return connection
        except Exception as e:
            logger.error(f"Error connecting to FalkorDB: {e}")
            return None

    # Implement abstract methods
    def delete_node(self, node_id: str) -> None:
        self.delete(node_id, "", "")

    def load_entry(self, node_id: str, namespace: Optional[str] = None) -> Optional[BaseGraphStoreDriver.Entry]:
        query = f"MATCH (n {{id: '{node_id}'}}) RETURN n"
        result = self.query(query)
        if result:
            properties = result[0][0].properties
            return BaseGraphStoreDriver.Entry(id=node_id, properties=properties)
        else:
            return None

    def load_entries(self, namespace: Optional[str] = None) -> list[BaseGraphStoreDriver.Entry]:
        query = "MATCH (n) RETURN n"
        result = self.query(query)
        entries = [BaseGraphStoreDriver.Entry(id=node.id, properties=node.properties) for node in result]
        return entries

    def upsert_node(
        self, node_id: Optional[str] = None, namespace: Optional[str] = None, meta: Optional[dict] = None, **kwargs
    ) -> str:
        self.upsert_triplet(node_id, "", "")
        return node_id
