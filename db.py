import os
from pymongo import MongoClient
from dotenv import load_dotenv
from config import MONGO_URI, DB_NAME

load_dotenv()

if not MONGO_URI:
    raise RuntimeError("MONGO_URI missing")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
