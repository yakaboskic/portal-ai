from kg_ingress.assets import *
import pandas as pd
from rdflib import Graph
from neo4j import GraphDatabase
import argparse
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger('pipeline')

# Neo4J Connection Details
NEO4J_URI = "bolt://localhost:7687"  # Change this if Neo4j is hosted elsewhere
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "mysecret"

# Neo4j Driver
logger.info("Creating Neo4j driver")
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
logger.info("Neo4j driver created")

# Data Mappings
logger.info("Loading data files")
portal_phenotype_info = pd.read_csv("data/amp-traits-mapping-portal-phenotypes_06262024.csv")
gcat_phenotype_info = pd.read_csv("data/gcat_v1.0.3.1.tsv", sep="\t")
gcat_phenotype_info = preprocess_gcat_info(gcat_phenotype_info)
orphanet_owl = Graph()
orphanet_owl.parse("data/ORDO_en_4.5.owl", format="xml")
logger.info("Data files loaded")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--verbose", action="store_true", help="Run in verbose mode")
    parser.add_argument("--clean-db", action="store_true", help="Clean the database before running")
    parser.add_argument("--log-level", type=str, default="INFO", help="Set the log level")
    args = parser.parse_args()

    # Set log level based on argument
    log_level = getattr(logging, args.log_level.upper())
    logger.setLevel(log_level)

    if args.clean_db:
        logger.warning("Cleaning database")
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.info("Database cleaned")

    logger.info("Fetching phenotype data")
    data = fetch_phenotype_data()
    if args.test:
        logger.debug("Running in test mode")
        data = data[:10]
    logger.info("Transforming phenotype data")
    transformed = transform_phenotype_data(data, portal_phenotype_info, gcat_phenotype_info, orphanet_owl, verbose=True)
    logger.info("Inserting data into Neo4j")
    insert_data(transformed, driver=driver)
    logger.info("Done")