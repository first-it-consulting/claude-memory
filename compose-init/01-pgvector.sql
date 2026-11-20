-- Pre-create pgvector so mem0's lazy CREATE EXTENSION IF NOT EXISTS never
-- runs into its own concurrency race (two first requests colliding on
-- pg_extension_name_index drops one of them).
CREATE EXTENSION IF NOT EXISTS vector;
