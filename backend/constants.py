"""Shared constants. The seed script and the API MUST agree on these."""
import hashlib

SECRET = "CHRONOS_MASTER_SECRET_2026"        # same literal as in 03_self_healing_triggers.sql
GENESIS = "0" * 64
DEMO_CONTEST_ID = 1
PLAGIARISM_PROBLEM_ID = 1                    # problem 'A' of contest 1
PLAGIARISM_HASH = hashlib.sha256(b"chronosdb-plagiarized-solution").hexdigest()
RARE_TAG = "li_chao_tree"                    # present in ~0.5% of submissions (selective -> GIN wins)
