from dagster import asset, Definitions
import requests
from neo4j import GraphDatabase
from portal_model import *
from uuid import uuid4
import pandas as pd
from rdflib import Graph, Literal
import re

# Neo4j Connection Details
NEO4J_URI = "bolt://localhost:7687"  # Change this if Neo4j is hosted elsewhere
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "mysecret"

## Preprocess Functions

def preprocess_gcat_info(gcat_info: pd.DataFrame) -> pd.DataFrame:
    """
    Preprocess the GCAT information to extract the phenotype name and ID.
    """
    def process_col13(value):
        if pd.isna(value) or value == "":
            return None
        value = value.replace(" ", "_")  # Replace spaces with underscores
        value = re.sub(r"[^A-Za-z0-9_]", "", value)  # Remove special characters
        return f"gcat_trait_{value}"

    # Function to process column 14
    def process_col14(value):
        if pd.isna(value) or value == "":
            return None
        last_part = value.strip().split("/")[-1]  # Extract last part of the URL
        return last_part.replace("_", ":")  # Replace underscores with colons

    # Apply transformations
    gcat_info["processed_trait_name"] = gcat_info.iloc[:, 12].apply(process_col13)  # Column 13 (zero-based index 12)
    gcat_info["processed_curie"] = gcat_info.iloc[:, 13].apply(process_col14)  # Column 14 (zero-based index 13)

    # Keep only non-empty transformed rows
    gcat_info_filtered = gcat_info.dropna(subset=["processed_trait_name", "processed_curie"])
    return gcat_info_filtered

# Data Mappings
portal_phenotype_info = pd.read_csv("data/amp-traits-mapping-portal-phenotypes_06262024.csv")
gcat_phenotype_info = pd.read_csv("data/gcat_v1.0.3.1.tsv", sep="\t")
gcat_phenotype_info = preprocess_gcat_info(gcat_phenotype_info)
orphanet_owl = Graph()
orphanet_owl.parse("data/ORDO_en_4.5.owl", format="xml")

## Functions

def lookup_trait_with_db_refs(orpha_id):
    """
    Look up an Orphanet trait by its ORPHA ID and extract all database references.
    
    :param orpha_id: The numeric Orphanet ID (e.g., "93460")
    :return: Dictionary with trait details including database references
    """

    trait = f"http://www.orpha.net/ORDO/Orphanet_{orpha_id}"

    query = f"""
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX efo: <http://www.ebi.ac.uk/efo/>
    PREFIX owl: <http://www.w3.org/2002/07/owl#>
    PREFIX oboInOwl: <http://www.geneontology.org/formats/oboInOwl#>

    SELECT ?label ?description ?db_xref
    WHERE {{
        <{trait}> a owl:Class .
        <{trait}> rdfs:label ?label .
        OPTIONAL {{ <{trait}> efo:definition ?description . }}
        OPTIONAL {{ <{trait}> oboInOwl:hasDbXref ?db_xref . }}
    }}
    """

    results = orphanet_owl.query(query)
    
    trait_info = {
        "Orphanet ID": orpha_id,
        "Trait URI": None,
        "Label": None,
        "Description": None,
        "Database References": []
    }

    for row in results:
        if trait_info["Trait URI"] is None:
            trait_info["Trait URI"] = trait
            trait_info["Label"] = str(row.label)
            trait_info["Description"] = str(row.description) if row.description else "No description available"
        
        # Add database references if available
        if row.db_xref and isinstance(row.db_xref, Literal):
            trait_info["Database References"].append(str(row.db_xref))

    return trait_info if trait_info["Trait URI"] else None

def create_orphanet_phenotype(phenotype: str, phenotype_name: str) -> Phenotype:
    orphanet_id = phenotype.split("_")[-1]
    trait_info = lookup_trait_with_db_refs(orphanet_id)
    if trait_info:
        return Phenotype(
            id=trait_info["Trait URI"],
            name=phenotype,
            display_name=phenotype_name,
            description=trait_info["Description"],
            has_xrefs=trait_info["Database References"]
        )
    else:
        return None
    
def create_gcat_phenotype(phenotype: str, phenotype_name: str) -> Phenotype:
    gcat_info = gcat_phenotype_info[gcat_phenotype_info["processed_trait_name"] == phenotype]
    phenotypes = []
    studies = []
    if gcat_info.empty:
        return None
    study_info = gcat_info[["processed_trait_name", "STUDY ACCESSION"]].drop_duplicates()
    xrefs = gcat_info[["processed_trait_name", "processed_curie"]].drop_duplicates()
    # Make single phenotype and grab xrefs
    phenotype_obj = Phenotype(
        id=xrefs["processed_curie"].values[0],
        name=phenotype,
        display_name=phenotype_name,
        description="No description available",
        has_xrefs=xrefs["processed_curie"].tolist()
    )

    # Create studies
    for index, row in study_info.iterrows():
        # Extract GCST ID from study accession
        gwas_id = row["STUDY ACCESSION"].split("GCST")[-1]
        studies.append(Gwas(
            id=f'GCST:{gwas_id}',
            description=row["STUDY"],
            phenotype=phenotype_obj
        ))
    return phenotype_obj, studies

def create_portal_phenotype(phenotype: str, phenotype_name: str) -> Phenotype:
    portal_info = portal_phenotype_info[portal_phenotype_info["name"] == phenotype]
    if portal_info.empty:
        return None
    portal_id = portal_info.iloc[0]
    curies = portal_id["EFO_id"].split(",")
    return Phenotype(
        id=f'PORTAL.TRAIT:{portal_id[id]}',
        name=phenotype,
        display_name=phenotype_name,
        description=portal_id["description"],
        has_xrefs=curies
    )

## Pipeline

# Step 1: Fetch Phenotype Data
@asset
def fetch_phenotype_data():
    url = "https://bioindex-dev.hugeamp.org/api/bio/query/pigean-phenotypes?q=1"
    all_data = []
    
    response = requests.get(url)
    if response.status_code != 200:
        raise ValueError(f"API request failed with status {response.status_code}")
        
    data = response.json()
    all_data.extend(data['data'])
    
    # Keep fetching while we have a continuation token
    while data['continuation']:
        cont_url = f"https://bioindex-dev.hugeamp.org/api/bio/cont?token={data['continuation']}"
        response = requests.get(cont_url)
        if response.status_code != 200:
            raise ValueError(f"Continuation request failed with status {response.status_code}")
            
        data = response.json()
        all_data.extend(data['data'])
        
    return all_data

# Step 2: Transform Phenotype Data
@asset
def transform_phenotype_data(fetch_phenotype_data):
    transformed = []
    for item in fetch_phenotype_data:
        if "Orphanet" in item["phenotype"]:
            phenotype = create_orphanet_phenotype(item["phenotype"], item["phenotype_name"])
            if phenotype:
                transformed.append(phenotype)
        elif "gcat_trait" in item["phenotype"]:
            phenotype, studies = create_gcat_phenotype(item["phenotype"], item["phenotype_name"])
            if phenotype:
                transformed.append(phenotype)
                transformed.extend(studies)
        else:
            phenotype = create_portal_phenotype(item["phenotype"], item["phenotype_name"])
            if phenotype:
                transformed.append(phenotype)
    return transformed

# Step 3: Insert into Neo4j
def insert_data(tx, data):
    query = """
    MERGE (p:Post {id: $id})
    SET p.title = $title, p.body = $body
    """
    tx.run(query, id=data["id"], title=data["title"], body=data["body"])

@asset
def insert_into_neo4j(transform_data):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        for item in transform_data:
            session.write_transaction(insert_data, item)
    driver.close()
    return f"Inserted {len(transform_data)} records into Neo4j"

# Define the Dagster repository
defs = Definitions(assets=[fetch_phenotype_data, transform_phenotype_data])
