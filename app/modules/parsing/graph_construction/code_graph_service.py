import hashlib
import logging
from typing import Dict, Optional
import time 
from neo4j import GraphDatabase
from sqlalchemy.orm import Session

from app.modules.parsing.graph_construction.parsing_repomap import RepoMap
from app.modules.search.search_service import SearchService


class CodeGraphService:
    def __init__(self, neo4j_uri, neo4j_user, neo4j_password, db: Session):
        self.driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self.db = db

    @staticmethod
    def generate_node_id(path: str, user_id: str):
        # Concatenate path and signature
        combined_string = f"{user_id}:{path}"

        # Create a SHA-1 hash of the combined string
        hash_object = hashlib.md5()
        hash_object.update(combined_string.encode("utf-8"))

        # Get the hexadecimal representation of the hash
        node_id = hash_object.hexdigest()

        return node_id

    def close(self):
        self.driver.close()

    def create_and_store_graph(self, repo_dir, project_id, user_id):
        # Create the graph using RepoMap
        self.repo_map = RepoMap(
            root=repo_dir,
            verbose=True,
            main_model=SimpleTokenCounter(),
            io=SimpleIO(),
        )

        nx_graph = self.repo_map.create_graph(repo_dir)

        with self.driver.session() as session:
            # First, clear any existing data for this project
            session.run(
                """
                MATCH (n {repoId: $project_id})
                DETACH DELETE n
                """,
                project_id=project_id,
            )

            start_time = time.time()
            node_count = nx_graph.number_of_nodes()
            logging.info(f"Creating {node_count} nodes")

            # Batch insert nodes
            batch_size = 300
            for i in range(0, node_count, batch_size):
                batch_nodes = list(nx_graph.nodes(data=True))[i : i + batch_size]
                nodes_to_create = []
                
                for node_id, node_data in batch_nodes:
                    # Get the node type and ensure it's one of our expected types
                    node_type = node_data.get("type", "UNKNOWN")
                    if node_type == "UNKNOWN":
                        continue
                    # Initialize labels with NODE
                    labels = ["NODE"]
                    
                    # Add specific type label if it's a valid type
                    if node_type in ["FILE", "CLASS", "FUNCTION", "INTERFACE"]:
                        labels.append(node_type)
                    
                    # Prepare node data
                    processed_node = {
                        "name": node_data.get("name", node_id),  # Use node_id as fallback
                        "file_path": node_data.get("file", ""),
                        "start_line": node_data.get("line", -1),
                        "end_line": node_data.get("end_line", -1),
                        "repoId": project_id,
                        "node_id": CodeGraphService.generate_node_id(node_id, user_id),
                        "entityId": user_id,
                        "type": node_type,
                        "text": node_data.get("text", ""),
                        "labels": labels,
                    }
                    
                    # Remove None values
                    processed_node = {k: v for k, v in processed_node.items() if v is not None}
                    nodes_to_create.append(processed_node)

                # Create nodes with labels
                session.run(
                    """
                    UNWIND $nodes AS node
                    CALL apoc.create.node(node.labels, node) YIELD node AS n
                    RETURN count(*) AS created_count
                    """,
                    nodes=nodes_to_create,
                )

            relationship_count = nx_graph.number_of_edges()
            logging.info(f"Creating {relationship_count} relationships")

            # Create relationships in batches
            for i in range(0, relationship_count, batch_size):
                batch_edges = list(nx_graph.edges(data=True))[i : i + batch_size]
                edges_to_create = []
                for source, target, data in batch_edges:
                    edge_data = {
                        "source_id": CodeGraphService.generate_node_id(source, user_id),
                        "target_id": CodeGraphService.generate_node_id(target, user_id),
                        "type": data.get("type", "REFERENCES"),
                        "repoId": project_id,
                    }
                    # Remove any null values from edge_data
                    edge_data = {k: v for k, v in edge_data.items() if v is not None}
                    edges_to_create.append(edge_data)

                session.run(
                    """
                    UNWIND $edges AS edge
                    MATCH (source:NODE {node_id: edge.source_id, repoId: edge.repoId})
                    MATCH (target:NODE {node_id: edge.target_id, repoId: edge.repoId})
                    CALL apoc.create.relationship(source, edge.type, {repoId: edge.repoId}, target) YIELD rel
                    RETURN count(rel) AS created_count
                    """,
                    edges=edges_to_create,
                )

            end_time = time.time()
            logging.info(f"Time taken to create graph and search index: {end_time - start_time:.2f} seconds")
            
    def cleanup_graph(self, project_id: str):
        with self.driver.session() as session:
            session.run(
                """
                MATCH (n {repoId: $project_id})
                DETACH DELETE n
                """,
                project_id=project_id,
            )

        # Clean up search index
        search_service = SearchService(self.db)
        search_service.delete_project_index(project_id)

    async def get_node_by_id(self, node_id: str, project_id: str) -> Optional[Dict]:
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:NODE {node_id: $node_id, repoId: $project_id})
                RETURN n
                """,
                node_id=node_id,
                project_id=project_id,
            )
            record = result.single()
            return dict(record["n"]) if record else None

    def query_graph(self, query):
        with self.driver.session() as session:
            result = session.run(query)
            return [record.data() for record in result]


class SimpleIO:
    def read_text(self, fname):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            logging.warning(f"Could not read {fname} as UTF-8. Skipping this file.")
            return ""

    def tool_error(self, message):
        logging.error(f"Error: {message}")

    def tool_output(self, message):
        logging.info(message)


class SimpleTokenCounter:
    def token_count(self, text):
        return len(text.split())
